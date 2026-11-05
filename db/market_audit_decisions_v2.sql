-- Audit repairs and private, immutable, owner-only trade decision history.
-- Uses the existing project and service-role only. No browser or public table grants.

create table if not exists public.market_source_amendments(
 id bigint generated always as identity primary key,
 observation_id bigint not null references public.market_observations(id),
 amendment_kind text not null check(amendment_kind in ('canonical_link','unavailable','attribution','retraction')),
 corrected_source_url text,
 rationale text not null,
 reviewed_at timestamptz not null default now(),
 reviewed_by text not null default 'owner_research',
 unique (observation_id,amendment_kind,corrected_source_url)
);
create index if not exists market_amendments_observation_idx on public.market_source_amendments(observation_id,id desc);
alter table public.market_source_amendments enable row level security;
revoke all on public.market_source_amendments from public, anon, authenticated, service_role;
grant select,insert on public.market_source_amendments to service_role;
grant usage,select on sequence public.market_source_amendments_id_seq to service_role;

-- Existing private schema grants are intentionally narrow. A secret key remains server-side.
grant usage on schema private to service_role;
create table if not exists private.market_trade_decisions (
 id bigint generated always as identity primary key,
 request_key text not null unique,
 issued_at timestamptz not null default now(),
 as_of timestamptz not null,
 status text not null check(status in ('NO_TRADE','PROPOSAL_ONLY')),
 regime text not null check(regime in ('unavailable','mixed','risk_off','risk_on')),
 method text not null check(method in ('rule_based','ai_researched')),
 tickets jsonb not null default '[]'::jsonb,
 evidence_ids bigint[] not null default '{}',
 blocked_reasons jsonb not null default '[]'::jsonb,
 input_provenance text not null,
 model_label text,
 note text not null default 'Unexecuted research proposal; not a brokerage order',
 check (jsonb_typeof(tickets)='array'),
 check (jsonb_typeof(blocked_reasons)='array'),
 check (status='PROPOSAL_ONLY' or tickets='[]'::jsonb)
);
create index if not exists market_trade_decision_issued on private.market_trade_decisions(issued_at desc);
alter table private.market_trade_decisions enable row level security;
revoke all on private.market_trade_decisions from public,anon,authenticated,service_role;
grant select,insert on private.market_trade_decisions to service_role;
grant usage,select on sequence private.market_trade_decisions_id_seq to service_role;

create or replace function public.market_record_decision_v1(payload jsonb, p_key text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
 v_id bigint;
 v_when timestamptz := (payload->>'as_of')::timestamptz;
 v_issued timestamptz;
 v_tickets jsonb := coalesce(payload->'tickets', '[]'::jsonb);
 v_ids bigint[];
begin
 if length(coalesce(p_key,'')) <> 64 or v_when is null
    or v_when > now() + interval '5 minutes'
    or v_when < now() - interval '10 minutes'
    or jsonb_typeof(v_tickets) <> 'array'
    or jsonb_array_length(v_tickets) > 5
    or coalesce(payload->>'status','') not in ('NO_TRADE','PROPOSAL_ONLY')
    or coalesce(payload->>'regime','') not in ('unavailable','mixed','risk_off','risk_on')
    or coalesce(payload->>'method','') not in ('rule_based','ai_researched') then
  raise exception 'Invalid live decision receipt';
 end if;
 select coalesce(array_agg(t.value::bigint),'{}'::bigint[]) into v_ids
 from jsonb_array_elements_text(coalesce(payload->'evidence_ids','[]'::jsonb)) as t(value);
 if cardinality(v_ids) < 1 or cardinality(v_ids) > 20
    or (select count(distinct x.value) from unnest(v_ids) as x(value)) <> cardinality(v_ids)
    or (select count(*) from public.market_observations m
        where m.id=any(v_ids) and m.published_at<=v_when and m.collected_at<=v_when) <> cardinality(v_ids) then
  raise exception 'Missing or future-only evidence';
 end if;
 -- Do not store the full portfolio, cash balance, NAV, accounts or credentials.
 if payload ? 'portfolio' or payload ? 'cash' or payload ? 'net_liquidation_value' or payload ? 'positions'
    or payload ? 'api_key' or payload ? 'owner_token' then
  raise exception 'Private portfolio fields prohibited in receipt';
 end if;
 insert into private.market_trade_decisions(
  request_key,as_of,status,regime,method,tickets,evidence_ids,blocked_reasons,input_provenance,model_label)
 values (
  p_key,v_when,payload->>'status',payload->>'regime',payload->>'method',v_tickets,
  v_ids,coalesce(payload->'blocked_reasons','[]'::jsonb),
  coalesce(payload->>'input_provenance','unknown'),nullif(payload->>'model_label','')
 )
 on conflict(request_key) do nothing returning id,issued_at into v_id,v_issued;
 if v_id is null then
  select id,issued_at into v_id,v_issued from private.market_trade_decisions where request_key=p_key;
 end if;
 return jsonb_build_object('decision_id',v_id,'issued_at',v_issued,'stored_privately',true,'orders_placed',false);
end;
$$;
revoke all on function public.market_record_decision_v1(jsonb,text) from public,anon,authenticated;
grant execute on function public.market_record_decision_v1(jsonb,text) to service_role;

create or replace function public.market_decision_history_v1(p_limit integer default 20)
returns jsonb language sql security invoker set search_path='' as $$
 select coalesce(jsonb_agg(to_jsonb(x) order by x.issued_at desc),'[]'::jsonb)
 from (
  select id,issued_at,as_of,status,regime,method,tickets,evidence_ids,
    blocked_reasons,input_provenance,model_label,note
  from private.market_trade_decisions order by issued_at desc limit least(greatest(p_limit,1),100)
 ) as x
$$;
revoke all on function public.market_decision_history_v1(integer) from public,anon,authenticated;
grant execute on function public.market_decision_history_v1(integer) to service_role;
notify pgrst,'reload schema';
