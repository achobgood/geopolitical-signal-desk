"""Bounded official public aggregates, independent of licensed equity feeds.

Reference periods are not publication timestamps. Capture current vintages only;
never use subsequently revised history as a past point-in-time feature snapshot.
"""
from __future__ import annotations
import asyncio, base64, csv, gzip, hashlib, io, json, logging, time
from datetime import datetime, timezone, date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
import httpx
from app.market_catalysts import CALENDAR_PROVIDERS,parse_fomc,parse_bea_calendar

BLS_SERIES={'payroll':'CES0000000001','unemployment':'LNS14000000','hourly_earnings':'CES0500000003'}
BLS_BASE='https://api.bls.gov/publicAPI/v1/timeseries/data/'
SOFR_URL='https://markets.newyorkfed.org/api/rates/secured/sofr/last/3.json'
OFR_URL='https://www.financialresearch.gov/financial-stress-index/data/fsi.csv'
ALLOWED={BLS_BASE,SOFR_URL,OFR_URL}|set(CALENDAR_PROVIDERS.values())
PROVIDERS=('bls_employment','nyfed_sofr','ofr_fsi',*CALENDAR_PROVIDERS)
NYFED_TERMS='https://www.newyorkfed.org/privacy/termsofuse'
NYFED_NOTICE=(
 'Source: Federal Reserve Bank of New York. '
 'SOFR data is subject to the Terms of Use posted at newyorkfed.org. The New York Fed is not responsible for publication of SOFR data by Signal Desk, '
 'does not sanction or endorse any particular republication, and has no liability for your use. '
 'Signal Desk is not affiliated with the New York Fed. The New York Fed does not sanction, endorse, or recommend any products or services offered by Signal Desk. '
 'SOFR data are calculated using data provided under license to the New York Fed by DTCC Solutions LLC; DTCC Solutions, its affiliates and source third parties have no liability for this material. '
 'Signal Desk calculations are its own transformations.')
logger=logging.getLogger(__name__)


class OfficialSourceDeclined(ValueError):
    """Keep a bounded provider status, never its arbitrary response message."""
    def __init__(self,status,messages=None):
        self.provider_status=status if status in ('REQUEST_NOT_PROCESSED','REQUEST_FAILED','REQUEST_NOT_REGISTERED','REQUEST_INVALID') else 'UNKNOWN_STATUS'
        text=' '.join(x[:500].lower()for x in (messages or [])[:8]if isinstance(x,str)) if isinstance(messages,list)else''
        self.reason_code='DAILY_QUOTA'if 'daily'in text and ('threshold'in text or 'limit'in text)else 'ACCESS_POLICY'if 'blocked'in text else None
        super().__init__('Official source declined request')


def failure_diagnostic(error):
    """Safe operational metadata only: no response body, headers, or error text."""
    if isinstance(error,httpx.HTTPStatusError):return {'category':'UPSTREAM_HTTP','http_status':error.response.status_code}
    if isinstance(error,OfficialSourceDeclined):return {'category':'PROVIDER_DECLINED','provider_status':error.provider_status,**({'reason_code':error.reason_code}if error.reason_code else{})}
    if isinstance(error,(asyncio.TimeoutError,httpx.TimeoutException)):return {'category':'TIMEOUT'}
    if isinstance(error,httpx.HTTPError):return {'category':'TRANSPORT'}
    if isinstance(error,json.JSONDecodeError):return {'category':'INVALID_JSON'}
    return {'category':'SOURCE_VALIDATION'}


def number(v,minimum='-1e12',maximum='1e12'):
    try:
        n=Decimal(str(v).replace(',',''))
    except (InvalidOperation,TypeError,ValueError) as exc:raise ValueError('Invalid official numeric value') from exc
    if not n.is_finite() or not Decimal(minimum)<=n<=Decimal(maximum):raise ValueError('Official numeric value outside range')
    return n


async def official_bytes(url,*,method='GET',json_body=None):
    if url not in ALLOWED or urlsplit(url).scheme!='https':raise ValueError('Unreviewed macro source URL')
    if method!=('POST'if url==BLS_BASE else'GET'):raise ValueError('Unreviewed official request method')
    if url==BLS_BASE and (not isinstance(json_body,dict)or set(json_body)!={'seriesid','startyear','endyear'}or json_body['seriesid']!=list(BLS_SERIES.values())or not all(str(json_body[x]).isdigit()and len(str(json_body[x]))==4 for x in ('startyear','endyear'))or int(json_body['endyear'])-int(json_body['startyear'])!=1):raise ValueError('Unreviewed BLS series request')
    async with asyncio.timeout(24):
        async with httpx.AsyncClient(timeout=15,follow_redirects=False,trust_env=False) as c:
            async with c.stream(method,url,json=json_body,headers={'User-Agent':'SignalDeskPublicMacroResearch/1.0','Accept':'application/json,text/csv,text/html,text/calendar'}) as r:
                r.raise_for_status();raw=bytearray()
                async for part in r.aiter_bytes():
                    raw.extend(part)
                    if len(raw)>1_800_000:raise ValueError('Macro response exceeds bound')
                return bytes(raw),dict(r.headers)


def parse_bls(documents, *, captured_at):
    latest={};sources=[];raw={}
    for key,series_id in BLS_SERIES.items():
        doc=documents[key]
        if doc.get('status')!='REQUEST_SUCCEEDED':raise OfficialSourceDeclined(doc.get('status'),doc.get('message'))
        series=(doc.get('Results') or {}).get('series') or []
        if len(series)!=1 or series[0].get('seriesID')!=series_id:raise ValueError('BLS series identity mismatch')
        rows=[]
        for item in series[0].get('data',[]):
            period=item.get('period','')
            if len(period)!=3 or not period.startswith('M') or not period[1:].isdigit() or not 1<=int(period[1:])<=12:continue
            year=int(item['year']);month=int(period[1:]);ref=f'{year:04d}-{month:02d}'
            if ref>=captured_at.strftime('%Y-%m'):continue
            missing=str(item.get('value','')).strip() in ('','-','NA','N/A')
            val=None if missing else number(item['value'],'0',{'unemployment':'100','hourly_earnings':'1000','payroll':'1e6'}[key])
            rows.append({'reference_period':ref,'value':str(val) if val is not None else None,'status':'MISSING' if missing else 'OBSERVED','footnotes':item.get('footnotes',[])})
        rows.sort(key=lambda x:x['reference_period'])
        if len(rows)<2:raise ValueError('Insufficient completed monthly BLS observations')
        previous,current=rows[-2:]
        if previous['value'] is None or current['value'] is None:raise ValueError('Latest consecutive BLS reference months are incomplete; no older pair substituted')
        prior_month=date(int(current['reference_period'][:4]),int(current['reference_period'][5:]),1).replace(day=1)
        last_of_previous=prior_month.fromordinal(prior_month.toordinal()-1).strftime('%Y-%m')
        if previous['reference_period']!=last_of_previous:raise ValueError('Missing previous BLS reference month')
        latest[key]=(current,previous);sources.append(BLS_BASE+series_id);raw[key]=rows
    periods={x[0]['reference_period'] for x in latest.values()}
    if len(periods)!=1:raise ValueError('BLS series reference months disagree')
    payroll,previous=latest['payroll'];earnings,prior_earnings=latest['hourly_earnings'];rate=latest['unemployment'][0]
    wage_growth=(number(earnings['value'])/number(prior_earnings['value'])-1)*100 if number(prior_earnings['value'])>0 else None
    return {'provider':'bls_employment','source_urls':[BLS_BASE],'series_reference_urls':sources,'requested_series_ids':list(BLS_SERIES.values()),'reference_period':periods.pop(),
     'source_as_of':payroll['reference_period'],'published_at':None,'publication_basis':'API does not provide a publication timestamp; monthly reference period is not a release time',
     'data':{'payroll_change_jobs':str((number(payroll['value'])-number(previous['value']))*1000),
      'payroll_level_thousands':payroll['value'],'previous_payroll_level_thousands':previous['value'],
      'unemployment_pct':rate['value'],'hourly_earnings_usd':earnings['value'],
      'hourly_earnings_monthly_change_pct':str(wage_growth.quantize(Decimal('.1'))) if wage_growth is not None else None,
      'hourly_earnings_change_unrounded_pct':str(wage_growth) if wage_growth is not None else None,
      'preliminary':any(x.get('code')=='P' for pair in latest.values() for row in pair for x in row['footnotes'])},
     'calculation':'Payroll level difference in thousands ×1000; wage percentage uses same-month aligned series and is rounded to 0.1 percentage point',
     'history':raw,'limitations':['Current revised API vintage; historical rows are not previously captured forecasts.','Monthly employment evidence is not a daily market price.']}


def parse_sofr(doc, *, captured_at):
    rows=doc.get('refRates')
    if not isinstance(rows,list) or not 1<=len(rows)<=10:raise ValueError('Missing NY Fed reference rates')
    valid=[]
    for row in rows:
        if row.get('type')!='SOFR':raise ValueError('Unexpected reference rate type')
        day=date.fromisoformat(row['effectiveDate'])
        if day>captured_at.date():raise ValueError('Future effective SOFR date')
        valid.append({'effective_date':str(day),'rate_pct':str(number(row['percentRate'],'-5','50')),
          'volume_usd_billions':str(number(row['volumeInBillions'],'0','1e6')),
          'revision_indicator':row.get('revisionIndicator','')})
    valid.sort(key=lambda x:x['effective_date'])
    if len({x['effective_date'] for x in valid})!=len(valid):raise ValueError('Duplicate SOFR effective dates')
    last=valid[-1];change=(number(last['rate_pct'])-number(valid[-2]['rate_pct']))*100 if len(valid)>1 else None
    return {'provider':'nyfed_sofr','source_urls':[SOFR_URL],'source_as_of':last['effective_date'],
      'published_at':None,'publication_basis':'Effective date supplied; exact publication timestamp absent. First collection is the conservative usable time.',
      'data':{**last,'change_bp':str(change) if change is not None else None},'history':valid,
      'attribution':NYFED_NOTICE,'terms_url':NYFED_TERMS,
      'limitations':['Prior-business-day secured funding rate, not an issuer credit spread.','Possible same-day revision; each captured vintage is retained.']}


def parse_ofr(blob, *, captured_at):
    rows=list(csv.DictReader(io.StringIO(blob.decode('utf-8-sig'))));valid=[]
    if not rows or len(rows)>20000:raise ValueError('Invalid OFR aggregate CSV size')
    def take(row,*names):
        for n in names:
            if n in row:return row[n]
        raise ValueError('OFR required aggregate column missing')
    for row in rows:
        day=date.fromisoformat(take(row,'Date','date'))
        if day>captured_at.date():raise ValueError('Future OFR date')
        record={'date':str(day)}
        for out,names in {'fsi':('OFR FSI','OFR FSI Index','OFRFSI'),'credit':('Credit',),'funding':('Funding',),'volatility':('Volatility',)}.items():
            record[out]=str(number(take(row,*names),'-100','100'))
        valid.append(record)
    valid.sort(key=lambda x:x['date'])
    if len({x['date'] for x in valid})!=len(valid):raise ValueError('Duplicate OFR dates')
    last=valid[-1];delta=number(last['fsi'])-number(valid[-2]['fsi']) if len(valid)>1 else None
    return {'provider':'ofr_fsi','source_urls':[OFR_URL],'source_as_of':last['date'],
      'published_at':None,'publication_basis':'Published aggregate CSV; exact row publication time absent; typical lag is two business days',
      'data':{**last,'change_index_points':str(delta) if delta is not None else None},'history':valid[-12:],
      'attribution':'Source: Office of Financial Research, Financial Stress Index. Published aggregate contributions only.',
      'terms_url':'https://www.financialresearch.gov/legal-notices/',
      'limitations':['Typical two-business-day lag. An observation predating a selloff cannot establish conditions after that selloff.','Aggregate credit contribution is an index contribution, not a basis-point CDS or ICE/BofA spread.']}


async def fetch_macro(provider):
    captured=datetime.now(timezone.utc)
    if provider=='bls_employment':
        raw,headers=await official_bytes(BLS_BASE,method='POST',json_body={'seriesid':list(BLS_SERIES.values()),'startyear':str(captured.year-1),'endyear':str(captured.year)})
        doc=json.loads(raw)
        if not isinstance(doc,dict):raise ValueError('Invalid BLS batch envelope')
        if doc.get('status')!='REQUEST_SUCCEEDED':raise OfficialSourceDeclined(doc.get('status'),doc.get('message'))
        results=doc.get('Results')
        if not isinstance(results,dict):raise ValueError('Invalid BLS batch results')
        series=results.get('series')or[]
        if not isinstance(series,list)or any(not isinstance(x,dict)for x in series):raise ValueError('Invalid BLS batch series')
        if len(series)!=len(BLS_SERIES)or {x.get('seriesID')for x in series}!=set(BLS_SERIES.values()):raise ValueError('BLS batch series identities disagree')
        by_id={x['seriesID']:x for x in series}
        documents={key:{'status':doc['status'],'Results':{'series':[by_id[sid]]}}for key,sid in BLS_SERIES.items()}
        result=parse_bls(documents,captured_at=captured)
    elif provider=='nyfed_sofr':
        raw,headers=await official_bytes(SOFR_URL);result=parse_sofr(json.loads(raw),captured_at=captured)
    elif provider=='ofr_fsi':
        raw,headers=await official_bytes(OFR_URL);result=parse_ofr(raw,captured_at=captured)
    elif provider in CALENDAR_PROVIDERS:
        raw,headers=await official_bytes(CALENDAR_PROVIDERS[provider]);result=(parse_fomc if provider=='fed_fomc_calendar'else parse_bea_calendar)(raw,captured_at=captured)
    else:raise ValueError('Unreviewed macro provider')
    result['captured_at']=datetime.now(timezone.utc).isoformat();result['usable_at']=result['captured_at']
    result['document_sha256']=hashlib.sha256(raw).hexdigest()
    result['raw_gzip_base64']=base64.b64encode(gzip.compress(raw)).decode()
    return result


async def refresh_macro(store, *, force=False):
    results=[]
    for provider in PROVIDERS:
        claim=await store.rpc('market_macro_claim_v1',{'p_provider':provider,'p_force':force})
        if not claim['claimed']:
            results.append({'provider':provider,'status':'CACHED'if claim.get('capture_id')else'NOT_REFRESHED_NO_CAPTURE','capture_id':claim.get('capture_id')});continue
        started=time.monotonic()
        try:
            payload=await fetch_macro(provider)
            result=await store.rpc('market_macro_capture_v1',{'p':payload,'p_check_id':claim['check_id']})
            results.append({'provider':provider,'status':'CAPTURED','capture_id':result['id']})
        except (httpx.HTTPError,asyncio.TimeoutError,ValueError,KeyError,TypeError,ZeroDivisionError) as error:
            diagnostic=failure_diagnostic(error)
            logger.warning('macro_source_unavailable provider=%s check_id=%s diagnostic=%s elapsed_ms=%s',provider,claim['check_id'],json.dumps(diagnostic,sort_keys=True),round((time.monotonic()-started)*1000))
            await store.rpc('market_macro_failure_v1',{'p_check_id':claim['check_id']})
            results.append({'provider':provider,'status':'UNAVAILABLE','reason':'Official source response was unavailable or invalid; no substituted data','diagnostic':diagnostic})
    return results
