-- Public-source research remains separate from owner-private financial records.
create table public.market_macro_captures(
 id bigint generated always as identity primary key,
 provider text not null check(provider in ('bls_employment','nyfed_sofr','ofr_fsi')),
 captured_at timestamptz not null default clock_timestamp(),
 source_as_of text not null, document_sha256 text not null check(document_sha256~'^[a-f0-9]{64}$'),
 payload jsonb not null,raw_gzip_base64 text not null,
 unique(provider,document_sha256)
);
create table private.market_macro_checks(
 id bigint generated always as identity primary key,provider text not null,
 started_at timestamptz not null default clock_timestamp()
);
create table private.market_macro_check_results(
 id bigint generated always as identity primary key,check_id bigint not null unique references private.market_macro_checks(id),
 status text not null check(status in ('CAPTURED','UNAVAILABLE')),
 capture_id bigint references public.market_macro_captures(id),finished_at timestamptz not null default clock_timestamp()
);
create table public.market_risk_briefs(
 id bigint generated always as identity primary key,generated_at timestamptz not null default clock_timestamp(),
 brief_date date not null default (clock_timestamp() at time zone 'America/New_York')::date,
 method_version text not null,source_signature text not null check(source_signature~'^[a-f0-9]{64}$'),
 payload jsonb not null,unique(brief_date,method_version,source_signature)
);
create table private.market_finance_keys(
 name text primary key check(name='owner_finance'),key_sha256 text not null check(key_sha256~'^[a-f0-9]{64}$'),
 created_at timestamptz not null default clock_timestamp()
);
create table private.market_portfolio_snapshots(
 id bigint generated always as identity primary key,record_key text not null unique,
 created_at timestamptz not null default clock_timestamp(),as_of timestamptz not null,
 payload jsonb not null
);
create table private.market_finance_research_reports(
 id bigint generated always as identity primary key,ticker text not null check(ticker~'^[A-Z][A-Z0-9.-]{0,11}$'),
 captured_at timestamptz not null default clock_timestamp(),payload jsonb not null
);
create table private.market_portfolio_analyses(
 id bigint generated always as identity primary key,snapshot_id bigint not null references private.market_portfolio_snapshots(id),
 generated_at timestamptz not null default clock_timestamp(),payload jsonb not null
);
create index on public.market_macro_captures(provider,id desc);
create index on private.market_macro_checks(provider,started_at desc);
create index on private.market_finance_research_reports(ticker,id desc);
create index on private.market_portfolio_snapshots(id desc);
do $$ declare s text;t text;begin
 for s,t in select * from(values('public','market_macro_captures'),('private','market_macro_checks'),('private','market_macro_check_results'),
 ('public','market_risk_briefs'),('private','market_finance_keys'),('private','market_portfolio_snapshots'),
 ('private','market_finance_research_reports'),('private','market_portfolio_analyses')) x(s,t) loop
 execute format('alter table %I.%I enable row level security',s,t);
 execute format('revoke all on %I.%I from public,anon,authenticated,service_role',s,t);
 if t<>'market_finance_keys' then execute format('create trigger immutable_research before update or delete on %I.%I for each row execute function public.market_forecast_immutable_v1()',s,t);end if;
 end loop;
end $$;
grant select on public.market_macro_captures,public.market_risk_briefs to service_role;

create function private.market_macro_claim_v1(p_provider text,p_force boolean) returns jsonb
language plpgsql security definer set search_path='' as $$
declare last_check private.market_macro_checks;last_result private.market_macro_check_results;cid bigint;num int;n timestamptz:=clock_timestamp();special boolean:=false;new_id bigint;begin
 if p_provider not in ('bls_employment','nyfed_sofr','ofr_fsi') then raise exception 'Unknown macro provider';end if;
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
create function public.market_macro_claim_v1(p_provider text,p_force boolean) returns jsonb language sql security invoker set search_path='' as $$select private.market_macro_claim_v1(p_provider,p_force)$$;

create function private.market_macro_capture_v1(p jsonb,p_check_id bigint) returns jsonb
language plpgsql security definer set search_path='' as $$
declare check_row private.market_macro_checks;rec public.market_macro_captures;clean jsonb;n timestamptz:=clock_timestamp();begin
 select * into strict check_row from private.market_macro_checks where id=p_check_id;
 if check_row.provider is distinct from p->>'provider' or jsonb_typeof(p->'data') is distinct from 'object'
  or p->>'document_sha256' is null or length(p->>'raw_gzip_base64') not between 1 and 2500000
  or jsonb_typeof(p->'source_urls') is distinct from 'array' then raise exception 'Invalid macro source receipt';end if;
 if (p->>'provider'='nyfed_sofr' and p->'source_urls'<>jsonb_build_array('https://markets.newyorkfed.org/api/rates/secured/sofr/last/3.json'))
 or (p->>'provider'='ofr_fsi' and p->'source_urls'<>jsonb_build_array('https://www.financialresearch.gov/financial-stress-index/data/fsi.csv'))
 or (p->>'provider'='bls_employment' and p->'source_urls'<>jsonb_build_array('https://api.bls.gov/publicAPI/v1/timeseries/data/CES0000000001','https://api.bls.gov/publicAPI/v1/timeseries/data/LNS14000000','https://api.bls.gov/publicAPI/v1/timeseries/data/CES0500000003')) then raise exception 'Unapproved macro origin';end if;
 clean:=(p-'raw_gzip_base64')||jsonb_build_object('captured_at',n,'usable_at',n);
 insert into public.market_macro_captures(provider,source_as_of,document_sha256,payload,raw_gzip_base64)
 values(p->>'provider',p->>'source_as_of',p->>'document_sha256',clean,p->>'raw_gzip_base64')
 on conflict(provider,document_sha256) do nothing returning * into rec;
 if not found then select * into strict rec from public.market_macro_captures where provider=p->>'provider' and document_sha256=p->>'document_sha256';end if;
 insert into private.market_macro_check_results(check_id,status,capture_id)values(p_check_id,'CAPTURED',rec.id) on conflict(check_id)do nothing;
 return jsonb_build_object('id',rec.id,'captured_at',rec.captured_at,'payload',rec.payload);
end $$;
create function public.market_macro_capture_v1(p jsonb,p_check_id bigint)returns jsonb language sql security invoker set search_path='' as $$select private.market_macro_capture_v1(p,p_check_id)$$;
create function private.market_macro_failure_v1(p_check_id bigint)returns jsonb language plpgsql security definer set search_path='' as $$begin
 insert into private.market_macro_check_results(check_id,status)values(p_check_id,'UNAVAILABLE')on conflict(check_id)do nothing;
 return jsonb_build_object('status','UNAVAILABLE');end $$;
create function public.market_macro_failure_v1(p_check_id bigint)returns jsonb language sql security invoker set search_path='' as $$select private.market_macro_failure_v1(p_check_id)$$;

create function private.market_record_risk_brief_v1(p jsonb) returns jsonb language plpgsql security definer set search_path='' as $$
declare rec public.market_risk_briefs;today date:=(clock_timestamp() at time zone 'America/New_York')::date;begin
 if p->>'method_version' is distinct from 'source-bound-market-risk-v1' or p->'execution_enabled' is distinct from 'false'::jsonb or p->>'report_type' is distinct from 'MARKET_RISK_INTELLIGENCE_NOT_FORECAST'
 or jsonb_typeof(p->'evidence')is distinct from 'array' or p ? 'positions' or p ? 'portfolio' then raise exception 'Invalid public-source risk report';end if;
 insert into public.market_risk_briefs(method_version,source_signature,payload)values(p->>'method_version',p->>'source_signature',p||jsonb_build_object('recorded_at',clock_timestamp()))
 on conflict(brief_date,method_version,source_signature)do nothing returning * into rec;
 if not found then select * into strict rec from public.market_risk_briefs where brief_date=today and method_version=p->>'method_version' and source_signature=p->>'source_signature';end if;
 return to_jsonb(rec);
end $$;
create function public.market_record_risk_brief_v1(p jsonb)returns jsonb language sql security invoker set search_path='' as $$select private.market_record_risk_brief_v1(p)$$;

create function private.market_finance_authorize_v1(p_hash text)returns jsonb language sql stable security definer set search_path='' as $$
 select jsonb_build_object('configured',exists(select 1 from private.market_finance_keys where name='owner_finance'),
 'valid',p_hash~'^[a-f0-9]{64}$' and exists(select 1 from private.market_finance_keys where name='owner_finance' and key_sha256=p_hash))
$$;
create function public.market_finance_authorize_v1(p_hash text)returns jsonb language sql stable security invoker set search_path='' as $$select private.market_finance_authorize_v1(p_hash)$$;

create function private.market_portfolio_save_v1(p jsonb,p_key text)returns jsonb language plpgsql security definer set search_path='' as $$
declare rec private.market_portfolio_snapshots;begin
 if length(p_key)not between 8 and 160 or jsonb_typeof(p->'positions')is distinct from 'array' or jsonb_array_length(p->'positions')>100
 or p->>'currency' is distinct from 'USD' or (p->>'as_of')::timestamptz>clock_timestamp()+interval '5 minutes' then raise exception 'Invalid private portfolio contract';end if;
 insert into private.market_portfolio_snapshots(record_key,as_of,payload)values(p_key,(p->>'as_of')::timestamptz,p)on conflict(record_key)do nothing returning * into rec;
 if not found then
  select * into strict rec from private.market_portfolio_snapshots where record_key=p_key;
  if rec.payload<>p then raise exception 'Immutable portfolio request conflict';end if;
 end if;
 return jsonb_build_object('id',rec.id,'created_at',rec.created_at,'as_of',rec.as_of,'portfolio',rec.payload);
end $$;
create function public.market_portfolio_save_v1(p jsonb,p_key text)returns jsonb language sql security invoker set search_path='' as $$select private.market_portfolio_save_v1(p,p_key)$$;
create function private.market_portfolio_latest_v1()returns jsonb language sql stable security definer set search_path='' as $$
 select coalesce((select jsonb_build_object('id',id,'created_at',created_at,'as_of',as_of,'portfolio',payload)from private.market_portfolio_snapshots order by id desc limit 1),'{}'::jsonb)
$$;
create function public.market_portfolio_latest_v1()returns jsonb language sql stable security invoker set search_path='' as $$select private.market_portfolio_latest_v1()$$;

create function private.market_finance_report_v1(p_ticker text,p_payload jsonb default null)returns jsonb language plpgsql security definer set search_path='' as $$
declare rec private.market_finance_research_reports;begin
 if p_ticker!~'^[A-Z][A-Z0-9.-]{0,11}$' then raise exception 'Invalid research instrument';end if;
 if p_payload is not null then
  if p_payload->>'ticker' is distinct from p_ticker or p_payload->'execution_enabled' is distinct from 'false'::jsonb then raise exception 'Invalid private research result';end if;
  insert into private.market_finance_research_reports(ticker,payload)values(p_ticker,p_payload)returning * into rec;
 else select * into rec from private.market_finance_research_reports where ticker=p_ticker order by id desc limit 1;end if;
 if rec.id is null then return '{}'::jsonb;end if;
 return jsonb_build_object('id',rec.id,'captured_at',rec.captured_at,'report',rec.payload);
end $$;
create function public.market_finance_report_v1(p_ticker text,p_payload jsonb default null)returns jsonb language sql security invoker set search_path='' as $$select private.market_finance_report_v1(p_ticker,p_payload)$$;
create function private.market_portfolio_analysis_v1(p_snapshot_id bigint,p_payload jsonb)returns jsonb language plpgsql security definer set search_path='' as $$
declare rec private.market_portfolio_analyses;begin
 if p_payload->'execution_enabled' is distinct from 'false'::jsonb then raise exception 'Financial execution forbidden';end if;
 insert into private.market_portfolio_analyses(snapshot_id,payload)values(p_snapshot_id,p_payload)returning * into rec;
 return jsonb_build_object('id',rec.id,'snapshot_id',rec.snapshot_id,'generated_at',rec.generated_at,'analysis',rec.payload);
end $$;
create function public.market_portfolio_analysis_v1(p_snapshot_id bigint,p_payload jsonb)returns jsonb language sql security invoker set search_path='' as $$select private.market_portfolio_analysis_v1(p_snapshot_id,p_payload)$$;

do $$declare f record;begin
 for f in select oid::regprocedure sig from pg_proc where pronamespace in ('public'::regnamespace,'private'::regnamespace)and proname in(
 'market_macro_claim_v1','market_macro_capture_v1','market_macro_failure_v1','market_record_risk_brief_v1','market_finance_authorize_v1','market_portfolio_save_v1','market_portfolio_latest_v1','market_finance_report_v1','market_portfolio_analysis_v1')loop
 execute format('revoke all on function %s from public,anon,authenticated',f.sig);execute format('grant execute on function %s to service_role',f.sig);
 end loop;
end $$;
notify pgrst,'reload schema';
