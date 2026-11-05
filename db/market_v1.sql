-- Market Intelligence Desk V1: parallel, additive domain. Never mix market records into geopolitical leads.
-- No user holdings, account identifiers, or trade execution fields belong in this schema.
create table if not exists public.market_observations (
  id bigint generated always as identity primary key,
  canonical_key text not null unique,
  source_url text not null,
  source_title text not null,
  publisher text not null,
  published_at timestamptz not null,
  event_at timestamptz,
  collected_at timestamptz not null default now(),
  family text not null check (family in ('ai_credit','ai_capex_returns','policy_siting','rates_liquidity','earnings_demand','market_breadth','valuation','macro','geopolitical','other')),
  phase text not null check (phase in ('structural','leading','catalyst','confirmation','counterevidence')),
  pressure text not null check (pressure in ('risk_up','risk_down','ambiguous')),
  claim_status text not null check (claim_status in ('primary_document','reported','unverified','disputed')),
  origin_group text not null,
  summary text not null,
  tickers text[] not null default '{}',
  metric_code text,
  metric_value numeric,
  metric_unit text,
  caveats text not null default '',
  check (length(origin_group) > 0),
  check (metric_value is null or metric_code is not null)
);
create index if not exists market_observations_published on public.market_observations(published_at desc, id desc);
create index if not exists market_observations_family on public.market_observations(family, id desc);
alter table public.market_observations enable row level security;
revoke all on public.market_observations from public, anon, authenticated;
grant select, insert on public.market_observations to service_role;
grant usage, select on sequence public.market_observations_id_seq to service_role;

create table if not exists public.market_assessments (
  id bigint generated always as identity primary key,
  request_key text not null unique,
  as_of timestamptz not null,
  mode text not null check (mode in ('live','replay')),
  horizon_sessions integer not null check (horizon_sessions between 1 and 60),
  scenario text not null check (scenario in ('further_downside','stabilization','rebound')),
  confidence text not null check (confidence in ('low','medium','high')),
  thesis text not null,
  countercase text not null,
  falsification text not null,
  next_check text not null,
  coverage_gaps text not null,
  evidence_ids bigint[] not null,
  created_at timestamptz not null default now(),
  check (cardinality(evidence_ids) between 1 and 20)
);
create index if not exists market_assessments_as_of on public.market_assessments(as_of desc, id desc);
alter table public.market_assessments enable row level security;
revoke all on public.market_assessments from public, anon, authenticated;
grant select, insert on public.market_assessments to service_role;
grant usage, select on sequence public.market_assessments_id_seq to service_role;

-- Atomic provenance-preserving record and immutable audit, with idempotent retries.
create or replace function public.market_record_observation_v1(payload jsonb, p_key text, p_actor text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  v_record public.market_observations;
  v_created boolean := false;
begin
  if coalesce(payload->>'source_url','') = ''
     or coalesce(payload->>'source_title','') = ''
     or coalesce(payload->>'summary','') = ''
     or coalesce(payload->>'origin_group','') = ''
     or coalesce(p_key,'') = '' then
    raise exception 'Incomplete public market observation';
  end if;
  insert into public.market_observations(
    canonical_key, source_url, source_title, publisher, published_at, event_at,
    family, phase, pressure, claim_status, origin_group, summary, tickers,
    metric_code, metric_value, metric_unit, caveats
  ) values (
    p_key, payload->>'source_url', payload->>'source_title', payload->>'publisher',
    (payload->>'published_at')::timestamptz,
    nullif(payload->>'event_at','')::timestamptz,
    payload->>'family', payload->>'phase', payload->>'pressure',
    payload->>'claim_status', payload->>'origin_group', payload->>'summary',
    coalesce(array(select jsonb_array_elements_text(payload->'tickers')), '{}'::text[]),
    nullif(payload->>'metric_code',''), nullif(payload->>'metric_value','')::numeric,
    nullif(payload->>'metric_unit',''), coalesce(payload->>'caveats','')
  )
  on conflict (canonical_key) do nothing returning * into v_record;
  if found then
    v_created := true;
    insert into public.audit_events(actor, action, entity_type, entity_id, request_key)
    values (p_actor, 'record_market_observation', 'market_observation', v_record.id, p_key);
  else
    select * into v_record from public.market_observations where canonical_key = p_key;
  end if;
  return jsonb_build_object('created',v_created,'observation',to_jsonb(v_record));
end;
$$;
revoke all on function public.market_record_observation_v1(jsonb,text,text) from public, anon, authenticated;
grant execute on function public.market_record_observation_v1(jsonb,text,text) to service_role;

-- Enforce point-in-time integrity IN DATABASE, not only in application code.
-- Historical replay: source publication must predate cut-off, but the record may have
-- been collected later. It must never be labeled an actual previously issued alert.
-- Live review: both original publication AND first actual collection must predate cut-off.
create or replace function public.market_record_assessment_v1(payload jsonb, p_key text, p_actor text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  v_record public.market_assessments;
  v_ids bigint[];
  v_as_of timestamptz := (payload->>'as_of')::timestamptz;
  v_mode text := payload->>'mode';
  v_created boolean := false;
begin
  select coalesce(array_agg(x.value::bigint), '{}'::bigint[]) into v_ids
  from jsonb_array_elements_text(coalesce(payload->'evidence_ids','[]'::jsonb)) as x(value);
  if coalesce(p_key,'') = '' or cardinality(v_ids) < 1 or cardinality(v_ids) > 20
     or (select count(distinct x.value) from unnest(v_ids) as x(value)) <> cardinality(v_ids)
     or v_as_of > now() + interval '5 minutes'
     or v_mode not in ('live','replay') then
    raise exception 'Invalid market assessment';
  end if;
  if (select count(*) from public.market_observations where id = any(v_ids)) <> cardinality(v_ids)
     or exists (
       select 1 from public.market_observations
       where id = any(v_ids)
         and (published_at > v_as_of or (v_mode = 'live' and collected_at > v_as_of))
     ) then
    raise exception 'Assessment contains unavailable or future evidence';
  end if;
  insert into public.market_assessments(
    request_key, as_of, mode, horizon_sessions, scenario, confidence,
    thesis, countercase, falsification, next_check, coverage_gaps, evidence_ids
  ) values (
    p_key, v_as_of, v_mode, (payload->>'horizon_sessions')::integer,
    payload->>'scenario', payload->>'confidence', payload->>'thesis',
    payload->>'countercase', payload->>'falsification',
    payload->>'next_check', payload->>'coverage_gaps', v_ids
  )
  on conflict (request_key) do nothing returning * into v_record;
  if found then
    v_created := true;
    insert into public.audit_events(actor, action, entity_type, entity_id, request_key)
    values (p_actor, 'record_market_assessment', 'market_assessment', v_record.id, p_key);
  else
    select * into v_record from public.market_assessments where request_key = p_key;
  end if;
  return jsonb_build_object('created',v_created,'assessment',to_jsonb(v_record));
end;
$$;
revoke all on function public.market_record_assessment_v1(jsonb,text,text) from public, anon, authenticated;
grant execute on function public.market_record_assessment_v1(jsonb,text,text) to service_role;

-- No public/anonymous SQL access: only the server can read and sanitize market data.
