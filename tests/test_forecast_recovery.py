"""Independent local recovery/cutoff invariants. No real prices or provider calls."""
import asyncio
from copy import deepcopy
from datetime import date,datetime,timedelta,timezone
import pytest
from app import market_forecasting as m
from app import market_risk
from app.market_calendar import is_expected_market_session


def capture(symbol,at,flat=False):
    day=date(2026,11,6);days=[]
    while len(days)<60:
        if is_expected_market_session(day):days.append(day)
        day-=timedelta(days=1)
    bars=[{'session':str(d),'close':str(100 if flat or d==date(2026,11,6) else 99)}for d in reversed(days)]
    return {'id':1 if symbol=='SPY' else 2,'captured_at':at.isoformat(),'input_bars':bars}

class Store:
    def __init__(self,now):self.now=now;self.forecasts=[];self.receipts=[];self.events=[];self.keys={};self.pending=[]
    async def rows(self,table,**kw):
        assert table=='market_forecasts'
        f=kw['filters'];return [x for x in self.forecasts if x['symbol']==f['symbol'][3:]and x['target_session']==f['target_session'][3:]and x['model_version']==f['model_version'][3:]][:1]
    async def rpc(self,name,p):
        self.events.append(name)
        if name=='market_forecast_run_begin_v1':
            key=p['p_key']
            if key in self.keys:return {'id':self.keys[key],'created':False}
            self.keys[key]=len(self.keys)+1;return {'id':self.keys[key],'created':True}
        if name=='market_issue_forecast_v1':
            f={**deepcopy(p['p']),'id':100+len(self.forecasts),'issued_at':self.now().isoformat(),'reference_close':'100'}
            assert self.now().astimezone(m.NY).hour<19 or self.now().astimezone(m.NY).time().isoformat()=='19:00:00'
            self.forecasts.append(f);return f
        if name=='market_pending_forecasts_v1':return deepcopy(self.pending)
        if name=='market_evaluate_forecast_v1':return {'status':p['p_status']}
        if name=='market_forecast_run_finish_v1':
            self.receipts.append({'id':len(self.receipts)+1,'run_id':p['p_id'],'details':deepcopy(p['p_details'])})
            return {'finished_at':self.now().isoformat()}
        raise AssertionError(name)


def setup(monkeypatch):
    state={'now':datetime.fromisoformat('2026-11-06T23:00:00+00:00'),'missing':True,'flat':False}
    monkeypatch.setattr(m,'clock',lambda:state['now'])
    store=Store(lambda:state['now'])
    async def source(_store,symbol):
        if state['missing']:return {'status':'NOT_CONFIGURED','reason':'Synthetic provider unavailable'}
        return {'status':'CAPTURED','capture':capture(symbol,state['now'],state['flat'])}
    async def research(*args,**kw):return {'status':'SOURCE_BOUND_RESEARCH','brief_id':1,'source_refresh':[]}
    monkeypatch.setattr(m,'capture_price',source);monkeypatch.setattr(market_risk,'generate_risk_brief',research)
    return state,store


def test_missing_reference_remains_durable_retryable_attempt_then_recovers(monkeypatch):
    state,store=setup(monkeypatch)
    first=asyncio.run(m.run_forecast_cycle(store,'critic-missing-001','manual'))
    assert store.forecasts==[]
    assert len(first['issuance'])==2 and all(x['status']=='BLOCKED_NO_REFERENCE'for x in first['issuance'])
    original=deepcopy(store.receipts[0])
    state['missing']=False;state['now']+=timedelta(minutes=15)
    second=asyncio.run(m.run_forecast_cycle(store,'critic-recover-002','manual'))
    assert len(store.forecasts)==2 and all(f['direction']=='UP'for f in store.forecasts)
    assert store.receipts[0]==original
    coverage=m.summarize_issuance_coverage(store.receipts,now=state['now'])
    assert coverage['attempted_symbol_sessions']==2 and coverage['issued_symbol_sessions']==2
    assert all(x['attempts']==2 for x in coverage['slots'])


def test_unrecovered_missing_data_stays_visible_after_window_without_scored_prediction(monkeypatch):
    state,store=setup(monkeypatch)
    asyncio.run(m.run_forecast_cycle(store,'critic-missing-003','manual'))
    coverage=m.summarize_issuance_coverage(store.receipts,now=datetime.fromisoformat('2026-11-07T00:01:00+00:00'))
    assert coverage['missed_windows']==2 and coverage['issued_symbol_sessions']==0
    assert m.summarize_performance(store.forecasts,[])['groups']==[]
    assert m.forecast_claim([]).startswith('No prospective forecasts')


def test_reference_backed_abstention_stays_locked_even_if_later_price_changes(monkeypatch):
    state,store=setup(monkeypatch);state.update(missing=False,flat=True)
    asyncio.run(m.run_forecast_cycle(store,'critic-flat-004','manual'))
    assert len(store.forecasts)==2 and all(x['direction']=='ABSTAIN'and x['reference_capture_id']is not None for x in store.forecasts)
    original=deepcopy(store.forecasts);state['flat']=False
    second=asyncio.run(m.run_forecast_cycle(store,'critic-flat-005','manual'))
    assert store.forecasts==original
    assert all(x['status']=='ALREADY_LOCKED'for x in second['issuance'])


def test_optional_slow_failing_brief_runs_after_issuance_and_evaluation(monkeypatch):
    state,store=setup(monkeypatch);state.update(missing=False,now=datetime.fromisoformat('2026-11-06T23:59:30+00:00'))
    store.pending=[{'id':71,'symbol':'QQQ','target_session':'2026-11-06','reference_session':'2026-11-05','reference_close':'99','direction':'UP','issued_at':'2026-11-05T23:00:00Z'}]
    async def slow(*args,**kw):
        store.events.append('OPTIONAL_BRIEF')
        assert len(store.forecasts)==2
        assert 'market_evaluate_forecast_v1'in store.events
        state['now']=datetime.fromisoformat('2026-11-07T00:01:00+00:00')
        raise RuntimeError('Synthetic optional outage')
    monkeypatch.setattr(market_risk,'generate_risk_brief',slow)
    result=asyncio.run(m.run_forecast_cycle(store,'critic-priority-006','manual'))
    assert result['status']=='RECORDED'and result['risk_intelligence']['status']=='UNAVAILABLE'
    assert store.events.index('market_issue_forecast_v1')<store.events.index('OPTIONAL_BRIEF')
    assert result['evaluation'][0]['status']=='OBSERVED'


def test_collection_crossing_cutoff_never_issues_or_backdates(monkeypatch):
    state,store=setup(monkeypatch);state['now']=datetime.fromisoformat('2026-11-06T23:59:59.999000+00:00')
    async def crossing(_store,symbol):
        state['now']=datetime.fromisoformat('2026-11-07T00:00:00.001000+00:00')
        return {'status':'CAPTURED','capture':capture(symbol,state['now'])}
    monkeypatch.setattr(m,'capture_price',crossing)
    result=asyncio.run(m.run_forecast_cycle(store,'critic-cross-007','manual'))
    assert store.forecasts==[]
    assert all(x['status'].startswith('LOCK_MISSED')for x in result['issuance'])
    assert m.summarize_issuance_coverage(store.receipts,now=state['now'])['missed_windows']==2


def test_one_symbol_outage_does_not_suppress_other_symbol_or_its_outcome(monkeypatch):
    state,store=setup(monkeypatch)
    store.pending=[{'id':71,'symbol':'SPY','target_session':'2026-11-06','reference_session':'2026-11-05','reference_close':'99','direction':'UP','issued_at':'2026-11-05T23:00:00Z'},
                   {'id':72,'symbol':'QQQ','target_session':'2026-11-06','reference_session':'2026-11-05','reference_close':'99','direction':'UP','issued_at':'2026-11-05T23:00:00Z'}]
    async def source(_store,symbol):return {'status':'PROVIDER_UNAVAILABLE','reason':'Synthetic source outage'}if symbol=='SPY'else{'status':'CAPTURED','capture':capture(symbol,state['now'])}
    monkeypatch.setattr(m,'capture_price',source)
    result=asyncio.run(m.run_forecast_cycle(store,'critic-isolate-008','manual'))
    assert [f['symbol']for f in store.forecasts]==['QQQ']
    assert {x['forecast_id']:x['status']for x in result['evaluation']}=={71:'MISSING_PRICE',72:'OBSERVED'}
