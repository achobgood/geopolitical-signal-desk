-- Durable social retrieval queue and restricted worker interface.
create table if not exists private.signal_social_queue (
 lead_id bigint primary key references public.leads(id) on delete cascade,
 state text not null default 'pending' check(state in ('pending','leased','retry','retrieved','unavailable')),
 attempts integer not null default 0,
 queued_at timestamptz not null default now(),
 due_at timestamptz not null default now(),
 lease_until timestamptz,
 receipt jsonb not null default '{}'
);
alter table private.signal_social_queue enable row level security;
revoke all on private.signal_social_queue from public, anon, authenticated, service_role;
create index if not exists signal_social_queue_due on private.signal_social_queue(due_at,lead_id)
where state in ('pending','retry','leased');

create or replace function public.signal_social_claim_v1(p_limit integer)
returns jsonb language plpgsql security definer set search_path='' as $$
declare result jsonb;
begin
 insert into private.signal_social_queue(lead_id,queued_at)
 select l.id,l.created_at from public.leads l
 where l.source_platform='mastodon_public' and l.provenance->>'content_access'='metadata_only'
 on conflict do nothing;
 update private.signal_social_queue set state='unavailable',lease_until=null,
 receipt=jsonb_build_object('outcome','attempt_limit','finished_at',now())
 where state='leased' and lease_until<=now() and attempts>=4;
 with picked as (
  select q.lead_id from private.signal_social_queue q
  where q.attempts<4 and ((q.state in ('pending','retry') and q.due_at<=now())
        or (q.state='leased' and q.lease_until<=now()))
  order by q.due_at,q.lead_id limit greatest(0,least(p_limit,8)) for update skip locked
 ), claimed as (
  update private.signal_social_queue q set state='leased',attempts=q.attempts+1,lease_until=now()+interval '5 minutes'
  from picked p where p.lead_id=q.lead_id returning q.lead_id,q.attempts
 )
 select coalesce(jsonb_agg(jsonb_build_object('lead_id',c.lead_id,'attempt',c.attempts,'source_url',l.source_url,
 'provider_item_id',l.provenance->>'provider_item_id')),'[]') into result
 from claimed c join public.leads l on l.id=c.lead_id;
 return result;
end $$;

create or replace function public.signal_social_finish_v1(p_lead bigint,p_attempt integer,p_outcome text,p_refs jsonb,p_origin text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare q private.signal_social_queue%rowtype; state_after text; rec jsonb; anchor record; relation jsonb; links jsonb:='[]';
begin
 select * into q from private.signal_social_queue where lead_id=p_lead for update;
 if not found or q.state<>'leased' or q.attempts<>p_attempt or q.lease_until<=now() then
  return jsonb_build_object('lead_id',p_lead,'outcome','stale_lease');
 end if;
 if p_outcome not in ('public_text','publisher_pointer','retryable','unavailable') then raise exception 'Invalid outcome';end if;
 state_after:=case when p_outcome='public_text' then 'retrieved'
  when p_outcome='retryable' and q.attempts<4 then 'retry' else 'unavailable' end;
 if p_outcome in ('public_text','publisher_pointer') then
  for anchor in
   select distinct l.id,c.id cluster_id,l.source_url from public.leads l
   join public.cluster_leads m on m.lead_id=l.id
   join public.clusters c on c.id=m.cluster_id
   where l.id<>p_lead and c.canonical_key like 'event-review:%'
     and (l.source_url=p_origin or p_refs ? l.source_url)
   order by c.id,l.id limit 12
  loop
   relation:=public.signal_relation_v1(jsonb_build_object('lead_id',p_lead,'other_lead_id',anchor.id,
    'relation_type',case when anchor.source_url=p_origin and p_outcome='public_text' then 'same_origin' else 'related' end,
    'rationale','Exact public source URL match. Shared reference only, with event claim and independence awaiting review.'),'pipeline:social-recovery');
   insert into public.cluster_leads(cluster_id,lead_id,relationship,rationale)
   values(anchor.cluster_id,p_lead,'related','Automatic exact source reference match. Substantive assessment pending.') on conflict do nothing;
   links:=links||jsonb_build_array(jsonb_build_object('cluster_id',anchor.cluster_id,'anchor_lead_id',anchor.id,'relation_id',relation->'id'));
  end loop;
 end if;
 rec:=jsonb_build_object('lead_id',p_lead,'attempt',p_attempt,'outcome',p_outcome,'state',state_after,
  'finished_at',now(),'links',links,'next_retry_at',case when state_after='retry' then now()+make_interval(mins=>15*(1<<(q.attempts-1))) end);
 update private.signal_social_queue set state=state_after,lease_until=null,
 due_at=coalesce((rec->>'next_retry_at')::timestamptz,due_at),receipt=rec where lead_id=p_lead;
 return rec;
end $$;

create or replace function public.signal_social_status_v1()
returns jsonb language sql stable security definer set search_path='' as $$
 select jsonb_build_object('counts',coalesce((select jsonb_object_agg(state,n) from
 (select state,count(*) n from private.signal_social_queue group by state) s),'{}'),
 'oldest_pending_at',min(queued_at) filter(where state in ('pending','retry','leased')),
 'oldest_pending_age_seconds',extract(epoch from now()-min(queued_at) filter(where state in ('pending','retry','leased'))),
 'next_retry_at',min(due_at) filter(where state='retry'),
 'awaiting_claim_review',count(*) filter(where state='retrieved'),
 'recent_receipts',coalesce((select jsonb_agg(receipt) from
 (select receipt from private.signal_social_queue where receipt<>'{}' order by receipt->>'finished_at' desc limit 20) r),'[]'))
 from private.signal_social_queue;
$$;
revoke all on function public.signal_social_claim_v1(integer),public.signal_social_finish_v1(bigint,integer,text,jsonb,text),public.signal_social_status_v1() from public,anon,authenticated;
grant execute on function public.signal_social_claim_v1(integer),public.signal_social_finish_v1(bigint,integer,text,jsonb,text),public.signal_social_status_v1() to service_role;
