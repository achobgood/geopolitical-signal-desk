-- Market collector run receipts: additive, separate from geopolitical pipeline_runs.
create table if not exists public.market_feed_runs (
  id bigint generated always as identity primary key,
  run_key text unique not null,
  trigger_kind text not null,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running'
    check (status in ('running','completed','partial','failed')),
  attempts integer not null default 1 check (attempts between 1 and 100),
  provider_results jsonb not null default '[]'::jsonb,
  error_summary text not null default '',
  check (length(run_key) between 6 and 120)
);
create index if not exists market_feed_runs_started on public.market_feed_runs(started_at desc);
alter table public.market_feed_runs enable row level security;
revoke all on public.market_feed_runs from public, anon, authenticated, service_role;
grant select, insert, update on public.market_feed_runs to service_role;
grant usage, select on sequence public.market_feed_runs_id_seq to service_role;

create or replace function public.market_feed_claim_v1(p_key text, p_trigger text)
returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  v_row public.market_feed_runs;
  v_claimed boolean := false;
begin
  if length(coalesce(p_key,'')) not between 6 and 120
     or p_trigger not in ('manual', 'supabase_cron') then
    raise exception 'Invalid market feed run';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('market-feed:' || p_key, 0));
  select * into v_row from public.market_feed_runs where run_key = p_key;
  if v_row.id is null then
    insert into public.market_feed_runs(run_key, trigger_kind)
    values (p_key, p_trigger) returning * into v_row;
    v_claimed := true;
  elsif v_row.status in ('failed','partial')
     or (v_row.status = 'running' and v_row.started_at < now() - interval '12 minutes') then
    update public.market_feed_runs
    set started_at = now(), finished_at = null, status = 'running',
        attempts = attempts + 1, provider_results = '[]'::jsonb, error_summary = ''
    where id = v_row.id and attempts < 100
    returning * into v_row;
    v_claimed := found;
  end if;
  return jsonb_build_object('claimed',v_claimed,'run',to_jsonb(v_row));
end;
$$;
revoke all on function public.market_feed_claim_v1(text,text) from public, anon, authenticated;
grant execute on function public.market_feed_claim_v1(text,text) to service_role;

notify pgrst, 'reload schema';
