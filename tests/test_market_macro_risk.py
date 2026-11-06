import asyncio,copy
from datetime import datetime,timezone
import pytest
from app.market_macro import parse_bls,parse_sofr,parse_ofr,BLS_SERIES
from app.market_risk import build_brief
NOW=datetime(2026,11,6,16,tzinfo=timezone.utc)
def bls():
 values={'payroll':('159293','159128'),'unemployment':('4.1','4.2'),'hourly_earnings':('38.00','37.86')}
 return {k:{'status':'REQUEST_SUCCEEDED','Results':{'series':[{'seriesID':sid,'data':[{'year':'2026','period':'M10','value':values[k][0],'footnotes':[{'code':'P','text':'preliminary'}]},{'year':'2026','period':'M09','value':values[k][1],'footnotes':[]},{'year':'2025','period':'M10','value':'-','footnotes':[{'code':'9','text':'Data unavailable'}]}]}]}}for k,sid in BLS_SERIES.items()}
def test_monthly_source_units_rounding_and_missing_history_are_explicit():
 r=parse_bls(bls(),captured_at=NOW)
 assert r['data']['payroll_change_jobs']=='165000'
 assert r['data']['unemployment_pct']=='4.1'
 assert r['data']['hourly_earnings_monthly_change_pct']=='0.4'
 assert r['data']['preliminary']is True and r['published_at']is None
 assert r['history']['unemployment'][0]['value']is None
@pytest.mark.parametrize('field',['payroll','unemployment','hourly_earnings'])
def test_latest_month_missing_never_substitutes_an_older_pair(field):
 d=bls();d[field]['Results']['series'][0]['data'][0]['value']='-'
 with pytest.raises(ValueError):parse_bls(d,captured_at=NOW)
def test_sofr_is_dated_secured_rate_with_revision_and_attribution():
 d={'refRates':[{'type':'SOFR','effectiveDate':'2026-11-05','percentRate':3.92,'volumeInBillions':3256,'revisionIndicator':'*'},{'type':'SOFR','effectiveDate':'2026-11-04','percentRate':3.94,'volumeInBillions':3200,'revisionIndicator':''}]}
 r=parse_sofr(d,captured_at=NOW);assert r['data']['change_bp']=='-2.00' and r['data']['revision_indicator']=='*'
 assert r['terms_url'] and 'Federal Reserve Bank of New York'in r['attribution']
 d['refRates'][0]['effectiveDate']='2027-01-01'
 with pytest.raises(ValueError):parse_sofr(d,captured_at=NOW)
def test_ofr_lagged_aggregate_is_not_a_current_credit_spread():
 r=parse_ofr(b'Date,OFR FSI,Credit,Funding,Volatility\n2026-11-02,-1.472,-.8,-.1,.2\n2026-11-03,-1.484,-.842,-.129,.237\n',captured_at=NOW)
 assert r['source_as_of']=='2026-11-03' and r['data']['fsi']=='-1.484'
 assert any('not a basis-point' in x for x in r['limitations'])
def observed(code,value,session='2026-11-05',publisher='Cboe Global Markets',status='primary_document',id=1):
 s='VIX'if code.startswith('vix_')else'VXN'
 return {'id':id,'metric_code':code,'metric_value':value,'metric_unit':'percent','event_at':session+'T00:00:00Z','published_at':'2026-11-06T13:00:00Z','collected_at':'2026-11-06T13:01:00Z','publisher':publisher,'claim_status':status,'source_url':f'https://cdn-api.cboe.com/api/global/us_indices/daily_prices/{s}_History.csv','source_title':code,'summary':'Measured source value','family':'market_breadth','phase':'confirmation','origin_group':s+session,'caveats':'No forecast'}
def market():return {'observations':[observed('vix_daily_close','28.18',id=1),observed('vix_5session_change_pct','63.84',id=2),observed('vxn_daily_close','38.26',id=3),observed('vxn_5session_change_pct','51.95',id=4)]}
def test_descriptive_risk_is_useful_without_equity_credentials_but_no_probability():
 r=build_brief(market(),[],[],as_of=NOW)
 assert r['market_regime']=='RISK_OFF' and r['probability']is None
 assert r['execution_enabled']is False and r['intraday_confirmation']=='NOT_ESTABLISHED'
def test_stale_change_pair_cannot_trigger_a_fresh_risk_regime():
 d=market();d['observations'][1]['event_at']='2026-10-20T00:00:00Z'
 r=build_brief(d,[],[],as_of=NOW)
 assert r['market_regime']=='INSUFFICIENTLY_OBSERVED'
 assert any('close/change sessions'in x for x in r['missing_inputs'])
def test_unverified_same_named_metric_cannot_displace_primary_receipt():
 d=market();d['observations'].insert(0,observed('vix_daily_close','1',publisher='Rumor',status='unverified',id=99))
 r=build_brief(d,[],[],as_of=NOW);assert r['market_regime']=='RISK_OFF'
 assert next(x for x in r['evidence']if x['metric']=='vix_daily_close')['value']=='28.18'
def test_missing_data_and_lagged_macro_do_not_create_risk_on_or_invent_events():
 r=build_brief({'observations':[]},[],[],as_of=NOW)
 assert r['market_regime']=='INSUFFICIENTLY_OBSERVED' and r['geopolitical_watch']==[]
 assert r['probability']is None

def test_provider_refusal_preserves_only_reviewed_status_without_response_message():
 from app.market_macro import OfficialSourceDeclined,failure_diagnostic
 d=bls();d['payroll']={'status':'REQUEST_NOT_PROCESSED','message':['sensitive untrusted response text']}
 with pytest.raises(OfficialSourceDeclined)as rejected:parse_bls(d,captured_at=NOW)
 assert failure_diagnostic(rejected.value)=={'category':'PROVIDER_DECLINED','provider_status':'REQUEST_NOT_PROCESSED'}
 assert failure_diagnostic(OfficialSourceDeclined('arbitrary secret'))['provider_status']=='UNKNOWN_STATUS'

def test_failed_source_is_recorded_and_diagnosed_without_logging_body_or_credentials(monkeypatch,caplog):
 import httpx
 from app import market_macro as macro
 class Store:
  def __init__(self):self.calls=[]
  async def rpc(self,name,args):
   self.calls.append((name,args))
   return {'claimed':True,'check_id':71}if name=='market_macro_claim_v1'and args['p_provider']=='bls_employment'else {'claimed':False}
 async def unavailable(provider):
  request=httpx.Request('GET','https://api.bls.gov/?secret=do-not-log',headers={'Authorization':'Bearer do-not-log'})
  response=httpx.Response(403,request=request,text='untrusted do-not-log body')
  response.raise_for_status()
 monkeypatch.setattr(macro,'fetch_macro',unavailable);store=Store()
 with caplog.at_level('WARNING'):result=asyncio.run(macro.refresh_macro(store))
 assert result[0]['status']=='UNAVAILABLE'and result[0]['diagnostic']=={'category':'UPSTREAM_HTTP','http_status':403}
 assert ('market_macro_failure_v1',{'p_check_id':71})in store.calls
 assert 'macro_source_unavailable'in caplog.text and '403'in caplog.text and 'do-not-log'not in caplog.text
 assert 'CAPTURED'not in str(result)
