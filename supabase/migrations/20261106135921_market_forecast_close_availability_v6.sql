-- Wait beyond the 16-minute delayed-SIP boundary before accepting today's
-- consolidated daily close. At 16:15 ET the requested end is still 15:59;
-- issuance/capture start at 16:30 ET and the 19:00 ET lock remains authoritative.
create or replace function private.market_capture_forecast_prices_v1(p jsonb) returns jsonb language plpgsql security definer set search_path='' as $$
declare rec public.market_forecast_prices; b jsonb; prev date; ds date; last_session date; begin
 if p->>'symbol' not in ('SPY','QQQ') or p->>'provider'<>'alpaca' or p->>'feed'<>'sip' or p->>'adjustment'<>'split'
    or p->>'source_url'<>'https://data.alpaca.markets/v2/stocks/'||(p->>'symbol')||'/bars'
    or jsonb_typeof(p->'input_bars')<>'array' or jsonb_array_length(p->'input_bars') not between 60 and 1500 then
  raise exception 'Invalid consolidated price provenance';
 end if;
 for b in select value from jsonb_array_elements(p->'input_bars') loop
  if not (b ?& array['session','open','high','low','close']) or
     b->>'session' is null or b->>'open' is null or b->>'high' is null or b->>'low' is null or b->>'close' is null
     or (b->>'open') !~ '^[0-9]+([.][0-9]+)?$' or (b->>'high') !~ '^[0-9]+([.][0-9]+)?$'
     or (b->>'low') !~ '^[0-9]+([.][0-9]+)?$' or (b->>'close') !~ '^[0-9]+([.][0-9]+)?$' then raise exception 'Missing/non-numeric OHLC/session'; end if;
  ds:=(b->>'session')::date;
  if extract(year from ds) between 2026 and 2028 and not public.market_is_session_v1(ds) then raise exception 'Exchange holiday bar'; end if;
  if prev is not null and extract(year from prev) between 2026 and 2028 and public.market_next_session_v1(prev)<>ds then raise exception 'Missing completed exchange session'; end if;
  if prev is not null and ds<=prev then raise exception 'Unordered/duplicate session'; end if;
  if extract(isodow from ds)>5 or (b->>'low')::numeric<=0 or (b->>'high')::numeric<(b->>'low')::numeric
   or (b->>'open')::numeric not between (b->>'low')::numeric and (b->>'high')::numeric
   or (b->>'close')::numeric not between (b->>'low')::numeric and (b->>'high')::numeric
   or (b->>'close')::numeric::text in ('NaN','Infinity','-Infinity') then raise exception 'Invalid daily OHLC'; end if;
  prev:=ds;
 end loop;
 last_session:=prev;
 if last_session >= (clock_timestamp() at time zone 'America/New_York')::date and
    (clock_timestamp() at time zone 'America/New_York')::time<time '16:30' then raise exception 'Incomplete current session'; end if;
 if last_session>(clock_timestamp() at time zone 'America/New_York')::date then raise exception 'Future source session'; end if;
 insert into public.market_forecast_prices(symbol,provider,feed,adjustment,source_url,observed_session,bars_sha256,input_bars,quality)
 values(p->>'symbol','alpaca','sip','split',p->>'source_url',last_session,p->>'bars_sha256',p->'input_bars',p->'quality')
 on conflict(symbol,bars_sha256) do nothing returning * into rec;
 if not found then select * into rec from public.market_forecast_prices where symbol=p->>'symbol' and bars_sha256=p->>'bars_sha256'; end if;
 return to_jsonb(rec);
end $$;

create or replace function private.market_issue_forecast_v1(p jsonb) returns jsonb language plpgsql security definer set search_path='' as $$
declare t timestamptz:=clock_timestamp(); local_now timestamp; ref date; target date; cut timestamptz;
 prices public.market_forecast_prices; rec public.market_forecasts; base numeric; prior numeric; expected_direction text; begin
 local_now:=t at time zone 'America/New_York'; ref:=local_now::date;
 if not public.market_is_session_v1(ref) or local_now::time not between time '16:30' and time '19:00' then
  raise exception 'Outside prior trading day 16:30–19:00 ET issuance window; no backdating'; end if;
 target:=public.market_next_session_v1(ref); cut:=(ref+time '19:00') at time zone 'America/New_York';
 if (p->>'reference_session')::date<>ref or (p->>'target_session')::date<>target or p->>'model_version'<>'momentum-1-research-v1'
   or coalesce((p->>'selected_strategy')::boolean,true) or p->>'probability' is not null then raise exception 'Forecast target/model contract mismatch'; end if;
 if p->>'reference_capture_id' is not null then
  select * into strict prices from public.market_forecast_prices where id=(p->>'reference_capture_id')::bigint;
  if prices.symbol<>p->>'symbol' or prices.observed_session<>ref or prices.captured_at>t or prices.captured_at>cut or prices.captured_at<((ref+time '16:30') at time zone 'America/New_York') then raise exception 'Unavailable/stale reference'; end if;
  base:=(prices.input_bars->-1->>'close')::numeric;
  prior:=(prices.input_bars->-2->>'close')::numeric;
  expected_direction:=case when base>prior then 'UP' when base<prior then 'DOWN' else 'ABSTAIN' end;
  if p->>'direction' is distinct from expected_direction or p->'benchmarks'->>'always_up' is distinct from 'UP' or p->'benchmarks'->>'momentum' is distinct from expected_direction
   or (p->'features'->>'source_price_capture_id')::bigint is distinct from prices.id
   or (p->'features'->>'usable_at')::timestamptz is distinct from prices.captured_at
   then raise exception 'Model direction/features do not reproduce the immutable price source'; end if;
 elsif p->>'direction'<>'ABSTAIN' or jsonb_array_length(p->'abstention_reasons')<1 then raise exception 'Missing price must abstain with reason'; end if;
 insert into public.market_forecasts(symbol,issued_at,target_session,reference_session,cutoff_at,reference_capture_id,reference_close,
 model_version,code_hash,config_hash,configuration,direction,probability,features,benchmarks,abstention_reasons,supporting_evidence,counterevidence,missing_inputs,invalidation)
 values(p->>'symbol',t,target,ref,cut,prices.id,base,p->>'model_version',p->>'code_hash',p->>'config_hash',p->'configuration',p->>'direction',null,
 p->'features',p->'benchmarks',p->'abstention_reasons',p->'supporting_evidence',p->'counterevidence',p->'missing_inputs',p->'invalidation')
 on conflict(symbol,target_session,model_version) do nothing returning * into rec;
 if not found then select * into rec from public.market_forecasts where symbol=p->>'symbol' and target_session=target and model_version=p->>'model_version'; end if;
 return to_jsonb(rec);
end $$;
revoke all on function private.market_capture_forecast_prices_v1(jsonb) from public,anon,authenticated;
revoke all on function private.market_issue_forecast_v1(jsonb) from public,anon,authenticated;
grant execute on function private.market_capture_forecast_prices_v1(jsonb) to service_role;
grant execute on function private.market_issue_forecast_v1(jsonb) to service_role;
notify pgrst,'reload schema';
