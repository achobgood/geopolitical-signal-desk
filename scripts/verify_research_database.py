"""Independent v7–v9 role, key-rotation, immutability, and rolling quota checks.

Usage: python scripts/verify_research_database.py --isolated-local-only

Wraps the existing disposable local PostgreSQL harness; no production host or
connection string is accepted. All credentials and values are synthetic. A fixed
clock is injected only into the disposable quota function. No CI gate is added.
"""
import hashlib,importlib.util,json,re,sys
from datetime import datetime,timezone
from pathlib import Path
repo=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('forecast_harness',repo/'scripts/verify_forecast_database.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
base=module.build_checks

def q(value):return "'"+json.dumps(value).replace("'","''")+"'::jsonb"

def checks(repo):
    migration=(repo/'db/market_research_v7.sql').read_text()
    calendar_migration=(repo/'db/market_catalyst_calendar_v9.sql').read_text()
    sql=base(repo)+migration+'\n'+(repo/'db/market_research_monitoring_v8.sql').read_text()+'\n'+calendar_migration+'\n'
    claim=re.search(r'create or replace function private.market_macro_claim_v1\(p_provider text,p_force boolean\).*?end \$\$;',calendar_migration,re.S).group(0)
    claim=claim.replace('n timestamptz:=clock_timestamp()',"n timestamptz:='2026-11-06T00:30:00Z'::timestamptz")
    sql+=claim+'\n'
    sql+="""
select pg_temp.require((select count(*)=8 from pg_class c join pg_namespace n on n.oid=c.relnamespace where (n.nspname,c.relname)in(('public','market_macro_captures'),('private','market_macro_checks'),('private','market_macro_check_results'),('public','market_risk_briefs'),('private','market_finance_keys'),('private','market_portfolio_snapshots'),('private','market_finance_research_reports'),('private','market_portfolio_analyses'))and c.relrowsecurity),'All eight new tables enforce RLS');
select pg_temp.require(not has_table_privilege('service_role','private.market_portfolio_snapshots','select') and not has_table_privilege('service_role','private.market_finance_keys','select'),'Direct private table reads unavailable to service role');
select pg_temp.require(not has_function_privilege('anon','public.market_portfolio_latest_v1()','execute')and not has_function_privilege('authenticated','public.market_finance_authorize_v1(text)','execute'),'Public and authenticated roles cannot call private finance RPCs');
set role service_role;
do $$ declare denied boolean:=false; begin begin perform * from private.market_portfolio_snapshots; exception when insufficient_privilege then denied:=true;end;perform pg_temp.require(denied,'Actual direct private table SELECT denied');end $$;
select pg_temp.require((public.market_finance_authorize_v1(repeat('a',64))->>'configured')='false','Finance is fail-closed when key not configured');
reset role;
-- The disposable quota function uses a fixed00:30UTC clock. Eight attempts
-- all fall on the previous UTC date, yet are
-- inside the rolling24h window. The previous calendar-day implementation would
-- count zero of them and allow another claim.
insert into private.market_macro_checks(provider,started_at)select 'bls_employment','2026-11-06T00:30:00Z'::timestamptz-make_interval(hours=>g)from generate_series(16,23)g;
insert into private.market_macro_check_results(check_id,status)select id,'CAPTURED'from private.market_macro_checks where provider='bls_employment';
set role service_role;
select pg_temp.require((public.market_macro_claim_v1('bls_employment',true)->>'daily_bound_reached')='true','Eight rolling24h BLS claims block a ninth across UTC midnight');
reset role;
"""
    keyhash=hashlib.sha256(b'local-synthetic-finance-key-not-live').hexdigest();newhash=hashlib.sha256(b'rotated-local-synthetic-key-not-live').hexdigest()
    sql+="insert into private.market_finance_keys(name,key_sha256)values('owner_finance','"+keyhash+"');\nset role service_role;\n"
    sql+="select pg_temp.require((public.market_finance_authorize_v1('"+keyhash+"')->>'valid')='true'and(public.market_finance_authorize_v1(repeat('f',64))->>'valid')='false','Only dedicated finance hash authorizes');\nreset role;\n"
    sql+="update private.market_finance_keys set key_sha256='"+newhash+"'where name='owner_finance';\nset role service_role;\n"
    sql+="select pg_temp.require((public.market_finance_authorize_v1('"+keyhash+"')->>'valid')='false'and(public.market_finance_authorize_v1('"+newhash+"')->>'valid')='true','Admin rotation invalidates old finance key');\n"
    portfolio={'as_of':datetime.now(timezone.utc).isoformat(),'source':'Synthetic local critic fixture only','currency':'USD','complete':True,'cash':'17','positions':[]}
    sql+="select pg_temp.require((public.market_portfolio_save_v1("+q(portfolio)+",'critic-private-001')->'portfolio'->>'cash')='17','Private save and read receipt succeeds');\n"
    sql+="select pg_temp.require((public.market_portfolio_latest_v1()->'portfolio'->>'source')='Synthetic local critic fixture only','Only private RPC reads local portfolio receipt');\n"
    portfolio['cash']='18'
    sql+="do $$declare denied boolean:=false;begin begin perform public.market_portfolio_save_v1("+q(portfolio)+",'critic-private-001');exception when others then denied:=sqlerrm='Immutable portfolio request conflict';end;perform pg_temp.require(denied,'Conflicting retry cannot rewrite saved balance');end $$;\nreset role;\n"
    sql+="""do $$declare denied boolean:=false;begin begin update private.market_portfolio_snapshots set payload='{}';exception when others then denied:=sqlerrm like 'Append-only research record%';end;perform pg_temp.require(denied,'Even owner cannot overwrite saved portfolio receipt');end $$;
set role anon;
do $$declare denied boolean:=false;begin begin perform public.market_portfolio_latest_v1();exception when insufficient_privilege then denied:=true;end;perform pg_temp.require(denied,'Actual anonymous finance RPC denied');end $$;
reset role;
select pg_temp.require(not has_function_privilege('anon','public.market_macro_health_v1()','execute'),'Anonymous source-health RPC denied');
set role service_role;
select pg_temp.require(jsonb_typeof(public.market_macro_health_v1())='array','Backend source check health is typed and bounded');
reset role;
do $$declare cid bigint;body jsonb;denied boolean:=false;begin
 cid:=(private.market_macro_claim_v1('bea_release_calendar',false)->>'check_id')::bigint;
 perform pg_temp.require(cid is not null,'New official calendar claim succeeds');
 body:=jsonb_build_object('provider','bea_release_calendar','source_as_of','2026-11-06','document_sha256',repeat('c',64),'raw_gzip_base64','AA==','source_urls',jsonb_build_array('https://evil.example/calendar'),'data',jsonb_build_object('scheduled_event_count',1),'events',jsonb_build_array(jsonb_build_object('event_id','local-only-fixture')));
 begin perform private.market_macro_capture_v1(body,cid);exception when others then denied:=sqlerrm='Unapproved macro origin';end;
 perform pg_temp.require(denied,'Unreviewed calendar origin rejected');
 body:=jsonb_set(body,'{source_urls}',jsonb_build_array('https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics'));
 perform pg_temp.require((private.market_macro_capture_v1(body,cid)->>'id')is not null,'Reviewed calendar persists through existing immutable boundary');
 insert into private.market_macro_checks(provider)values('bls_employment')returning id into cid;
 body:=jsonb_build_object('provider','bls_employment','source_as_of','2026-10','document_sha256',repeat('d',64),'raw_gzip_base64','AA==','source_urls',jsonb_build_array('https://api.bls.gov/publicAPI/v1/timeseries/data/'),'data','{}'::jsonb);
 denied:=false;begin perform private.market_macro_capture_v1(body,cid);exception when others then denied:=sqlerrm='Unapproved BLS series origin';end;
 perform pg_temp.require(denied,'BLS batch receipt without approved series IDs is rejected');
 body:=jsonb_set(body,'{requested_series_ids}',jsonb_build_array('CES0000000001','LNS14000000','CES0500000003'));
 perform pg_temp.require((private.market_macro_capture_v1(body,cid)->>'id')is not null,'BLS batch with exact reviewed series accepted');
end $$;
select 'INDEPENDENT_RESEARCH_PRIVACY_DATABASE_PASSED';
"""
    return sql
if __name__=='__main__':
    module.build_checks=checks
    module.main()
    print('Independent v7–v9 local database: RLS/private privilege isolation/key rotation/portfolio immutability/rolling BLS quota/calendar origin/BLS batch identity checks passed.')
