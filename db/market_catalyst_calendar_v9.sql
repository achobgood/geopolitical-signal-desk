-- Extend the existing immutable receipt pipeline to two reviewed official calendars.
-- Existing private authorization, rolling source quotas and retry backoff are retained.
alter table public.market_macro_captures drop constraint market_macro_captures_provider_check;
alter table public.market_macro_captures add constraint market_macro_captures_provider_check check(provider in('bls_employment','nyfed_sofr','ofr_fsi','fed_fomc_calendar','bea_release_calendar'));
create or replace function private.market_macro_claim_v1(p_provider text,p_force boolean) returns jsonb
language plpgsql security definer set search_path='' as $$
declare last_check private.market_macro_checks;last_result private.market_macro_check_results;cid bigint;num int;n timestamptz:=clock_timestamp();special boolean:=false;new_id bigint;begin
 if p_provider not in ('bls_employment','nyfed_sofr','ofr_fsi','fed_fomc_calendar','bea_release_calendar') then raise exception 'Unknown macro provider';end if;
 perform pg_advisory_xact_lock(hashtextextended('macro:'||p_provider,0));
 select id into cid from public.market_macro_captures where provider=p_provider order by id desc limit 1;
 select * into last_check from private.market_macro_checks where provider=p_provider order by id desc limit 1;
 if last_check.id is not null then
  select * into last_result from private.market_macro_check_results where check_id=last_check.id;
  if last_result.id is null and last_check.started_at>n-interval '2 minutes' then return jsonb_build_object('claimed',false,'capture_id',cid);end if;
  if last_result.status='UNAVAILABLE' and last_check.started_at>n-interval '1 hour' then return jsonb_build_object('claimed',false,'capture_id',cid);end if;
  special:=p_provider='bls_employment' and (n at time zone 'America/New_York')::time>=time '08:30'
    and (last_check.started_at at time zone 'America/New_York')::date=(n at time zone 'America/New_York')::date
    and (last_check.started_at at time zone 'America/New_York')::time<time '08:30';
  special:=special or (p_provider='nyfed_sofr' and (n at time zone 'America/New_York')::time>=time '08:15'
    and (last_check.started_at at time zone 'America/New_York')::date=(n at time zone 'America/New_York')::date
    and (last_check.started_at at time zone 'America/New_York')::time<time '08:15');
  special:=special or (p_provider='nyfed_sofr' and (n at time zone 'America/New_York')::time>=time '14:45'
    and (last_check.started_at at time zone 'America/New_York')::date=(n at time zone 'America/New_York')::date
    and (last_check.started_at at time zone 'America/New_York')::time<time '14:45');
  if last_result.status='CAPTURED' and not special and last_check.started_at>n-(case when p_force then interval '30 minutes' else interval '4 hours' end) then return jsonb_build_object('claimed',false,'capture_id',cid);end if;
 end if;
 select count(*) into num from private.market_macro_checks where provider=p_provider and started_at>=n-interval '24 hours';
 if num>=(case when p_provider='bls_employment' then 8 else 12 end) then return jsonb_build_object('claimed',false,'capture_id',cid,'daily_bound_reached',true);end if;
 insert into private.market_macro_checks(provider) values(p_provider) returning id into new_id;
 return jsonb_build_object('claimed',true,'check_id',new_id);
end $$;
create or replace function private.market_macro_capture_v1(p jsonb,p_check_id bigint) returns jsonb
language plpgsql security definer set search_path='' as $$
declare check_row private.market_macro_checks;rec public.market_macro_captures;clean jsonb;approved jsonb;n timestamptz:=clock_timestamp();begin
 select * into strict check_row from private.market_macro_checks where id=p_check_id;
 if check_row.provider is distinct from p->>'provider' or jsonb_typeof(p->'data') is distinct from 'object'
  or p->>'document_sha256' is null or length(p->>'raw_gzip_base64') not between 1 and 2500000
  or jsonb_typeof(p->'source_urls') is distinct from 'array' then raise exception 'Invalid macro source receipt';end if;
 if p->>'provider'='bls_employment' then
  -- Accept old GET receipts during rollout; new code makes one official batch POST.
  if p->'source_urls' is distinct from jsonb_build_array('https://api.bls.gov/publicAPI/v1/timeseries/data/CES0000000001','https://api.bls.gov/publicAPI/v1/timeseries/data/LNS14000000','https://api.bls.gov/publicAPI/v1/timeseries/data/CES0500000003')
   and (p->'source_urls' is distinct from jsonb_build_array('https://api.bls.gov/publicAPI/v1/timeseries/data/')
    or p->'requested_series_ids' is distinct from jsonb_build_array('CES0000000001','LNS14000000','CES0500000003')) then raise exception 'Unapproved BLS series origin';end if;
 else
  approved:=case p->>'provider'
   when 'nyfed_sofr' then jsonb_build_array('https://markets.newyorkfed.org/api/rates/secured/sofr/last/3.json')
   when 'ofr_fsi' then jsonb_build_array('https://www.financialresearch.gov/financial-stress-index/data/fsi.csv')
   when 'fed_fomc_calendar' then jsonb_build_array('https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm')
   when 'bea_release_calendar' then jsonb_build_array('https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics')end;
  if p->'source_urls' is distinct from approved then raise exception 'Unapproved macro origin';end if;
 end if;
 if p->>'provider' in('fed_fomc_calendar','bea_release_calendar') then
  if jsonb_typeof(p->'events')is distinct from 'array' then raise exception 'Invalid calendar event collection';end if;
  if jsonb_array_length(p->'events')not between 1 and 1500
   or (p->'data'->>'scheduled_event_count')::int is distinct from jsonb_array_length(p->'events') then raise exception 'Invalid calendar event count';end if;
 end if;
 clean:=(p-'raw_gzip_base64')||jsonb_build_object('captured_at',n,'usable_at',n);
 insert into public.market_macro_captures(provider,source_as_of,document_sha256,payload,raw_gzip_base64)
 values(p->>'provider',p->>'source_as_of',p->>'document_sha256',clean,p->>'raw_gzip_base64')
 on conflict(provider,document_sha256)do nothing returning * into rec;
 if not found then select * into strict rec from public.market_macro_captures where provider=p->>'provider'and document_sha256=p->>'document_sha256';end if;
 insert into private.market_macro_check_results(check_id,status,capture_id)values(p_check_id,'CAPTURED',rec.id)on conflict(check_id)do nothing;
 return jsonb_build_object('id',rec.id,'captured_at',rec.captured_at,'payload',rec.payload);
end $$;
revoke all on function private.market_macro_claim_v1(text,boolean),private.market_macro_capture_v1(jsonb,bigint)from public,anon,authenticated;
grant execute on function private.market_macro_claim_v1(text,boolean),private.market_macro_capture_v1(jsonb,bigint)to service_role;
notify pgrst,'reload schema';
