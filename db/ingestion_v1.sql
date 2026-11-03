-- Additive change against the inspected ten-table schema. No table recreation.
alter table public.leads add column if not exists provenance jsonb not null default '{}'::jsonb;
alter table public.assessments add column if not exists analysis jsonb not null default '{}'::jsonb;
alter table public.assessments add column if not exists request_key text;
create unique index if not exists assessments_request_key_unique on public.assessments(request_key);
alter table public.alert_history add column if not exists notification_threshold text not null default '';
alter table public.alert_history add column if not exists request_key text;
create unique index if not exists alerts_request_key_unique on public.alert_history(request_key);

create table if not exists public.audit_events (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  actor text not null,
  action text not null,
  entity_type text not null,
  entity_id bigint not null,
  request_key text
);
alter table public.audit_events enable row level security;
revoke all on public.audit_events from public, anon, authenticated;
grant select, insert on public.audit_events to service_role;
grant usage, select on sequence public.audit_events_id_seq to service_role;

create or replace function public.signal_ingest_v1(payload jsonb, p_key text, p_actor text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  v_lead public.leads;
  v_source_id bigint;
  v_created boolean := false;
begin
  if payload->'provenance'->>'permitted_public' is distinct from 'true'
     or payload->'provenance'->>'safety_reviewed' is distinct from 'true'
     or coalesce(payload->>'source_url','') = ''
     or coalesce(payload->>'observation','') = ''
     or coalesce(p_key,'') = '' then
    raise exception 'Invalid public intake';
  end if;
  insert into public.leads(source_url,source_platform,observation,canonical_key,source_kind,broad_region,
    original_posted_at,claimed_event_at,capture_time_note,evidence_type,observed_vs_inferred,
    media_actually_inspected,alternative_explanations,strengthening_evidence,falsifying_evidence,
    reliability_note,source_independence_note,provenance)
  values(payload->>'source_url',payload->>'source_platform',payload->>'observation',p_key,
    payload->>'source_kind',payload->>'broad_region',(payload->>'original_posted_at')::timestamptz,
    (payload->>'claimed_event_at')::timestamptz,payload->>'capture_time_note',payload->>'evidence_type',
    payload->>'observed_vs_inferred',coalesce(payload->'provenance'->>'media_access' = 'inspected',false),
    payload->>'alternative_explanations',payload->>'strengthening_evidence',payload->>'falsifying_evidence',
    payload->>'reliability_note',payload->>'source_independence_note',payload->'provenance')
  on conflict (canonical_key) do nothing returning * into v_lead;
  if v_lead.id is not null then
    v_created := true;
    insert into public.sources(platform,public_url,account_type,language_code,broad_region,reliability_note)
    values(payload->>'source_platform',payload->>'source_url',payload->'provenance'->>'account_type',
      payload->'provenance'->>'language_code',payload->>'broad_region',payload->>'reliability_note')
    on conflict (platform,public_url) do nothing returning id into v_source_id;
    if v_source_id is null then
      select id into v_source_id from public.sources
      where platform=payload->>'source_platform' and public_url=payload->>'source_url';
    end if;
    insert into public.evidence(lead_id,source_id,public_url,medium,posted_at,capture_time_note,
      access_status,observations,provenance_note)
    values(v_lead.id,v_source_id,payload->>'source_url',payload->>'evidence_type',
      (payload->>'original_posted_at')::timestamptz,payload->>'capture_time_note',
      payload->'provenance'->>'media_access',payload->>'observation',(payload->'provenance')::text);
  else
    select * into strict v_lead from public.leads where canonical_key=p_key;
  end if;
  insert into public.audit_events(actor,action,entity_type,entity_id,request_key)
  values(p_actor,case when v_created then 'ingest' else 'duplicate_intake' end,'lead',v_lead.id,p_key);
  return jsonb_build_object('created',v_created,'lead',to_jsonb(v_lead));
end $$;
revoke all on function public.signal_ingest_v1(jsonb,text,text) from public,anon,authenticated;
grant execute on function public.signal_ingest_v1(jsonb,text,text) to service_role;

create or replace function public.signal_record_v1(p_kind text,payload jsonb,p_key text,p_actor text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  v_id bigint;
  v_created boolean := true;
  v_record jsonb;
  v_ref bigint;
begin
  if coalesce(p_key,'') = '' then raise exception 'Request key required'; end if;
  if p_kind='assessment' then
    for v_ref in select value::bigint from jsonb_array_elements_text(
      coalesce(payload->'corroborating_lead_ids','[]'::jsonb) || coalesce(payload->'contradicting_lead_ids','[]'::jsonb))
    loop
      if not exists(select 1 from public.leads where id=v_ref) then
        raise foreign_key_violation using message='Referenced lead missing';
      end if;
    end loop;
    insert into public.assessments(lead_id,scenario,rationale,confidence,counterevidence,market_hypothesis,
      alternative_explanations,analysis,request_key)
    values((payload->>'lead_id')::bigint,payload->>'scenario',payload->>'rationale',payload->>'confidence',
      payload->>'counterevidence',payload->>'market_hypothesis',payload->>'alternative_explanations',payload,p_key)
    on conflict(request_key) do nothing returning id into v_id;
    if v_id is null then
      v_created:=false;
      select id into strict v_id from public.assessments where request_key=p_key;
    end if;
    select to_jsonb(a) into v_record from public.assessments a where id=v_id;
  elsif p_kind='decision' then
    insert into public.alert_history(lead_id,decision,rationale,confidence,market_scenario,notification_threshold,request_key)
    values((payload->>'lead_id')::bigint,payload->>'decision',payload->>'rationale',payload->>'confidence',
      payload->>'market_scenario',payload->>'notification_threshold',p_key)
    on conflict(request_key) do nothing returning id into v_id;
    if v_id is null then
      v_created:=false;
      select id into strict v_id from public.alert_history where request_key=p_key;
    end if;
    select to_jsonb(a) into v_record from public.alert_history a where id=v_id;
  else
    raise exception 'Unsupported record kind';
  end if;
  insert into public.audit_events(actor,action,entity_type,entity_id,request_key)
  values(p_actor,case when v_created then 'create' else 'duplicate_intake' end,p_kind,v_id,p_key);
  return jsonb_build_object('created',v_created,'record',v_record);
end $$;
revoke all on function public.signal_record_v1(text,jsonb,text,text) from public,anon,authenticated;
grant execute on function public.signal_record_v1(text,jsonb,text,text) to service_role;

create or replace function public.signal_relation_v1(payload jsonb,p_actor text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare v_id bigint; v_created boolean := true;
begin
  insert into public.lead_relations(lead_id,other_lead_id,relation_type,rationale)
  values((payload->>'lead_id')::bigint,(payload->>'other_lead_id')::bigint,payload->>'relation_type',payload->>'rationale')
  on conflict(lead_id,other_lead_id,relation_type) do nothing returning id into v_id;
  if v_id is null then
    v_created:=false;
    select id into strict v_id from public.lead_relations where lead_id=(payload->>'lead_id')::bigint
      and other_lead_id=(payload->>'other_lead_id')::bigint and relation_type=payload->>'relation_type';
  end if;
  insert into public.audit_events(actor,action,entity_type,entity_id)
  values(p_actor,case when v_created then 'create' else 'duplicate_intake' end,'relation',v_id);
  return jsonb_build_object('id',v_id,'created',v_created);
end $$;
revoke all on function public.signal_relation_v1(jsonb,text) from public,anon,authenticated;
grant execute on function public.signal_relation_v1(jsonb,text) to service_role;

create or replace function public.signal_ready_v1()
returns jsonb language sql security invoker set search_path = '' as $$
  select jsonb_build_object('schema_version',1,
    'rls_tables',(select count(*) from pg_catalog.pg_class c join pg_catalog.pg_namespace n on n.oid=c.relnamespace
      where n.nspname='public' and c.relrowsecurity and c.relname in ('leads','sources','evidence','clusters',
        'cluster_leads','lead_relations','assessments','alert_history','collection_runs','activity_baselines','audit_events')),
    'end_user_policies',(select count(*) from pg_catalog.pg_policies where schemaname='public'),
    'lead_probe',(select count(*) from (select id from public.leads limit 1) p))
$$;
revoke all on function public.signal_ready_v1() from public,anon,authenticated;
grant execute on function public.signal_ready_v1() to service_role;
notify pgrst, 'reload schema';
