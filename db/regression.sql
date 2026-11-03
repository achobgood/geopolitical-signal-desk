-- Run with owner/service access. All synthetic records roll back.
begin;
set local role service_role;
do $$
declare p jsonb; a jsonb; b jsonb; id1 bigint; id2 bigint; n bigint;
begin
  p := '{"source_url":"https://example.org/signal-desk-synthetic-regression",
    "source_platform":"test","observation":"SYNTHETIC TEST: not an intelligence claim",
    "source_kind":"unknown","broad_region":"unspecified","capture_time_note":"synthetic",
    "evidence_type":"text","observed_vs_inferred":"test only","alternative_explanations":"test",
    "strengthening_evidence":"","falsifying_evidence":"","reliability_note":"synthetic",
    "source_independence_note":"unknown","provenance":{"permitted_public":true,"safety_reviewed":true,
    "account_type":"unknown","language_code":"und","media_access":"not_inspected"}}'::jsonb;
  a := public.signal_ingest_v1(p,'synthetic-regression-1','test');
  id1 := (a->'lead'->>'id')::bigint;
  if a->>'created' <> 'true' or a->'lead'->>'status' <> 'unverified' then raise exception 'New raw lead failed'; end if;
  b := public.signal_ingest_v1(p || '{"observation":"MUST NOT OVERWRITE"}'::jsonb,'synthetic-regression-1','test');
  if b->>'created' <> 'false' or b->'lead'->>'observation' <> p->>'observation' then raise exception 'Duplicate overwritten'; end if;
  select count(*) into n from public.evidence where lead_id=id1;
  if n <> 1 then raise exception 'Evidence duplicated'; end if;
  select count(*) into n from public.audit_events where entity_type='lead' and entity_id=id1;
  if n <> 2 then raise exception 'Audit missing'; end if;
  a := public.signal_ingest_v1(p || '{"source_url":"https://example.org/signal-desk-synthetic-regression-2"}'::jsonb,'synthetic-regression-2','test');
  id2 := (a->'lead'->>'id')::bigint;
  if id1=id2 then raise exception 'Different source collapsed'; end if;
  a := public.signal_record_v1('assessment',jsonb_build_object('lead_id',id1,'scenario','synthetic','rationale','synthetic',
    'confidence','low','counterevidence','','market_hypothesis','','alternative_explanations','routine',
    'corroborating_lead_ids',jsonb_build_array(id2),'baseline_comparison','not established'),'synthetic-assessment-1','test');
  b := public.signal_record_v1('assessment',jsonb_build_object('lead_id',id1,'scenario','synthetic','rationale','synthetic',
    'confidence','low','counterevidence','','market_hypothesis','','alternative_explanations','routine',
    'corroborating_lead_ids',jsonb_build_array(id2),'baseline_comparison','not established'),'synthetic-assessment-1','test');
  if a->>'created' <> 'true' or b->>'created' <> 'false' then raise exception 'Assessment retry failed'; end if;
  a := public.signal_relation_v1(jsonb_build_object('lead_id',id1,'other_lead_id',id2,'relation_type','related','rationale','synthetic'),'test');
  b := public.signal_relation_v1(jsonb_build_object('lead_id',id1,'other_lead_id',id2,'relation_type','related','rationale','synthetic'),'test');
  if a->>'created' <> 'true' or b->>'created' <> 'false' then raise exception 'Relation retry failed'; end if;
  a := public.signal_record_v1('decision',jsonb_build_object('lead_id',id1,'decision','no_alert','rationale','synthetic',
    'confidence','low','market_scenario','','notification_threshold','test only'),'synthetic-decision-1','test');
  if a->>'created' <> 'true' then raise exception 'Decision missing'; end if;
  begin
    perform public.signal_ingest_v1(p || '{"evidence_type":null}'::jsonb,'synthetic-rollback','test');
    raise exception 'Invalid transaction accepted';
  exception when not_null_violation then null;
  end;
  if exists(select 1 from public.leads where canonical_key='synthetic-rollback') then raise exception 'Atomic rollback failed'; end if;
  if has_table_privilege('service_role','public.audit_events','DELETE') then raise exception 'Audit delete permitted'; end if;
end $$;
reset role;
set local role anon;
do $$ begin
  begin perform id from public.leads limit 1; raise exception 'Anonymous read allowed';
  exception when insufficient_privilege then null; end;
  begin perform public.signal_ready_v1(); raise exception 'Anonymous RPC allowed';
  exception when insufficient_privilege then null; end;
end $$;
reset role;
set local role authenticated;
do $$ begin
  begin perform id from public.leads limit 1; raise exception 'End-user read allowed';
  exception when insufficient_privilege then null; end;
  begin perform public.signal_ready_v1(); raise exception 'End-user RPC allowed';
  exception when insufficient_privilege then null; end;
end $$;
rollback;
select 'database regression assertions passed; synthetic records rolled back' as result;
