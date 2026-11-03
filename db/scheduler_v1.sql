-- Existing Supabase project only. No credential is returned or embedded in SQL.
create extension if not exists pg_cron;
create extension if not exists pg_net with schema extensions;
create schema if not exists private;
revoke all on schema private from public,anon,authenticated;
do $$ begin
 if not exists(select 1 from vault.secrets where name='signal_desk_scheduler') then
  perform vault.create_secret(encode(extensions.gen_random_bytes(32),'hex'),'signal_desk_scheduler','Narrow collection-only Render credential');
 end if;
end $$;
create table if not exists private.signal_dispatches (
 request_id bigint primary key,run_key text not null,dispatched_at timestamptz not null default now(),
 status_code integer,error_message text
);
alter table private.signal_dispatches enable row level security;
revoke all on private.signal_dispatches from public,anon,authenticated,service_role;
create or replace function private.dispatch_signal_collection()
returns bigint language plpgsql security invoker set search_path='' as $$
declare token text; req bigint; k text;
begin
 update private.signal_dispatches d set status_code=r.status_code,error_message=case when r.error_msg is not null then 'HTTP transport failure' else null end
 from net._http_response r where r.id=d.request_id and d.status_code is null;
 k:='cron:'||to_char(now() at time zone 'UTC','YYYYMMDDHH24')||':'||floor(extract(minute from now() at time zone 'UTC')/15)::text;
 select d.request_id into req from private.signal_dispatches d join net._http_response r on r.id=d.request_id
 where d.run_key=k and r.status_code=200 and r.content::jsonb->'run'->>'run_status'='completed' order by d.dispatched_at desc limit 1;
 if req is not null then return req;end if;
 select decrypted_secret into strict token from vault.decrypted_secrets where name='signal_desk_scheduler';
 req:=net.http_post(url:='https://geopolitical-signal-desk.onrender.com/v1/pipeline/scheduled',
   headers:=jsonb_build_object('Content-Type','application/json','Authorization','Bearer '||token),
   body:=jsonb_build_object('run_key',k),timeout_milliseconds:=240000);
 insert into private.signal_dispatches(request_id,run_key) values(req,k);
 return req;
end $$;
revoke all on function private.dispatch_signal_collection() from public,anon,authenticated,service_role;
-- Registration is intentionally a separate verified action after the API deploy.
-- cron.schedule('signal-desk-collection-dispatch','*/5 * * * *','select private.dispatch_signal_collection();');
