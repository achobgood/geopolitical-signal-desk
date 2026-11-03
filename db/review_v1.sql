-- Additive, service-only review transaction; existing tables and data preserved.
create or replace function public.signal_review_v1(payload jsonb, p_key text, p_actor text)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare
 cid bigint; aid bigint; did bigint; lid bigint; h jsonb; e jsonb; rel jsonb;
 a_ids jsonb:='[]'; relation_ids jsonb:='[]'; current_asof timestamptz;
 anchor_id bigint; v_analysis jsonb; duplicate boolean:=false;
begin
 if coalesce(p_key,'')='' or jsonb_array_length(payload->'evidence')=0 then
  raise exception 'Review key and evidence required';
 end if;
 perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('review:'||(payload->>'event_key'),0));
 select entity_id into cid from public.audit_events
 where entity_type='review' and action='commit_review' and request_key=p_key limit 1;
 if cid is not null then
  select coalesce(jsonb_agg(a.id order by a.id),'[]') into a_ids from public.assessments a where a.analysis->>'review_key'=p_key;
  select id into did from public.alert_history where request_key=p_key||':decision';
  return jsonb_build_object('created',false,'cluster_id',cid,'assessment_ids',a_ids,'decision_id',did,'notification_delivered',false);
 end if;
 anchor_id:=(payload->'evidence'->0->>'lead_id')::bigint;
 -- Check all references before creating any event/assessment/decision rows.
 for e in select value from jsonb_array_elements(payload->'evidence') loop
  if not exists(select 1 from public.leads where id=(e->>'lead_id')::bigint and source_url=e->>'source_url') then
   raise foreign_key_violation using message='Evidence reference missing or URL mismatch';
  end if;
 end loop;
 insert into public.clusters(canonical_key,title,broad_region,scenario,competing_explanations)
 values('event-review:'||(payload->>'event_key'),payload->>'title',payload->>'broad_region',payload->>'summary',
        payload->'hypotheses'->0->>'alternative_explanations')
 on conflict(canonical_key) do nothing returning id into cid;
 if cid is null then select id into strict cid from public.clusters where canonical_key='event-review:'||(payload->>'event_key');end if;
 select max((a.analysis->>'as_of')::timestamptz) into current_asof from public.assessments a where a.cluster_id=cid and a.analysis ? 'as_of';
 -- A late backfill appends history without replacing a newer current scenario.
 if current_asof is null or (payload->>'as_of')::timestamptz>=current_asof then
  update public.clusters set title=payload->>'title',scenario=payload->>'summary',
   competing_explanations=payload->'hypotheses'->0->>'alternative_explanations',updated_at=now() where id=cid;
 end if;
 for e in select value from jsonb_array_elements(payload->'evidence') loop
  insert into public.cluster_leads(cluster_id,lead_id,relationship,rationale)
  values(cid,(e->>'lead_id')::bigint,'related','Research review linkage; evidence roles and origin basis in versioned assessments') on conflict do nothing;
 end loop;
 for rel in select value from jsonb_array_elements(payload->'relations') loop
  v_analysis:=public.signal_relation_v1(rel,p_actor);
  relation_ids:=relation_ids||jsonb_build_array(v_analysis->'id');
 end loop;
 for h in select value from jsonb_array_elements(payload->'hypotheses') loop
  v_analysis:=(payload-'hypotheses'-'relations'-'decision')||jsonb_build_object(
   'review_key',p_key,'assessment_origin',case when payload->'report_provenance' is not null and payload->'report_provenance'<>'null'::jsonb then 'historical_summary' else 'agent_analysis' end,
   'hypothesis_key',h->>'key','corroborating_lead_ids',h->'supporting_lead_ids','contradicting_lead_ids',h->'contradicting_lead_ids',
   'strengthening_conditions',h->>'strengthening_conditions','falsification_criteria',h->>'falsification_criteria',
   'analysis_threshold',payload->>'analysis_threshold','source_limitations',payload->>'independence_assessment',
   'decision',payload->'decision','reasoning_origin','Authorized research caller; server validates and persists, no autonomous server model');
  insert into public.assessments(lead_id,cluster_id,scenario,rationale,confidence,counterevidence,market_hypothesis,alternative_explanations,analysis,request_key)
  values(anchor_id,cid,h->>'scenario',h->>'rationale',h->>'confidence',h->>'counterevidence',h->>'conditional_market_hypothesis',h->>'alternative_explanations',v_analysis,p_key||':'||(h->>'key')) returning id into aid;
  a_ids:=a_ids||jsonb_build_array(aid);
  insert into public.audit_events(actor,action,entity_type,entity_id,request_key) values(p_actor,'create','assessment',aid,p_key);
 end loop;
 insert into public.alert_history(lead_id,decision,rationale,confidence,market_scenario,notification_threshold,request_key)
 values(anchor_id,payload->'decision'->>'decision',payload->'decision'->>'rationale',payload->'hypotheses'->0->>'confidence',
        payload->'hypotheses'->0->>'conditional_market_hypothesis',payload->'decision'->>'notification_threshold',p_key||':decision') returning id into did;
 insert into public.audit_events(actor,action,entity_type,entity_id,request_key) values(p_actor,'commit_review','review',cid,p_key);
 return jsonb_build_object('created',true,'cluster_id',cid,'assessment_ids',a_ids,'decision_id',did,'relation_ids',relation_ids,'notification_delivered',false);
end $$;
revoke all on function public.signal_review_v1(jsonb,text,text) from public,anon,authenticated;
grant execute on function public.signal_review_v1(jsonb,text,text) to service_role;
