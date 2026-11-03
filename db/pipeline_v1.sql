alter table public.collection_runs add column if not exists details jsonb not null default '{}'::jsonb;
alter table public.clusters add column if not exists canonical_key text;
create unique index if not exists clusters_canonical_key on public.clusters(canonical_key);
create table if not exists public.pipeline_runs (
 id bigint generated always as identity primary key, run_key text unique not null,
 trigger_kind text not null, started_at timestamptz not null default now(), finished_at timestamptz,
 run_status text not null default 'running' check(run_status in ('running','completed','partial','failed')),
 attempts integer not null default 1, details jsonb not null default '{}'::jsonb
);
alter table public.pipeline_runs enable row level security;
revoke all on public.pipeline_runs from public,anon,authenticated;
revoke all on public.pipeline_runs from service_role;
grant select,insert,update on public.pipeline_runs to service_role;
grant usage,select on sequence public.pipeline_runs_id_seq to service_role;
create or replace function public.signal_pipeline_claim_v1(p_key text,p_trigger text)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare r public.pipeline_runs; claimed boolean:=false;
begin
 perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(p_key,0));
 select * into r from public.pipeline_runs where run_key=p_key;
 if r.id is null then
  insert into public.pipeline_runs(run_key,trigger_kind) values(p_key,p_trigger) returning * into r;claimed:=true;
 elsif r.run_status in ('failed','partial') or (r.run_status='running' and r.started_at<now()-interval '15 minutes') then
  update public.pipeline_runs set run_status='running',started_at=now(),finished_at=null,attempts=attempts+1 where id=r.id returning * into r;claimed:=true;
 end if;
 return jsonb_build_object('claimed',claimed,'run',to_jsonb(r));
end $$;
revoke all on function public.signal_pipeline_claim_v1(text,text) from public,anon,authenticated;
grant execute on function public.signal_pipeline_claim_v1(text,text) to service_role;
create or replace function public.signal_cluster_v1(payload jsonb,p_actor text)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare cid bigint; lid bigint; added integer:=0;
begin
 insert into public.clusters(canonical_key,title,broad_region,scenario,competing_explanations)
 values(payload->>'canonical_key',payload->>'title',payload->>'broad_region',payload->>'scenario',payload->>'competing_explanations')
 on conflict(canonical_key) do nothing returning id into cid;
 if cid is null then select id into strict cid from public.clusters where canonical_key=payload->>'canonical_key';end if;
 for lid in select value::bigint from jsonb_array_elements_text(payload->'lead_ids') loop
  insert into public.cluster_leads(cluster_id,lead_id,relationship,rationale) values(cid,lid,'related','Explicit analyst linkage or automated topic-review queue; not corroboration') on conflict do nothing;
  if found then added:=added+1;end if;
 end loop;
 if added>0 then insert into public.audit_events(actor,action,entity_type,entity_id) values(p_actor,'link_evidence','cluster',cid);end if;
 return jsonb_build_object('id',cid,'new_links',added);
end $$;
revoke all on function public.signal_cluster_v1(jsonb,text) from public,anon,authenticated;
grant execute on function public.signal_cluster_v1(jsonb,text) to service_role;
create or replace function public.signal_ready_v1()
returns jsonb language sql security invoker set search_path='' as $$
 select jsonb_build_object('schema_version',1,
  'rls_tables',(select count(*) from pg_catalog.pg_class c join pg_catalog.pg_namespace n on n.oid=c.relnamespace
    where n.nspname='public' and c.relkind='r' and c.relrowsecurity),
  'end_user_policies',(select count(*) from pg_catalog.pg_policies where schemaname='public'),
  'lead_probe',(select count(*) from (select id from public.leads limit 1) p))
$$;
revoke all on function public.signal_ready_v1() from public,anon,authenticated;
grant execute on function public.signal_ready_v1() to service_role;
notify pgrst,'reload schema';
