-- Deployed as market_descriptive_watch_engine_v1.
-- Read-only daily-market confirmations, not pre-crash warnings or investment orders.
-- Runs independently of geopolitical collection or private trade decisions.
create table if not exists public.market_watch_signals (
 id bigint generated always as identity primary key,
 observation_id bigint not null references public.market_observations(id),
 rule_id text not null check (rule_id in ('vxn_5day_spike','vix_5day_spike','treasury_10y_rate_spike','vxn_volatility_easing','vix_volatility_easing')),
 observed_session date not null,
 source_first_seen_at timestamptz not null,
 first_detected_at timestamptz not null default now(),
 observed_value numeric not null,
 threshold numeric not null,
 unit text not null,
 direction text not null check(direction in ('risk_up','risk_down')),
 signal_type text not null default 'market_confirmation_only',
 limitations text not null,
 unique(observation_id,rule_id)
);
create index if not exists market_watch_signals_time on public.market_watch_signals(first_detected_at desc,id desc);
alter table public.market_watch_signals enable row level security;
revoke all on public.market_watch_signals from public,anon,authenticated,service_role;
grant select on public.market_watch_signals to service_role;

create table if not exists private.market_watch_checks (
 id bigint generated always as identity primary key,
 started_at timestamptz not null default now(),
 trigger_kind text not null check(trigger_kind in ('manual','cron')),
 expected_session date not null,
 status text not null check(status in ('completed','partial')),
 inspected_metric_count integer not null,
 new_signal_count integer not null,
 coverage_gaps text[] not null default '{}',
 details jsonb not null default '{}'::jsonb
);
alter table private.market_watch_checks enable row level security;
revoke all on private.market_watch_checks from public,anon,authenticated,service_role;
grant select on private.market_watch_checks to service_role;
grant usage on schema private to service_role;

create or replace function private.market_watch_evaluate_v1(p_trigger text default 'manual')
returns jsonb language plpgsql security invoker set search_path='' as $$
declare
 us_now timestamp := pg_catalog.timezone('America/New_York',now());
 expected date;
 o public.market_observations;
 rec public.market_watch_signals;
 examined integer := 0;
 created integer := 0;
 gaps text[] := '{}'::text[];
 cfg record;
 active boolean;
 output jsonb := '[]'::jsonb;
begin
 if p_trigger not in ('manual','cron') then
  raise exception 'Invalid evaluation trigger';
 end if;
 expected := us_now::date;
 if extract(isodow from expected) <= 5 and us_now::time < time '16:15' then
  expected := expected - 1;
 end if;
 while extract(isodow from expected) > 5 loop
  expected := expected - 1;
 end loop;
 for cfg in
  select * from (values
   ('vxn_5day_spike','vxn_5session_change_pct',20::numeric,true,'Cboe Global Markets'),
   ('vix_5day_spike','vix_5session_change_pct',20::numeric,true,'Cboe Global Markets'),
   ('treasury_10y_rate_spike','us_treasury_10y_5session_delta_bp',30::numeric,true,'U.S. Department of the Treasury'),
   ('vxn_volatility_easing','vxn_5session_change_pct',-20::numeric,false,'Cboe Global Markets'),
   ('vix_volatility_easing','vix_5session_change_pct',-20::numeric,false,'Cboe Global Markets')
  ) as rules(rule_id,metric_code,threshold,risk_increase,publisher)
 loop
  select * into o from public.market_observations
  where metric_code=cfg.metric_code and publisher=cfg.publisher
    and claim_status='primary_document'
    and event_at is not null and collected_at <= now()
  order by event_at desc, collected_at desc, id desc limit 1;
  if not found then
   gaps:=array_append(gaps,cfg.metric_code || ':not_collected');
   continue;
  end if;
  if o.event_at::date < expected then
   gaps:=array_append(gaps,cfg.metric_code || ':last_session_stale');
   continue;
  end if;
  examined:=examined+1;
  active:=case when cfg.risk_increase then o.metric_value>=cfg.threshold else o.metric_value<=cfg.threshold end;
  if not active then continue; end if;
  insert into public.market_watch_signals(
    observation_id,rule_id,observed_session,source_first_seen_at,
    observed_value,threshold,unit,direction,limitations
  ) values (
    o.id,cfg.rule_id,o.event_at::date,o.collected_at,
    o.metric_value,cfg.threshold,coalesce(o.metric_unit,'unknown'),
    case when cfg.risk_increase then 'risk_up' else 'risk_down' end,
    'Single measured daily reference signal, detected at first_detected_at. Confirmation/attention heuristic only, NOT advance prediction, tradable quote, probability, or buy/sell advice.'
  ) on conflict (observation_id,rule_id) do nothing returning * into rec;
  if found then
   created:=created+1;
   insert into public.audit_events(actor,action,entity_type,entity_id)
   values('market-watch-sql','record_descriptive_signal','market_watch_signal',rec.id);
   output:=output||pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object('id',rec.id,'rule',rec.rule_id,'observation_id',rec.observation_id,'direction',rec.direction));
  end if;
 end loop;
 insert into private.market_watch_checks(trigger_kind,expected_session,status,inspected_metric_count,new_signal_count,coverage_gaps,details)
 values(p_trigger,expected,case when cardinality(gaps)>0 then 'partial' else 'completed' end,
  examined,created,gaps,
  pg_catalog.jsonb_build_object('signal_kind','market_confirmation_only','orders_placed',false));
 return pg_catalog.jsonb_build_object('trigger',p_trigger,'expected_session',expected,
  'inspected_metrics',examined,'new_signals',created,'coverage_gaps',to_jsonb(gaps),
  'signals',output,'orders_placed',false);
end;
$$;
revoke all on function private.market_watch_evaluate_v1(text) from public,anon,authenticated,service_role;
-- Registered as active independent cron after the reference feeds (at minute :15).
select cron.schedule('market-evidence-watch','25,50 0,13,23 * * *', $job$select private.market_watch_evaluate_v1('cron');$job$);
