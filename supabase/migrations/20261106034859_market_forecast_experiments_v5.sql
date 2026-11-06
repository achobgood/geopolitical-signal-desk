-- Prospective next-session research. All live dates originate in PostgreSQL.
-- Anonymous and authenticated Supabase clients have no direct table/RPC access.
create table public.market_forecast_prices (
 id bigint generated always as identity primary key,
 symbol text not null check(symbol in ('SPY','QQQ')),
 provider text not null check(provider='alpaca'), feed text not null check(feed='sip'),
 adjustment text not null check(adjustment='split'),
 source_url text not null, captured_at timestamptz not null default clock_timestamp(),
 observed_session date not null, bars_sha256 text not null check(bars_sha256 ~ '^[0-9a-f]{64}$'),
 input_bars jsonb not null, quality jsonb not null,
 unique(symbol,bars_sha256)
);
create table public.market_forecasts (
 id bigint generated always as identity primary key,
 symbol text not null check(symbol in ('SPY','QQQ')),
 issued_at timestamptz not null default clock_timestamp(),
 target_session date not null, reference_session date not null, cutoff_at timestamptz not null,
 reference_capture_id bigint references public.market_forecast_prices(id),
 reference_close numeric check(reference_close>0),
 model_version text not null check(model_version='momentum-1-research-v1'), code_hash text not null check(code_hash ~ '^[0-9a-f]{64}$'),
 config_hash text not null check(config_hash ~ '^[0-9a-f]{64}$'), configuration jsonb not null,
 direction text not null check(direction in ('UP','DOWN','ABSTAIN')),
 probability numeric check(probability is null), features jsonb not null,
 benchmarks jsonb not null, abstention_reasons jsonb not null,
 supporting_evidence jsonb not null, counterevidence jsonb not null,
 missing_inputs jsonb not null, invalidation jsonb not null,
 lock_status text not null default 'LOCKED_BEFORE_TARGET',
 research_only boolean not null default true check(research_only),
 selected_strategy boolean not null default false check(not selected_strategy),
 unique(symbol,target_session,model_version),
 check(issued_at<=cutoff_at and reference_session<target_session),
 check(direction='ABSTAIN' or (reference_close is not null and reference_capture_id is not null))
);
create table public.market_forecast_evaluations (
 id bigint generated always as identity primary key,
 forecast_id bigint not null references public.market_forecasts(id),
 capture_id bigint references public.market_forecast_prices(id),
 evaluated_at timestamptz not null default clock_timestamp(),
 status text not null check(status in ('OBSERVED','SOURCE_REVISED','MISSING_PRICE','UNSCORABLE_ABSTENTION','AWAITING_CLOSE')),
 reason text not null default '', target_close numeric, actual_direction text check(actual_direction in ('UP','DOWN','FLAT')),
 price_return numeric, evidence_key text not null,
 unique(forecast_id,evidence_key),
 check(status<>'OBSERVED' or (target_close>0 and actual_direction is not null and capture_id is not null))
);
create table public.market_forecast_runs (
 id bigint generated always as identity primary key, run_key text not null unique,
 trigger_kind text not null check(trigger_kind in ('manual','supabase_cron')),
 started_at timestamptz not null default clock_timestamp()
);
create table public.market_forecast_run_receipts (
 id bigint generated always as identity primary key,
 run_id bigint not null unique references public.market_forecast_runs(id),
 finished_at timestamptz not null default clock_timestamp(), details jsonb not null
);
create table public.market_strategy_experiments (
 id bigint generated always as identity primary key,
 recorded_at timestamptz not null default clock_timestamp(), study_id text not null,
 event_key text not null, kind text not null,
 payload jsonb not null, unique(study_id,event_key)
);
-- A final holdout session can be opened once for a given instrument, irrespective
-- of renamed datasets/aliases/configurations. Do not grant direct INSERT.
create table public.market_strategy_holdout_reservations (
 id bigint generated always as identity primary key,
 instrument text not null, session date not null, study_id text not null,
 unique(instrument,session)
);
create index on public.market_forecasts(target_session,id);
create index on public.market_forecast_evaluations(forecast_id,id desc);
create index on public.market_strategy_experiments(study_id,id);

create function public.market_forecast_immutable_v1() returns trigger language plpgsql security invoker set search_path='' as $$
begin raise exception 'Append-only research record: mutation forbidden'; end $$;
revoke all on function public.market_forecast_immutable_v1() from public,anon,authenticated;
do $$ declare t text; begin
 foreach t in array array['market_forecast_prices','market_forecasts','market_forecast_evaluations','market_forecast_runs','market_forecast_run_receipts','market_strategy_experiments','market_strategy_holdout_reservations'] loop
  execute format('alter table public.%I enable row level security',t);
  execute format('revoke all on public.%I from public,anon,authenticated,service_role',t);
  execute format('grant select on public.%I to service_role',t);
  execute format('grant usage,select on sequence public.%I to service_role',t||'_id_seq');
  execute format('create trigger immutable_record before update or delete on public.%I for each row execute function public.market_forecast_immutable_v1()',t);
 end loop;
end $$;

-- NYSE official 2026–2028 calendar. Renew before 2029, never guess new dates.
create function public.market_is_session_v1(d date) returns boolean language sql immutable security invoker set search_path='' as $$
 select extract(year from d) between 2026 and 2028 and extract(isodow from d)<6 and d <> all(array[
 '2026-01-01','2026-01-19','2026-02-16','2026-04-03','2026-05-25','2026-06-19','2026-07-03','2026-09-07','2026-11-26','2026-12-25',
 '2027-01-01','2027-01-18','2027-02-15','2027-03-26','2027-05-31','2027-06-18','2027-07-05','2027-09-06','2027-11-25','2027-12-24',
 '2028-01-17','2028-02-21','2028-04-14','2028-05-29','2028-06-19','2028-07-04','2028-09-04','2028-11-23','2028-12-25']::date[])
$$;
create function public.market_next_session_v1(d date) returns date language plpgsql immutable security invoker set search_path='' as $$
declare n date; begin
 for i in 1..11 loop
  n:=d+i;
  if public.market_is_session_v1(n) then return n; end if;
 end loop;
 raise exception 'No next session in verified NYSE 2026–2028 calendar';
end $$;

create function private.market_capture_forecast_prices_v1(p jsonb) returns jsonb language plpgsql security definer set search_path='' as $$
declare rec public.market_forecast_prices; b jsonb; prev date; ds date; last_session date; begin
 if p->>'symbol' not in ('SPY','QQQ') or p->>'provider'<>'alpaca' or p->>'feed'<>'sip' or p->>'adjustment'<>'split'
    or p->>'source_url'<>'https://data.alpaca.markets/v2/stocks/'||(p->>'symbol')||'/bars'
    or jsonb_typeof(p->'input_bars')<>'array' or jsonb_array_length(p->'input_bars') not between 60 and 1500 then
  raise exception 'Invalid consolidated price provenance';
 end if;
 for b in select value from jsonb_array_elements(p->'input_bars') loop
  if not (b ?& array['session','open','high','low','close']) or
     b->>'session' is null or b->>'open' is null or b->>'high' is null or b->>'low' is null or b->>'close' is null
     or (b->>'open') !~ '^[0-9]+([.][0-9]+)?$' or (b->>'high') !~ '^[0-9]+([.][0-9]+)?$'
     or (b->>'low') !~ '^[0-9]+([.][0-9]+)?$' or (b->>'close') !~ '^[0-9]+([.][0-9]+)?$' then raise exception 'Missing/non-numeric OHLC/session'; end if;
  ds:=(b->>'session')::date;
  if extract(year from ds) between 2026 and 2028 and not public.market_is_session_v1(ds) then raise exception 'Exchange holiday bar'; end if;
  if prev is not null and extract(year from prev) between 2026 and 2028 and public.market_next_session_v1(prev)<>ds then raise exception 'Missing completed exchange session'; end if;
  if prev is not null and ds<=prev then raise exception 'Unordered/duplicate session'; end if;
  if extract(isodow from ds)>5 or (b->>'low')::numeric<=0 or (b->>'high')::numeric<(b->>'low')::numeric
   or (b->>'open')::numeric not between (b->>'low')::numeric and (b->>'high')::numeric
   or (b->>'close')::numeric not between (b->>'low')::numeric and (b->>'high')::numeric
   or (b->>'close')::numeric::text in ('NaN','Infinity','-Infinity') then raise exception 'Invalid daily OHLC'; end if;
  prev:=ds;
 end loop;
 last_session:=prev;
 if last_session >= (clock_timestamp() at time zone 'America/New_York')::date and
    (clock_timestamp() at time zone 'America/New_York')::time<time '16:15' then raise exception 'Incomplete current session'; end if;
 if last_session>(clock_timestamp() at time zone 'America/New_York')::date then raise exception 'Future source session'; end if;
 insert into public.market_forecast_prices(symbol,provider,feed,adjustment,source_url,observed_session,bars_sha256,input_bars,quality)
 values(p->>'symbol','alpaca','sip','split',p->>'source_url',last_session,p->>'bars_sha256',p->'input_bars',p->'quality')
 on conflict(symbol,bars_sha256) do nothing returning * into rec;
 if not found then select * into rec from public.market_forecast_prices where symbol=p->>'symbol' and bars_sha256=p->>'bars_sha256'; end if;
 return to_jsonb(rec);
end $$;
create function public.market_capture_forecast_prices_v1(p jsonb) returns jsonb language sql security invoker set search_path='' as $$ select private.market_capture_forecast_prices_v1(p) $$;

create function private.market_issue_forecast_v1(p jsonb) returns jsonb language plpgsql security definer set search_path='' as $$
declare t timestamptz:=clock_timestamp(); local_now timestamp; ref date; target date; cut timestamptz;
 prices public.market_forecast_prices; rec public.market_forecasts; base numeric; prior numeric; expected_direction text; begin
 local_now:=t at time zone 'America/New_York'; ref:=local_now::date;
 if not public.market_is_session_v1(ref) or local_now::time not between time '16:15' and time '19:00' then
  raise exception 'Outside prior trading day 16:15–19:00 ET issuance window; no backdating'; end if;
 target:=public.market_next_session_v1(ref); cut:=(ref+time '19:00') at time zone 'America/New_York';
 if (p->>'reference_session')::date<>ref or (p->>'target_session')::date<>target or p->>'model_version'<>'momentum-1-research-v1'
   or coalesce((p->>'selected_strategy')::boolean,true) or p->>'probability' is not null then raise exception 'Forecast target/model contract mismatch'; end if;
 if p->>'reference_capture_id' is not null then
  select * into strict prices from public.market_forecast_prices where id=(p->>'reference_capture_id')::bigint;
  if prices.symbol<>p->>'symbol' or prices.observed_session<>ref or prices.captured_at>t or prices.captured_at>cut then raise exception 'Unavailable/stale reference'; end if;
  base:=(prices.input_bars->-1->>'close')::numeric;
  prior:=(prices.input_bars->-2->>'close')::numeric;
  expected_direction:=case when base>prior then 'UP' when base<prior then 'DOWN' else 'ABSTAIN' end;
  if p->>'direction' is distinct from expected_direction or p->'benchmarks'->>'always_up' is distinct from 'UP' or p->'benchmarks'->>'momentum' is distinct from expected_direction
   or (p->'features'->>'source_price_capture_id')::bigint is distinct from prices.id
   or (p->'features'->>'usable_at')::timestamptz is distinct from prices.captured_at
   then raise exception 'Model direction/features do not reproduce the immutable price source'; end if;
 elsif p->>'direction'<>'ABSTAIN' or jsonb_array_length(p->'abstention_reasons')<1 then raise exception 'Missing price must abstain with reason'; end if;
 insert into public.market_forecasts(symbol,issued_at,target_session,reference_session,cutoff_at,reference_capture_id,reference_close,
 model_version,code_hash,config_hash,configuration,direction,probability,features,benchmarks,abstention_reasons,supporting_evidence,counterevidence,missing_inputs,invalidation)
 values(p->>'symbol',t,target,ref,cut,prices.id,base,p->>'model_version',p->>'code_hash',p->>'config_hash',p->'configuration',p->>'direction',null,
 p->'features',p->'benchmarks',p->'abstention_reasons',p->'supporting_evidence',p->'counterevidence',p->'missing_inputs',p->'invalidation')
 on conflict(symbol,target_session,model_version) do nothing returning * into rec;
 if not found then select * into rec from public.market_forecasts where symbol=p->>'symbol' and target_session=target and model_version=p->>'model_version'; end if;
 return to_jsonb(rec);
end $$;
create function public.market_issue_forecast_v1(p jsonb) returns jsonb language sql security invoker set search_path='' as $$ select private.market_issue_forecast_v1(p) $$;

create function public.market_pending_forecasts_v1() returns jsonb language sql stable security invoker set search_path='' as $$
 select coalesce(jsonb_agg(to_jsonb(f)),'[]'::jsonb) from (
 select f.*, (select e.target_close from public.market_forecast_evaluations e where e.forecast_id=f.id and e.status='OBSERVED' order by e.id limit 1) previous_target_close from public.market_forecasts f where
 clock_timestamp()>=(f.target_session+time '16:30') at time zone 'America/New_York'
 and not exists(select 1 from public.market_forecast_evaluations e where e.forecast_id=f.id and e.status='UNSCORABLE_ABSTENTION')
 and (f.target_session>=(clock_timestamp() at time zone 'America/New_York')::date-10
 or not exists(select 1 from public.market_forecast_evaluations e where e.forecast_id=f.id and e.status='OBSERVED'))
 order by f.id limit 100) f
$$;

create function private.market_evaluate_forecast_v1(p_forecast_id bigint,p_capture_id bigint,p_status text,p_reason text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare f public.market_forecasts; c public.market_forecast_prices; b jsonb; a jsonb; s text; reason text; target_close numeric; direction text; ret numeric; k text; rec public.market_forecast_evaluations; begin
 select * into strict f from public.market_forecasts where id=p_forecast_id;
 if clock_timestamp()<(f.target_session+time '16:30') at time zone 'America/New_York' then
  s:='AWAITING_CLOSE'; reason:='Target close and conservative publication buffer not reached';
 elsif f.reference_close is null then s:='UNSCORABLE_ABSTENTION'; reason:='Original reference price unavailable';
 elsif p_capture_id is null then s:='MISSING_PRICE'; reason:='Consolidated target price unavailable';
 else
  select * into strict c from public.market_forecast_prices where id=p_capture_id;
  if c.symbol<>f.symbol or c.captured_at<=f.issued_at then raise exception 'Outcome source identity/time mismatch'; end if;
  select value into b from jsonb_array_elements(c.input_bars) where value->>'session'=f.reference_session::text;
  select value into a from jsonb_array_elements(c.input_bars) where value->>'session'=f.target_session::text;
  if b is null or a is null then s:='MISSING_PRICE'; reason:='Exact target/reference session absent';
  elsif (b->>'close')::numeric<>f.reference_close then s:='SOURCE_REVISED'; reason:='Frozen reference close revised; unresolved';
  elsif exists(select 1 from public.market_forecast_evaluations e where e.forecast_id=f.id and e.status='OBSERVED' and e.target_close<>(a->>'close')::numeric) then
   s:='SOURCE_REVISED'; reason:='Previously observed target close revised; unresolved';
  else
   s:='OBSERVED'; reason:='Observed consolidated split-adjusted close-to-close price change, not trading return';
   target_close:=(a->>'close')::numeric; ret:=target_close/f.reference_close-1;
   direction:=case when target_close>f.reference_close then 'UP' when target_close<f.reference_close then 'DOWN' else 'FLAT' end;
  end if;
 end if;
 -- Supplied status is only a contract assertion, never a source for actual prices.
 if p_status<>s then raise exception 'Evaluator and database outcome disagree'; end if;
 k:=s||':'||coalesce(c.id::text,'none');
 insert into public.market_forecast_evaluations(forecast_id,capture_id,status,reason,target_close,actual_direction,price_return,evidence_key)
 values(f.id,c.id,s,reason,target_close,direction,ret,k) on conflict(forecast_id,evidence_key) do nothing returning * into rec;
 if not found then select * into rec from public.market_forecast_evaluations where forecast_id=f.id and evidence_key=k; end if;
 return to_jsonb(rec);
end $$;
create function public.market_evaluate_forecast_v1(p_forecast_id bigint,p_capture_id bigint,p_status text,p_reason text) returns jsonb language sql security invoker set search_path='' as $$ select private.market_evaluate_forecast_v1(p_forecast_id,p_capture_id,p_status,p_reason) $$;

create function private.market_forecast_run_begin_v1(p_key text,p_trigger text) returns jsonb language plpgsql security definer set search_path='' as $$
declare rec public.market_forecast_runs; begin
 if length(p_key) not between 8 and 160 then raise exception 'Invalid run key'; end if;
 insert into public.market_forecast_runs(run_key,trigger_kind) values(p_key,p_trigger) on conflict(run_key) do nothing returning * into rec;
 if found then return jsonb_build_object('id',rec.id,'created',true); end if;
 select * into strict rec from public.market_forecast_runs where run_key=p_key;
 return jsonb_build_object('id',rec.id,'created',false);
end $$;
create function public.market_forecast_run_begin_v1(p_key text,p_trigger text) returns jsonb language sql security invoker set search_path='' as $$ select private.market_forecast_run_begin_v1(p_key,p_trigger) $$;
create function private.market_forecast_run_finish_v1(p_id bigint,p_details jsonb) returns jsonb language plpgsql security definer set search_path='' as $$
declare rec public.market_forecast_run_receipts; begin
 insert into public.market_forecast_run_receipts(run_id,details) values(p_id,p_details) on conflict(run_id) do nothing returning * into rec;
 if not found then select * into rec from public.market_forecast_run_receipts where run_id=p_id; end if;
 return to_jsonb(rec);
end $$;
create function public.market_forecast_run_finish_v1(p_id bigint,p_details jsonb) returns jsonb language sql security invoker set search_path='' as $$ select private.market_forecast_run_finish_v1(p_id,p_details) $$;

create function public.market_public_forecasts_v1() returns jsonb language sql stable security invoker set search_path='' as $$
 select coalesce(jsonb_agg(x),'[]'::jsonb) from (
 select f.id,f.symbol,f.issued_at,f.target_session,f.reference_session,f.reference_close,f.cutoff_at,f.model_version,
 f.direction,f.probability,f.abstention_reasons,f.supporting_evidence,f.counterevidence,f.missing_inputs,f.invalidation,
 f.lock_status,f.research_only,f.selected_strategy,
 coalesce(e.status,'AWAITING_CLOSE') evaluation_status,e.actual_direction,e.target_close,e.evaluated_at
 from (select distinct on(symbol) * from public.market_forecasts order by symbol,target_session desc,id desc) f
 left join lateral(select * from public.market_forecast_evaluations where forecast_id=f.id order by id desc limit 1) e on true) x
$$;

create function private.market_experiment_event_v1(p jsonb) returns jsonb language plpgsql security definer set search_path='' as $$
declare rec public.market_strategy_experiments; proto jsonb; ds text; session_text text; instrument text; k text:=p->>'kind'; begin
 perform pg_advisory_xact_lock(hashtextextended(p->>'study_id',0));
 select * into rec from public.market_strategy_experiments where study_id=p->>'study_id' and event_key=p->>'event_key';
 if found then
  if rec.kind is distinct from k or rec.payload is distinct from p->'payload' then raise exception 'Immutable experiment event conflict'; end if;
  return to_jsonb(rec);
 end if;
 if k='protocol_registered' then
  if exists(select 1 from public.market_strategy_experiments where study_id=p->>'study_id') then raise exception 'Study already registered'; end if;
  proto:=p->'payload'; instrument:=proto->>'instrument';
  if jsonb_typeof(proto->'holdout_sessions') is distinct from 'array' then raise exception 'Holdout reservation absent'; end if;
  if instrument not in ('SPY','QQQ') or instrument is null or jsonb_array_length(proto->'holdout_sessions')<1 then raise exception 'Instrument and reserved holdout required'; end if;
  for session_text in select jsonb_array_elements_text(proto->'holdout_sessions') loop
   insert into public.market_strategy_holdout_reservations(instrument,session,study_id) values(instrument,session_text::date,p->>'study_id');
  end loop;
 elsif k<>'study_blocked' then
  select payload into strict proto from public.market_strategy_experiments where study_id=p->>'study_id' and kind='protocol_registered';
  if k='selection_frozen' and exists(select 1 from public.market_strategy_experiments where study_id=p->>'study_id' and kind='selection_frozen') then raise exception 'Selection already frozen'; end if;
  if k='trial_registered' and exists(select 1 from public.market_strategy_experiments where study_id=p->>'study_id' and kind='selection_frozen') then raise exception 'Trial after selection freeze'; end if;
  if k='trial_result' and not exists(select 1 from public.market_strategy_experiments where study_id=p->>'study_id' and kind='trial_registered' and payload->>'candidate_id'=p->'payload'->>'candidate_id') then raise exception 'Unregistered trial'; end if;
  if k='holdout_result' then
   if not exists(select 1 from public.market_strategy_experiments where study_id=p->>'study_id' and kind='selection_frozen')
     or exists(select 1 from public.market_strategy_experiments where study_id=p->>'study_id' and kind='holdout_result') then raise exception 'Holdout must be opened exactly once after selection freeze'; end if;
  end if;
 end if;
 if k not in ('study_blocked','protocol_registered','trial_registered','trial_result','selection_frozen','holdout_result','final_decision') then raise exception 'Unknown ledger event'; end if;
 insert into public.market_strategy_experiments(study_id,event_key,kind,payload) values(p->>'study_id',p->>'event_key',k,p->'payload') returning * into rec;
 return to_jsonb(rec);
end $$;
create function public.market_experiment_event_v1(p jsonb) returns jsonb language sql security invoker set search_path='' as $$ select private.market_experiment_event_v1(p) $$;

-- End users never call these functions. Server-only RPC boundary, least privilege.
do $$ declare f record; begin
 for f in select oid::regprocedure as sig from pg_proc where pronamespace='public'::regnamespace and proname in (
 'market_is_session_v1','market_next_session_v1','market_capture_forecast_prices_v1','market_issue_forecast_v1','market_pending_forecasts_v1',
 'market_evaluate_forecast_v1','market_forecast_run_begin_v1','market_forecast_run_finish_v1','market_public_forecasts_v1','market_experiment_event_v1') loop
 execute format('revoke all on function %s from public,anon,authenticated',f.sig);
 execute format('grant execute on function %s to service_role',f.sig);
 end loop;
end $$;
do $$ declare f record; begin
 for f in select oid::regprocedure as sig from pg_proc where pronamespace='private'::regnamespace and proname in (
 'market_capture_forecast_prices_v1','market_issue_forecast_v1','market_evaluate_forecast_v1',
 'market_forecast_run_begin_v1','market_forecast_run_finish_v1','market_experiment_event_v1') loop
 execute format('revoke all on function %s from public,anon,authenticated',f.sig);
 execute format('grant execute on function %s to service_role',f.sig);
 end loop;
end $$;
notify pgrst,'reload schema';
