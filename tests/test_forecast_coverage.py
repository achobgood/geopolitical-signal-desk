import asyncio
from datetime import datetime
from app import market_forecasting as m

def receipt(id,attempts):
 return {'id':id,'run_id':id,'details':{'window':{'eligible':True,'reference_session':'2026-11-06','target_session':'2026-11-09','cutoff_at':'2026-11-06T19:00:00-05:00'},'issuance':attempts}}

def test_repeated_missing_source_attempts_remain_two_slots_and_can_recover_without_hiding_the_outage():
 blocked=[{'symbol':s,'status':'BLOCKED_NO_REFERENCE'}for s in m.LABELS]
 receipts=[receipt(1,blocked),receipt(2,blocked)]
 c=m.summarize_issuance_coverage(receipts,now=datetime.fromisoformat('2026-11-06T22:00:00+00:00'))
 assert c['attempted_symbol_sessions']==2 and c['retryable_unissued_slots']==2 and c['issued_symbol_sessions']==0
 assert all(x['attempts']==2 for x in c['slots'])
 receipts.append(receipt(3,[{'symbol':'SPY','status':'UP','forecast_id':4},{'symbol':'QQQ','status':'ABSTAIN','forecast_id':5}]))
 c=m.summarize_issuance_coverage(receipts,now=datetime.fromisoformat('2026-11-06T23:00:00+00:00'))
 assert c['attempted_symbol_sessions']==2 and c['issued_symbol_sessions']==2 and c['retryable_unissued_slots']==0
 assert all(x['attempts']==3 for x in c['slots'])

def test_closed_missing_source_window_is_visible_and_not_a_scored_forecast():
 c=m.summarize_issuance_coverage([receipt(1,[{'symbol':'SPY','status':'BLOCKED_NO_REFERENCE'}])],now=datetime.fromisoformat('2026-11-07T00:00:01+00:00'))
 assert c['missed_windows']==1 and c['issued_symbol_sessions']==0 and c['slots'][0]['forecast_id']is None
 assert m.summarize_performance([],[])['groups']==[]

def test_empty_pipeline_never_claims_to_have_produced_forecasts(monkeypatch):
 class Store:
  async def rpc(self,*a):return []
 monkeypatch.setattr(m,'configuration',lambda:{'configured':False,'historical_consolidated':False,'mode':'disabled','coverage_note':'No data'})
 result=asyncio.run(m.public_forecasts(Store()))
 assert result['status']=='BLOCKED_MARKET_DATA'and 'No prospective forecasts'in result['claim']
 assert 'Only abstentions'in m.forecast_claim([{'direction':'ABSTAIN'}])
