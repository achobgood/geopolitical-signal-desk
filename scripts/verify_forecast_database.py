#!/usr/bin/env python3
"""Exercise the forecast migration only in a disposable local PostgreSQL cluster.

Usage: python scripts/verify_forecast_database.py --isolated-local-only

No database URL or host can be supplied. It creates synthetic SPY fixtures and
recreates ONLY the disposable issuer function with a fixed clock to test the
positive issuance path. The original function is tested against the actual clock
first. These checks establish database invariants, never predictive performance.
Requires local PostgreSQL 16+ initdb, pg_ctl and psql; no new CI gate is installed.
"""
import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path


def build_checks(repo):
    sys.path.insert(0, str(repo))
    from app.market_calendar import is_expected_market_session
    source=(repo/'db/market_forecast_close_availability_v6.sql').read_text()
    fn=re.search(r'create or replace function private.market_issue_forecast_v1\(p jsonb\).*?end \$\$;',source,re.S).group(0)
    real_issue=fn
    real_capture=re.search(r'create or replace function private.market_capture_forecast_prices_v1\(p jsonb\).*?end \$\$;',source,re.S).group(0)
    fn=fn.replace('t timestamptz:=clock_timestamp()',"t timestamptz:='2026-11-04T18:00:00-05:00'::timestamptz")
    days=[]; day=date(2026,11,5)
    while len(days)<61:
        if is_expected_market_session(day): days.append(day)
        day-=timedelta(days=1)
    days.reverse()
    bars=[dict(session=str(d),open='100',high='102',low='98',close='101' if d==date(2026,11,5) else '100' if d==date(2026,11,4) else '99') for d in days]
    p=dict(symbol='SPY',provider='alpaca',feed='sip',adjustment='split',source_url='https://data.alpaca.markets/v2/stocks/SPY/bars',bars_sha256='a'*64,input_bars=bars,quality={})
    forecast=dict(symbol='SPY',target_session='2026-11-05',reference_session='2026-11-04',reference_capture_id=2,model_version='momentum-1-research-v1',code_hash='c'*64,config_hash='d'*64,configuration={},direction='UP',probability=None,features={'source_price_capture_id':2,'usable_at':'2026-11-04T17:00:00-05:00'},benchmarks={'always_up':'UP','momentum':'UP','training_majority':'UP'},abstention_reasons=[],supporting_evidence=[],counterevidence=[],missing_inputs=[],invalidation=[],selected_strategy=False,issued_at='2000-01-01T00:00:00Z')
    def q(obj): return "'"+json.dumps(obj).replace("'","''")+"'::jsonb"
    sql='''\\set ON_ERROR_STOP on
    create function pg_temp.require(ok boolean,msg text) returns void language plpgsql as $$ begin if ok is distinct from true then raise exception '%',msg; end if; end $$;
    select pg_temp.require((select count(*)=7 from pg_class where relnamespace='public'::regnamespace and relname like 'market_%' and relkind='r' and relrowsecurity),'All seven research tables have RLS');
    select pg_temp.require(not has_table_privilege('service_role','public.market_forecasts','insert') and not has_table_privilege('service_role','public.market_forecast_prices','update'),'service_role cannot insert/update research tables directly');
    select pg_temp.require(not has_function_privilege('anon','public.market_issue_forecast_v1(jsonb)','execute') and not has_function_privilege('authenticated','public.market_public_forecasts_v1()','execute'),'end users cannot invoke RPCs');
    select pg_temp.require(has_function_privilege('service_role','public.market_issue_forecast_v1(jsonb)','execute'),'service_role can invoke private boundary');
    select pg_temp.require(public.market_is_session_v1('2027-12-31') and public.market_next_session_v1('2026-11-25')='2026-11-27' and not public.market_is_session_v1('2029-01-02'),'verified calendar and expiry');
    set role service_role;
    do $$ declare rejected boolean:=false; begin begin insert into public.market_forecast_prices(symbol) values('SPY'); exception when insufficient_privilege then rejected:=true; end; perform pg_temp.require(rejected,'actual direct service-role INSERT denied'); end $$;
    reset role;
    set role anon;
    do $$ declare rejected boolean:=false; begin begin perform public.market_issue_forecast_v1('{}'::jsonb); exception when insufficient_privilege then rejected:=true; end; perform pg_temp.require(rejected,'actual anonymous RPC denied'); end $$;
    reset role;
    '''
    sql+='set role service_role;\nselect pg_temp.require((public.market_capture_forecast_prices_v1('+q(p)+")->>'id')='1','actual valid capture inserts');\nreset role;\n"
    sql+="""do $$ declare rejected boolean:=false; begin begin perform public.market_issue_forecast_v1('{}'::jsonb); exception when others then rejected:=sqlerrm like 'Outside prior trading day%' or sqlerrm like 'Forecast target/model contract mismatch%'; end; perform pg_temp.require(rejected,'backdated/invalid issuance rejected against actual server clock'); end $$;
    """
    # The daily timestamp is not completion evidence: at16:15ET, Basic end15:59.
    partial=json.loads(json.dumps(p)); partial['input_bars']=bars[:-1]; partial['bars_sha256']='0'*64
    for early in ('16:15:00','16:29:59.999999'):
        instant="('2026-11-04T"+early+"-05:00'::timestamptz)"
        early_capture=real_capture.replace('clock_timestamp()',instant)
        early_issue=real_issue.replace('clock_timestamp()',instant)
        sql+=early_capture+'\n'+early_issue+'\n'
        sql+="do $$ declare rejected boolean:=false; begin begin perform public.market_capture_forecast_prices_v1("+q(partial)+"); exception when others then rejected:=sqlerrm='Incomplete current session'; end; perform pg_temp.require(rejected,'current daily capture rejected before16:30: "+early+"'); end $$;\n"
        sql+="do $$ declare rejected boolean:=false; begin begin perform public.market_issue_forecast_v1("+q(forecast)+"); exception when others then rejected:=sqlerrm like 'Outside prior trading day%'; end; perform pg_temp.require(rejected,'issuance rejected before16:30: "+early+"'); end $$;\n"
    sql+=real_capture+'\n'+real_issue+'\n'
    # Invalid capture tries each get a distinct hash but must never add a row.
    for label, mut in [('missing_ohlc',lambda x:x['input_bars'][0].pop('open')),('gap',lambda x:x['input_bars'].pop(20)),('nan',lambda x:x['input_bars'][0].update(close='NaN')),('holiday',lambda x:x['input_bars'][20].update(session='2026-09-07'))]:
        obj=json.loads(json.dumps(p)); mut(obj); obj['bars_sha256']='b'*64
        sql+="do $$ declare rejected boolean:=false; begin begin perform public.market_capture_forecast_prices_v1("+q(obj)+"); exception when others then rejected:=true; end; perform pg_temp.require(rejected,'invalid capture rejected: "+label+"'); end $$;\n"
    sql+="select pg_temp.require((select count(*)=1 from public.market_forecast_prices),'rejected captures leave no rows');\n"
    sql+="insert into public.market_forecast_prices(id,symbol,provider,feed,adjustment,source_url,captured_at,observed_session,bars_sha256,input_bars,quality) overriding system value values(2,'SPY','alpaca','sip','split','https://data.alpaca.markets/v2/stocks/SPY/bars','2026-11-04T17:00:00-05:00','2026-11-04','"+'e'*64+"',"+q(bars[:-1])+",'{}');\n"
    # A pre-buffer capture cannot be reused later even when prices are unchanged.
    sql+="insert into public.market_forecast_prices(id,symbol,provider,feed,adjustment,source_url,captured_at,observed_session,bars_sha256,input_bars,quality) overriding system value values(7,'SPY','alpaca','sip','split','https://data.alpaca.markets/v2/stocks/SPY/bars','2026-11-04T16:15:00-05:00','2026-11-04','"+'f'*64+"',"+q(bars[:-1])+",'{}');\n"
    early_forecast=json.loads(json.dumps(forecast)); early_forecast['reference_capture_id']=7; early_forecast['features']={'source_price_capture_id':7,'usable_at':'2026-11-04T16:15:00-05:00'}
    sql+=fn+'\n'
    sql+="do $$ declare rejected boolean:=false; begin begin perform public.market_issue_forecast_v1("+q(early_forecast)+"); exception when others then rejected:=sqlerrm='Unavailable/stale reference'; end; perform pg_temp.require(rejected,'a pre-buffer capture cannot qualify at a later issuance time'); end $$;\n"
    sql+='set role service_role;\nselect pg_temp.require((public.market_issue_forecast_v1('+q(forecast)+")->>'direction')='UP','valid clock-fixture issuance succeeds');\n"

    forecast['counterevidence']=['Modified duplicate must preserve original']
    sql+='select pg_temp.require((public.market_issue_forecast_v1('+q(forecast)+")->>'direction')='UP','duplicate cannot overwrite locked direction');\nreset role;\n"
    sql+="select pg_temp.require((select issued_at='2026-11-04T18:00:00-05:00' from public.market_forecasts where id=1),'caller backdated issued_at ignored');\n"
    sql+="set role service_role;\nselect pg_temp.require((public.market_evaluate_forecast_v1(1,1,'OBSERVED','')->>'actual_direction')='UP','later real capture evaluates exact target');\nreset role;\n"
    # Direct fixture insertion is deliberately postgres-only in disposable DB.
    for ident,stamp,base,target in [(3,'2026-11-05T18:00:00-05:00','100',None),(4,'2026-11-04T16:00:00-05:00','100','101'),(5,'2026-11-05T18:01:00-05:00','99','101'),(6,'2026-11-05T18:02:00-05:00','100','99')]:
        fixture=[dict(session='2026-11-04',close=base)]
        if target is not None: fixture.append(dict(session='2026-11-05',close=target))
        sql+="insert into public.market_forecast_prices(id,symbol,provider,feed,adjustment,source_url,captured_at,observed_session,bars_sha256,input_bars,quality) overriding system value values("+str(ident)+",'SPY','alpaca','sip','split','https://data.alpaca.markets/v2/stocks/SPY/bars','"+stamp+"','2026-11-05','"+str(ident)*64+"',"+q(fixture)+",'{}');\n"
    sql+="""set role service_role;
    select pg_temp.require((public.market_evaluate_forecast_v1(1,3,'MISSING_PRICE','')->>'status')='MISSING_PRICE','missing target never substitutes adjacent session');
    do $$ declare rejected boolean:=false; begin begin perform public.market_evaluate_forecast_v1(1,4,'OBSERVED',''); exception when others then rejected:=sqlerrm like 'Outcome source identity/time mismatch%'; end; perform pg_temp.require(rejected,'pre-issuance capture cannot evaluate'); end $$;
    select pg_temp.require((public.market_evaluate_forecast_v1(1,5,'SOURCE_REVISED','')->>'status')='SOURCE_REVISED','revised reference unresolved');
    select pg_temp.require((public.market_evaluate_forecast_v1(1,6,'SOURCE_REVISED','')->>'status')='SOURCE_REVISED','revised target unresolved');
    reset role;
    do $$ declare rejected boolean:=false; begin begin update public.market_forecasts set direction='DOWN' where id=1; exception when others then rejected:=sqlerrm like 'Append-only research record%'; end; perform pg_temp.require(rejected,'even table owner UPDATE blocked'); end $$;
    do $$ declare rejected boolean:=false; begin begin delete from public.market_forecast_evaluations; exception when others then rejected:=sqlerrm like 'Append-only research record%'; end; perform pg_temp.require(rejected,'even table owner DELETE blocked'); end $$;
    set role service_role;
    """
    proto={'study_id':'critic-first','event_key':'protocol_registered','kind':'protocol_registered','payload':{'instrument':'SPY','holdout_sessions':['2026-11-05','2026-11-06']}}
    sql+='select public.market_experiment_event_v1('+q(proto)+');\n'
    proto2=json.loads(json.dumps(proto)); proto2['study_id']='critic-alias'; proto2['payload']['dataset_id']='different-name'
    sql+="do $$ declare rejected boolean:=false; begin begin perform public.market_experiment_event_v1("+q(proto2)+"); exception when unique_violation then rejected:=true; end; perform pg_temp.require(rejected,'overlapping holdout reserved across alias studies'); end $$;\n"
    for event in [dict(study_id='critic-first',event_key='trial_result:C',kind='trial_result',payload={'candidate_id':'C'}),dict(study_id='critic-first',event_key='holdout_result',kind='holdout_result',payload={})]:
        sql+="do $$ declare rejected boolean:=false; begin begin perform public.market_experiment_event_v1("+q(event)+"); exception when others then rejected:=true; end; perform pg_temp.require(rejected,'unregistered trial or unfrozen holdout rejected'); end $$;\n"
    sql+="reset role;\nselect 'CRITIC_SQL_CHECKS_PASSED' as result;\n"
    
    return sql


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isolated-local-only", action="store_true", required=True)
    parser.add_argument("--postgres-bin", type=Path,
                        help="Directory containing local PostgreSQL executables")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    bins = args.postgres_bin
    if bins is None:
        installed = shutil.which("initdb")
        bins = Path(installed).parent if installed else Path("/opt/homebrew/opt/postgresql@16/bin")
    for executable in ("initdb", "pg_ctl", "psql"):
        if not (bins / executable).is_file():
            parser.error("Local PostgreSQL executable missing: " + str(bins / executable))

    def run(command, **kwargs):
        process = subprocess.run(command, text=True, capture_output=True, **kwargs)
        if process.returncode:
            raise RuntimeError(process.stdout + process.stderr)
        return process

    with tempfile.TemporaryDirectory(prefix="signal-forecast-check-") as folder:
        root = Path(folder)
        data = root / "data"
        run([str(bins / "initdb"), "-D", str(data), "-A", "trust", "-U", "postgres"])
        started = False
        try:
            run([str(bins / "pg_ctl"), "-D", str(data), "-l", str(root / "server.log"),
                 "-o", "-k " + shlex.quote(folder) + " -h '' -p 55439", "start"])
            started = True
            psql = [str(bins / "psql"), "-X", "-q", "-h", folder, "-p", "55439",
                    "-U", "postgres", "-v", "ON_ERROR_STOP=1"]
            run(psql + ["-c", "create role anon; create role authenticated; "
                        "create role service_role bypassrls; create schema private; "
                        "grant usage on schema private to service_role;"])
            run(psql + ["-f", str(repo / "db/market_forecasts_v5.sql")])
            run(psql + ["-f", str(repo / "db/market_forecast_close_availability_v6.sql")])
            result = run(psql, input=build_checks(repo))
            if "CRITIC_SQL_CHECKS_PASSED" not in result.stdout:
                raise RuntimeError("Local checks did not reach their final assertion")
            print("Local PostgreSQL invariants passed: synthetic fixtures and local clock injection only.")
        finally:
            if started:
                run([str(bins / "pg_ctl"), "-D", str(data), "-m", "fast", "stop"])


if __name__ == "__main__":
    main()
