-- Source-linked deterministic technical snapshots, no brokerage orders.
-- Public API may publish sanitized index research only. Raw historical bars remain server-only.
create table if not exists public.market_technical_snapshots (
 id bigint generated always as identity primary key,
 request_key text not null unique,
 provider text not null check(provider in ('cboe_vix','cboe_vxn')),
 symbol text not null check(symbol in ('VIX','VXN')),
 instrument_kind text not null check(instrument_kind='volatility_index'),
 source_observation_id bigint not null references public.market_observations(id),
 source_url text not null,
 observed_session date not null,
 captured_at timestamptz not null,
 calculated_at timestamptz not null default now(),
 method_version text not null check(method_version='cross-timeframe-pivots-wilder-v1'),
 bar_count integer not null check(bar_count between 60 and 320),
 bars_sha256 text not null check(length(bars_sha256)=64),
 analysis jsonb not null,
 input_bars jsonb not null,
 check(jsonb_typeof(analysis)='object'),
 check(jsonb_typeof(input_bars)='array'),
 check(jsonb_array_length(input_bars)=bar_count),
 check((analysis->>'trade_ticket_approved')::boolean=false),
 check((analysis->>'last_completed_session')::date=observed_session),
 check((analysis->>'bars_sha256')=bars_sha256)
);
create index if not exists market_technicals_recent on public.market_technical_snapshots(calculated_at desc,id desc);
create index if not exists market_technicals_symbol on public.market_technical_snapshots(symbol,observed_session desc,id desc);
alter table public.market_technical_snapshots enable row level security;
revoke all on public.market_technical_snapshots from public,anon,authenticated,service_role;
grant select,insert on public.market_technical_snapshots to service_role;
grant usage,select on sequence public.market_technical_snapshots_id_seq to service_role;

create or replace function public.market_record_technical_v1(payload jsonb,p_key text)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare
 v_source public.market_observations;
 v_rec public.market_technical_snapshots;
 v_captured timestamptz := nullif(payload->>'captured_at','')::timestamptz;
 v_provider text := payload->>'provider';
 v_symbol text := payload->>'symbol';
 v_session date := nullif(payload->>'observed_session','')::date;
 v_analysis jsonb := payload->'analysis';
 v_bars jsonb := payload->'input_bars';
 v_created boolean := false;
begin
 if length(coalesce(p_key,''))<>64
    or v_provider not in ('cboe_vix','cboe_vxn')
    or (v_provider='cboe_vix' and v_symbol <> 'VIX')
    or (v_provider='cboe_vxn' and v_symbol <> 'VXN')
    or payload->>'instrument_kind' <> 'volatility_index'
    or payload->>'method_version' <> 'cross-timeframe-pivots-wilder-v1'
    or v_captured is null or v_session is null
    or v_captured < now()-interval '10 minutes'
    or v_captured > now()+interval '3 minutes'
    or v_session > (v_captured at time zone 'America/New_York')::date
    or jsonb_typeof(v_bars)<>'array'
    or jsonb_array_length(v_bars) not between 60 and 320
    or jsonb_typeof(v_analysis)<>'object'
    or (v_analysis->>'trade_ticket_approved')::boolean is distinct from false
    or v_analysis->>'method_version'<>'cross-timeframe-pivots-wilder-v1'
    or v_analysis->>'symbol'<>v_symbol
    or v_analysis->>'instrument_kind'<>'volatility_index'
    or (v_analysis->>'last_completed_session')::date is distinct from v_session
    or v_analysis->>'bars_sha256' <> payload->>'bars_sha256'
    or (v_bars->-1->>'session')::date is distinct from v_session
    or coalesce((payload->>'bar_count')::integer,0) <> jsonb_array_length(v_bars)
    or length(coalesce(payload->>'bars_sha256',''))<>64
 then raise exception 'Invalid technical snapshot contract'; end if;
 select * into v_source from public.market_observations
 where id=(payload->>'source_observation_id')::bigint;
 if not found or v_source.publisher<>'Cboe Global Markets'
    or v_source.claim_status<>'primary_document'
    or v_source.metric_code <> lower(v_symbol)||'_daily_close'
    or v_source.event_at::date<>v_session
    or v_source.collected_at>v_captured
    or v_source.published_at>v_captured
    or v_source.metric_value is distinct from (v_analysis->>'close')::numeric
    or v_source.source_url<>payload->>'source_url'
 then raise exception 'Official Cboe closing observation must substantiate technical input'; end if;
 insert into public.market_technical_snapshots(
  request_key,provider,symbol,instrument_kind,source_observation_id,
  source_url,observed_session,captured_at,method_version,bar_count,
  bars_sha256,analysis,input_bars
 ) values (
  p_key,v_provider,v_symbol,'volatility_index',v_source.id,
  v_source.source_url,v_session,v_captured,payload->>'method_version',
  (payload->>'bar_count')::integer,payload->>'bars_sha256',v_analysis,v_bars
 )
 on conflict(request_key) do nothing returning * into v_rec;
 if found then
  v_created:=true;
  insert into public.audit_events(actor,action,entity_type,entity_id,request_key)
  values('collector:cboe_quant','record_index_technical','market_technical_snapshot',v_rec.id,p_key);
 else
  select * into v_rec from public.market_technical_snapshots where request_key=p_key;
 end if;
 return jsonb_build_object('created',v_created,'snapshot_id',v_rec.id,
   'source_observation_id',v_rec.source_observation_id,'trade_approved',false);
end;
$$;
revoke all on function public.market_record_technical_v1(jsonb,text) from public,anon,authenticated;
grant execute on function public.market_record_technical_v1(jsonb,text) to service_role;
notify pgrst,'reload schema';
