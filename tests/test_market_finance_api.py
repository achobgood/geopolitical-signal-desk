import hashlib
from fastapi.testclient import TestClient
from app.main import app,requests
from app.store import get_store
from app.market_research_api import FINANCE_ATTEMPTS
KEY='dedicated-private-finance-key-value-32-chars'
class Store:
 def __init__(self):self.calls=[]
 async def rpc(self,name,body):
  self.calls.append((name,body))
  if name=='market_finance_authorize_v1':return {'configured':True,'valid':body['p_hash']==hashlib.sha256(KEY.encode()).hexdigest()}
  if name=='market_portfolio_latest_v1':return {}
  raise AssertionError(name)
def test_general_credentials_never_bypass_separate_financial_scope(monkeypatch):
 fake=Store();app.dependency_overrides[get_store]=lambda:fake;FINANCE_ATTEMPTS.clear();requests.clear()
 try:
  with TestClient(app)as c:
   for key in ['','owner-key-'*5,'reader-key-'*5,'scheduler-key-'*5]:
    h={'Authorization':'Bearer '+key}if key else{}
    for path in ['/v1/market/finance/status','/v1/market/finance/portfolio']:
     assert c.get(path,headers=h).status_code==401
    assert c.post('/v1/market/finance/research',headers=h,json={'ticker':'NVDA'}).status_code==401
   assert not any(x[0]=='market_portfolio_latest_v1' for x in fake.calls)
   r=c.get('/v1/market/finance/portfolio',headers={'Authorization':'Bearer '+KEY})
   assert r.status_code==200 and r.json()['status']=='NO_MANUAL_PORTFOLIO'
   assert r.headers['cache-control']=='no-store' and KEY not in r.text
 finally:app.dependency_overrides.clear()
def test_private_validation_never_echoes_financial_values(monkeypatch):
 fake=Store();app.dependency_overrides[get_store]=lambda:fake;FINANCE_ATTEMPTS.clear()
 try:
  with TestClient(app)as c:
   r=c.post('/v1/market/finance/portfolio',headers={'Authorization':'Bearer '+KEY},json={'record_key':'abc12345','portfolio':{'as_of':'2026-11-06T10:00:00-05:00','source':'statement','currency':'USD','complete':True,'cash':'sensitive-balance-canary','positions':[]}})
   assert r.status_code==422 and 'sensitive-balance-canary'not in r.text
 finally:app.dependency_overrides.clear()


def test_natural_language_ticker_selection_never_silently_uses_a_conflicting_default():
    import pytest
    from app.market_research_api import ResearchRequest
    assert ResearchRequest(question='Analyze Cisco').ticker=='CSCO'
    with pytest.raises(ValueError):ResearchRequest(ticker='NVDA',question='Analyze CSCO')
    with pytest.raises(ValueError):ResearchRequest(question='Compare NVDA and CSCO')
