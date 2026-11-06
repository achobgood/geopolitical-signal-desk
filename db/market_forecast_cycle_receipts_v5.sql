-- Record genuine autonomous cycle receipts outside the issuance window as well.
create or replace function private.dispatch_market_forecasts_v1() returns bigint language plpgsql security invoker set search_path='' as $$
declare token text; req bigint; k text; local_now timestamp; begin
 local_now:=clock_timestamp() at time zone 'America/New_York';
 -- Every real cycle leaves a backend receipt, including missed locks and provider outages.
 -- The issuance and evaluation RPCs enforce their own independent server-clock gates.
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
