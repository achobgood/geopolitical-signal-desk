import asyncio
from datetime import date,datetime,timezone
from fastapi.testclient import TestClient
import pytest

from app import market_forecasting as m
from app.main import app,requests
from app.store import get_store


def test_real_lock_calendar_dst_holidays_and_no_backdating():
    assert m.issuance_window(datetime.fromisoformat('2026-11-05T23:59:59+00:00'))['eligible']
    assert not m.issuance_window(datetime.fromisoformat('2026-11-06T00:00:00.000001+00:00'))['eligible']
    assert m.issuance_window(datetime.fromisoformat('2026-07-02T22:59:59+00:00'))['target_session']=='2026-07-06'
    assert m.issuance_window(datetime.fromisoformat('2026-11-06T23:50:00+00:00'))['target_session']=='2026-11-09'
    assert m.next_session(date(2027,12,30))==date(2027,12,31)
    assert m.issuance_window(datetime.fromisoformat('2026-11-07T23:00:00+00:00'))['status']=='NOT_PRECEDING_TRADING_DAY'
    assert m.issuance_window(datetime.fromisoformat('2026-11-06T01:00:00+00:00'))['status']=='LOCK_MISSED'
    assert m.issuance_window(datetime.fromisoformat('2029-01-02T23:00:00+00:00'))['status']=='CALENDAR_UNVERIFIED'
    with pytest.raises(ValueError):m.issuance_window(datetime(2026,11,5))


def test_no_credentials_real_configuration_is_fail_closed(monkeypatch):
    monkeypatch.delenv('ALPACA_MARKET_DATA_KEY_ID',raising=False)
    monkeypatch.delenv('ALPACA_MARKET_DATA_SECRET',raising=False)
    result=asyncio.run(m.capture_price(None,'SPY'))
    assert result['status']=='NOT_CONFIGURED'
    forecast=m.forecast_payload('SPY',{'target_session':'2026-11-06','reference_session':'2026-11-05'},None,result['reason'])
    assert forecast['direction']=='ABSTAIN'
    assert forecast['probability'] is None
    assert forecast['selected_strategy'] is False
    assert forecast['reference_capture_id'] is None


def test_exact_outcome_session_and_revised_reference():
    f={'target_session':'2026-11-06','reference_session':'2026-11-05','reference_close':'100','issued_at':'2026-11-05T23:00:00Z'}
    c={'captured_at':'2026-11-06T22:00:00Z','input_bars':[{'session':'2026-11-05','close':'100'},{'session':'2026-11-06','close':'99'}]}
    now=datetime.fromisoformat('2026-11-06T22:00:00+00:00')
    assert m.evaluate_pair(f,c,now)['actual_direction']=='DOWN'
    assert m.evaluate_pair(f,c,datetime.fromisoformat('2026-11-06T20:59:00+00:00'))['status']=='AWAITING_CLOSE'
    c['input_bars'][0]['close']='99.99'
    assert m.evaluate_pair(f,c,now)['status']=='SOURCE_REVISED'
    c['input_bars'].pop()
    assert m.evaluate_pair(f,c,now)['status']=='MISSING_PRICE'


def test_metrics_include_abstention_missing_flat_and_matched_baselines():
    ff=[{'id':i,'symbol':'SPY','model_version':m.MODEL,'direction':d,'features':{},'benchmarks':{'always_up':'UP','momentum':'DOWN','training_majority':'UP'}} for i,d in enumerate(['UP','DOWN','ABSTAIN','UP'],1)]
    oo=[{'id':i,'forecast_id':i,'status':'OBSERVED','actual_direction':d} for i,d in enumerate(['UP','UP','DOWN','FLAT'],1)]
    result=m.summarize_performance(ff,oo)['groups'][0]
    assert result['metrics']['n']==2 and result['metrics']['accuracy']==.5
    assert result['abstention_rate']==.25 and result['flat_outcomes']==1
    assert result['benchmarks']['always_up']['matched_sessions']==2
    assert result['benchmarks']['always_up']['baseline']['accuracy']==1
    assert result['brier_score'] is None
    oo.append({'id':5,'forecast_id':1,'status':'SOURCE_REVISED'})
    assert m.summarize_performance(ff,oo)['groups'][0]['observed']==3


class FakeStore:
    async def rpc(self,name,body):
        if name=='market_public_forecasts_v1':return []
        if name=='market_forecast_run_begin_v1':return {'id':1,'created':True}
        if name=='market_pending_forecasts_v1':return []
        if name=='market_forecast_run_finish_v1':return {'finished_at':'2026-11-06T04:00:00Z'}
        raise AssertionError(name)
    async def rows(self,*args,**kwargs):return []


def test_api_private_isolation_reader_writes_scheduler_and_public(monkeypatch):
    owner='owner-key-with-32-characters-long-value'
    read='reader-key-with-32-characters-long-value'
    import hashlib
    monkeypatch.setenv('SIGNAL_DESK_API_KEY',owner)
    monkeypatch.setenv('SIGNAL_DESK_READ_KEY_SHA256',hashlib.sha256(read.encode()).hexdigest())
    monkeypatch.setenv('SIGNAL_DESK_SCHEDULER_KEY_SHA256',hashlib.sha256(b'scheduler-test-key').hexdigest())
    app.dependency_overrides[get_store]=lambda:FakeStore();requests.clear()
    try:
        with TestClient(app) as c:
            for path in ('/v1/market/forecasts','/v1/market/forecasts/performance','/v1/market/forecasts/evaluations','/v1/market/experiments'):
                assert c.get(path).status_code==401
            assert c.get('/v1/market/forecasts/public').status_code==200
            assert 'input_bars' not in c.get('/v1/market/forecasts/public').text
            assert c.post('/v1/market/forecasts/run',json={'run_key':'forecast-test-001'},headers={'Authorization':'Bearer '+read}).status_code==403
            assert c.post('/v1/market/forecasts/scheduled',json={'run_key':'forecast-test-001'},headers={'Authorization':'Bearer '+owner}).status_code==401
            assert c.post('/v1/market/forecasts/scheduled',json={'run_key':'forecast-test-001'},headers={'Authorization':'Bearer scheduler-test-key'}).status_code==200
            assert c.get('/v1/market/forecasts/performance',headers={'Authorization':'Bearer '+owner}).json()['groups']==[]
    finally:app.dependency_overrides.clear()


def test_flat_momentum_keeps_valid_reference_for_outcome_and_benchmark_evaluation():
    capture={'id':8,'captured_at':'2026-11-05T23:00:00Z','input_bars':[{'session':'2026-11-05','close':'100'} for _ in range(60)]}
    f=m.forecast_payload('SPY',{'reference_session':'2026-11-05','target_session':'2026-11-06'},capture)
    assert f['direction']=='ABSTAIN' and f['abstention_reasons']
    assert f['reference_capture_id']==8
    assert f['features']['source_price_capture_id']==8
    assert f['benchmarks']['always_up']=='UP'


def test_basic_delayed_feed_cannot_lock_a_partial_current_daily_close():
    # 16:15 Eastern minus adapter's16min boundary is15:59, before16:00close.
    assert not m.issuance_window(datetime.fromisoformat('2026-11-06T21:15:00+00:00'))['eligible']
    assert not m.issuance_window(datetime.fromisoformat('2026-11-06T21:29:59.999999+00:00'))['eligible']
    assert m.issuance_window(datetime.fromisoformat('2026-11-06T21:30:00+00:00'))['eligible']
    assert m.issuance_window(datetime.fromisoformat('2026-07-02T20:30:00+00:00'))['eligible']


def test_older_same_session_capture_cannot_be_reused_after_close_buffer():
    capture={'id':9,'captured_at':'2026-11-06T21:15:00Z','input_bars':[{'session':'2026-11-06','close':'100'} for _ in range(60)]}
    f=m.forecast_payload('SPY',{'reference_session':'2026-11-06','target_session':'2026-11-09'},capture)
    assert f['direction']=='ABSTAIN' and f['reference_capture_id'] is None
    assert any('availability window' in x for x in f['abstention_reasons'])


def test_rechecks_wait_during_partial_delayed_daily_bar_window(monkeypatch):
    monkeypatch.setattr(m,'configuration',lambda:{'configured':True,'historical_consolidated':True})
    monkeypatch.setattr(m,'clock',lambda:datetime.fromisoformat('2026-11-06T21:20:00+00:00'))
    result=asyncio.run(m.capture_price(None,'SPY'))
    assert result['status']=='AWAITING_DELAYED_CLOSE'
    assert 'capture' not in result
