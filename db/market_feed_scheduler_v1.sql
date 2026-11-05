-- Independent market-source scheduling; does not change the geopolitical cron or dispatch function.
-- Uses the EXISTING Vault-held scheduler-only key and the EXISTING Render service.
-- This migration registers the function only. Register the cron job after deployment smoke tests.
create table if not exists private.market_dispatches (
 request_id bigint primary key,
 run_key text not null,
 dispatched_at timestamptz not null default now(),
 status_code integer,
 error_summary text
);
alter table private.market_dispatches enable row level security;
revoke all on private.market_dispatches from public, anon, authenticated, service_role;

create or replace function private.dispatch_market_feeds()
returns bigint language plpgsql security invoker set search_path = '' as $$
declare
 token text;
 req bigint;
 k text;
 existing bigint;
begin
 update private.market_dispatches d set status_code = r.status_code,
 error_summary = case when r.error_msg is not null then 'Network dispatch failure' else null end
 from net._http_response r where r.id = d.request_id and d.status_code is null;
 k := 'market:cron:' || to_char(now() at time zone 'UTC', 'YYYYMMDDHH24');
 select d.request_id into existing from private.market_dispatches d
 where d.run_key = k
 order by d.dispatched_at desc limit 1;
 if existing is not null then return existing; end if;
 select decrypted_secret into strict token from vault.decrypted_secrets where name='signal_desk_scheduler';
 req := net.http_post(
  url := 'https://geopolitical-signal-desk.onrender.com/v1/market/feeds/scheduled',
  headers := jsonb_build_object('Content-Type', 'application/json', 'Authorization', 'Bearer ' || token),
  body := jsonb_build_object('run_key', k),
  timeout_milliseconds := 240000
 );
 insert into private.market_dispatches(request_id,run_key) values(req,k);
 return req;
end $$;
revoke all on function private.dispatch_market_feeds() from public, anon, authenticated, service_role;

-- Activate only AFTER API and market_feed_runs RPCs are verified:
-- select cron.schedule('market-reference-feeds', '15 0,13,23 * * *', 'select private.dispatch_market_feeds();');
