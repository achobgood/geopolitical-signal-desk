-- One dedicated cycle, reusing the existing Vault scheduler credential.
-- No changes to existing geopolitical/reference-feed/watch jobs.
create table private.market_forecast_dispatches (
 id bigint generated always as identity primary key,
 request_id bigint not null unique, run_key text not null unique,
 dispatched_at timestamptz not null default clock_timestamp()
);
alter table private.market_forecast_dispatches enable row level security;
revoke all on private.market_forecast_dispatches from public,anon,authenticated,service_role;
create function private.dispatch_market_forecasts_v1() returns bigint language plpgsql security invoker set search_path='' as $$
declare token text; req bigint; k text; local_now timestamp; begin
 local_now:=clock_timestamp() at time zone 'America/New_York';
 -- Conservative close publication buffer, issuance attempts and revision checks.
 if not public.market_is_session_v1(local_now::date) or local_now::time not between time '16:30' and time '19:00' then return null; end if;
 k:='forecast:cron:'||to_char(clock_timestamp() at time zone 'UTC','YYYYMMDDHH24MI');
 select request_id into req from private.market_forecast_dispatches where run_key=k;
 if found then return req; end if;
 select decrypted_secret into strict token from vault.decrypted_secrets where name='signal_desk_scheduler';
 req:=net.http_post(url:='https://geopolitical-signal-desk.onrender.com/v1/market/forecasts/scheduled',
 headers:=jsonb_build_object('Content-Type','application/json','Authorization','Bearer '||token),
 body:=jsonb_build_object('run_key',k),timeout_milliseconds:=240000);
 insert into private.market_forecast_dispatches(request_id,run_key) values(req,k);
 return req;
end $$;
revoke all on function private.dispatch_market_forecasts_v1() from public,anon,authenticated,service_role;
-- Activate only after deploying and verifying the live scheduled route:
-- select cron.schedule('market-forecast-cycle','*/15 20-23,0 * * 1-5','select private.dispatch_market_forecasts_v1();');
