-- SEC source-linked annual issuer fundamentals. No private brokerage data.
-- Written by the authenticated server ONLY after official SEC companyfacts retrieval.
create table if not exists public.market_fundamental_snapshots (
 id bigint generated always as identity primary key,
 request_key text not null unique,
 ticker text not null check(ticker in ('NVDA','MSFT','GOOGL','AMZN','META','ORCL','CRWV','AMD')),
 cik bigint not null check(cik between 1 and 9999999999),
 issuer_name text not null,
 method_version text not null check(method_version='sec-us-gaap-same-filing-annual-v1'),
 source_url text not null,
 source_document_sha256 text not null check(source_document_sha256 ~ '^[a-f0-9]{64}$'),
 source_first_collected_at timestamptz not null,
 created_at timestamptz not null default now(),
 accession text not null,
 report_start date not null,
 report_end date not null,
 filed_date date not null,
 metrics jsonb not null check(jsonb_typeof(metrics)='object'),
 missing jsonb not null check(jsonb_typeof(missing)='object'),
 status text not null check(status in ('ANALYZED','ANALYZED_WITH_GAPS')),
 check(report_end >= report_start and report_end <= filed_date),
 check(source_first_collected_at >= filed_date::timestamptz),
 check(length(accession)=20)
);
create index if not exists market_fundamental_recent
 on public.market_fundamental_snapshots(created_at desc,id desc);
create index if not exists market_fundamental_ticker
 on public.market_fundamental_snapshots(ticker,report_end desc,id desc);
alter table public.market_fundamental_snapshots enable row level security;
revoke all on public.market_fundamental_snapshots from public,anon,authenticated,service_role;
grant select,insert on public.market_fundamental_snapshots to service_role;
grant usage,select on sequence public.market_fundamental_snapshots_id_seq to service_role;

create or replace function public.market_record_fundamentals_v1(payload jsonb,p_key text)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare
 v_cik bigint := nullif(payload->>'cik','')::bigint;
 v_captured timestamptz := nullif(payload->>'source_collected_at','')::timestamptz;
 v_report jsonb := payload->'annual_filing';
 v_metrics jsonb := payload->'metrics';
 v_missing jsonb := payload->'missing';
 v_record public.market_fundamental_snapshots;
 v_created boolean := false;
 v_expected_source text;
 v_ocf numeric;
 v_capex numeric;
 v_fcf numeric;
begin
 if coalesce(p_key,'') !~ '^[a-f0-9]{64}$'
    or v_cik not between 1 and 9999999999
    or coalesce(payload->>'ticker','') not in ('NVDA','MSFT','GOOGL','AMZN','META','ORCL','CRWV','AMD')
    or payload->>'method_version' <> 'sec-us-gaap-same-filing-annual-v1'
    or payload->>'status' not in ('ANALYZED','ANALYZED_WITH_GAPS')
    or v_captured is null
    or v_captured < now()-interval '10 minutes'
    or v_captured > now()+interval '3 minutes'
    or v_report is null or jsonb_typeof(v_report)<>'object'
    or v_metrics is null or jsonb_typeof(v_metrics)<>'object'
    or v_missing is null or jsonb_typeof(v_missing)<>'object'
    or coalesce(length(v_report->>'accession'),0) <> 20
    or (v_report->>'start')::date > (v_report->>'end')::date
    or (v_report->>'end')::date > v_captured::date
    or (v_report->>'filed')::date > v_captured::date
 then
  raise exception 'Invalid SEC fundamental source receipt';
 end if;
 v_expected_source := 'https://data.sec.gov/api/xbrl/companyfacts/CIK'
   ||lpad(v_cik::text,10,'0')||'.json';
 if payload->>'source_url' <> v_expected_source
   or coalesce(payload->>'source_document_sha256','') !~ '^[a-f0-9]{64}$'
 then raise exception 'Unrecognized official SEC companyfacts source';
 end if;
 -- A derived FCF number must match the exact same recorded annual concepts.
 if jsonb_typeof(v_metrics->'free_cash_flow_proxy')='object' then
  if jsonb_typeof(v_metrics->'operating_cash_flow')<>'object'
     or jsonb_typeof(v_metrics->'capital_expenditure')<>'object'
  then raise exception 'Derived free cash flow missing component evidence'; end if;
  v_ocf:=(v_metrics->'operating_cash_flow'->>'value')::numeric;
  v_capex:=(v_metrics->'capital_expenditure'->>'value')::numeric;
  v_fcf:=(v_metrics->'free_cash_flow_proxy'->>'value')::numeric;
  if v_ocf-v_capex is distinct from v_fcf
    or v_capex < 0
    or (v_metrics->'operating_cash_flow'->>'accession') is distinct from v_report->>'accession'
    or (v_metrics->'capital_expenditure'->>'accession') is distinct from v_report->>'accession'
    or (v_metrics->'operating_cash_flow'->>'start') is distinct from v_report->>'start'
    or (v_metrics->'operating_cash_flow'->>'end') is distinct from v_report->>'end'
    or (v_metrics->'capital_expenditure'->>'start') is distinct from v_report->>'start'
    or (v_metrics->'capital_expenditure'->>'end') is distinct from v_report->>'end'
  then raise exception 'Free cash flow components do not match same annual source'; end if;
 end if;
 insert into public.market_fundamental_snapshots(
  request_key,ticker,cik,issuer_name,method_version,source_url,
  source_document_sha256,source_first_collected_at,accession,
  report_start,report_end,filed_date,metrics,missing,status
 ) values(
  p_key,payload->>'ticker',v_cik,left(coalesce(payload->>'issuer_name',''),150),
  payload->>'method_version',v_expected_source,payload->>'source_document_sha256',
  v_captured,v_report->>'accession',
  (v_report->>'start')::date,(v_report->>'end')::date,
  (v_report->>'filed')::date,v_metrics,v_missing,payload->>'status'
 )
 on conflict(request_key) do nothing returning * into v_record;
 if found then
  v_created:=true;
  insert into public.audit_events(actor,action,entity_type,entity_id,request_key)
  values('collector:sec_companyfacts','record_fiscal_fundamentals','market_fundamental_snapshot',
    v_record.id,p_key);
 else
  select * into v_record from public.market_fundamental_snapshots where request_key=p_key;
 end if;
 return jsonb_build_object(
  'created',v_created, 'snapshot_id',v_record.id,
  'first_collected_at',v_record.source_first_collected_at,
  'source_sha256',v_record.source_document_sha256,
  'orders_placed',false
 );
end;
$$;
revoke all on function public.market_record_fundamentals_v1(jsonb,text)
 from public,anon,authenticated;
grant execute on function public.market_record_fundamentals_v1(jsonb,text) to service_role;
notify pgrst,'reload schema';
