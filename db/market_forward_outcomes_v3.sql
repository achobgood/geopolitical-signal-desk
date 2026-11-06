-- Forward-observed five-session movement of VIX/VXN volatility indices.
-- This is NOT stock-trading profit, a signal win rate, or a simulated order.
create table if not exists public.market_technical_outcomes(
 id bigint generated always as identity primary key,
 baseline_snapshot_id bigint not null references public.market_technical_snapshots(id),
 future_snapshot_id bigint not null references public.market_technical_snapshots(id),
 symbol text not null check(symbol in ('VIX','VXN')),
 horizon_sessions integer not null default 5 check(horizon_sessions=5),
 baseline_session date not null,
 target_session date not null,
 baseline_close numeric not null check(baseline_close>0),
 target_close numeric not null check(target_close>0),
 observed_index_change_pct numeric not null,
 first_evaluated_at timestamptz not null default now(),
 outcome_label text not null default 'observed_volatility_index_change_not_investment_return',
 method_version text not null default 'forward-five-completed-source-sessions-v1',
 unique(baseline_snapshot_id,horizon_sessions),
 check(target_session>baseline_session)
);
create index if not exists market_technical_outcomes_recent on public.market_technical_outcomes(first_evaluated_at desc,id desc);
alter table public.market_technical_outcomes enable row level security;
revoke all on public.market_technical_outcomes from public,anon,authenticated,service_role;
grant select,insert on public.market_technical_outcomes to service_role;
grant usage,select on sequence public.market_technical_outcomes_id_seq to service_role;

create or replace function public.market_record_technical_outcome_v1(p_baseline_id bigint,p_future_id bigint)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare
 v_base public.market_technical_snapshots;
 v_future public.market_technical_snapshots;
 v_first jsonb;
 v_target jsonb;
 v_after int := 0;
 v_close numeric;
 v_new public.market_technical_outcomes;
 v_created boolean := false;
begin
 if p_baseline_id is null or p_future_id is null or p_baseline_id=p_future_id then
  raise exception 'Invalid snapshot pair';
 end if;
 select * into v_base from public.market_technical_snapshots where id=p_baseline_id;
 if not found then raise exception 'Unknown baseline snapshot'; end if;
 select * into v_future from public.market_technical_snapshots where id=p_future_id;
 if not found then raise exception 'Unknown future snapshot'; end if;
 if v_future.provider<>v_base.provider or v_future.symbol<>v_base.symbol
    or v_future.observed_session<=v_base.observed_session
    or v_future.calculated_at<=v_base.calculated_at
    or v_future.captured_at<=v_base.captured_at
    or v_future.source_observation_id=v_base.source_observation_id then
  raise exception 'Not a later independent official source snapshot';
 end if;
 select e.value into v_first
 from jsonb_array_elements(v_future.input_bars) e(value)
 where (e.value->>'session')::date=v_base.observed_session
 limit 1;
 v_close:=(v_base.analysis->>'close')::numeric;
 if v_first is null or v_close is null
    or (v_first->>'close')::numeric is distinct from v_close then
  return jsonb_build_object('status','INCOMPARABLE_REVISED_SOURCE','created',false);
 end if;
 select e.value into v_target
 from jsonb_array_elements(v_future.input_bars) e(value)
 where (e.value->>'session')::date>v_base.observed_session
 order by (e.value->>'session')::date asc
 limit 1 offset 4;
 if v_target is null then
  return jsonb_build_object('status','PENDING_FUTURE_SESSIONS','created',false);
 end if;
 if (v_target->>'close')::numeric<=0 or v_close<=0 then
  raise exception 'Nonpositive official index level';
 end if;
 insert into public.market_technical_outcomes(
  baseline_snapshot_id,future_snapshot_id,symbol,horizon_sessions,
  baseline_session,target_session,baseline_close,target_close,
  observed_index_change_pct
 ) values (
  v_base.id,v_future.id,v_base.symbol,5,v_base.observed_session,
  (v_target->>'session')::date,v_close,(v_target->>'close')::numeric,
  round(((v_target->>'close')::numeric/v_close-1)*100,4)
 )
 on conflict (baseline_snapshot_id,horizon_sessions) do nothing returning * into v_new;
 if found then
  v_created:=true;
  insert into public.audit_events(actor,action,entity_type,entity_id)
  values('collector:market_forward_eval','record_observed_index_change','market_technical_outcome',v_new.id);
 else
  select * into v_new from public.market_technical_outcomes
  where baseline_snapshot_id=v_base.id and horizon_sessions=5;
 end if;
 return jsonb_build_object(
  'status','OBSERVED_NOT_TRADING_RETURN','created',v_created,
  'outcome_id',v_new.id,'baseline_session',v_new.baseline_session,
  'target_session',v_new.target_session,'observed_index_change_pct',v_new.observed_index_change_pct,
  'not_a_trade_pnl',true
 );
end;
$$;
revoke all on function public.market_record_technical_outcome_v1(bigint,bigint) from public,anon,authenticated;
grant execute on function public.market_record_technical_outcome_v1(bigint,bigint) to service_role;
notify pgrst,'reload schema';
