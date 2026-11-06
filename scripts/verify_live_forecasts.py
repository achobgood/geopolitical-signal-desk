"""Live public acceptance; zero forecasts is an honest data/lock state, not accuracy.

Waits for this revision's code fingerprint, checks private authentication, and
never creates a historical/prospective forecast, fixture, or broker request.
"""
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen, Request

BASE=os.getenv('BASE_URL','https://geopolitical-signal-desk.onrender.com').rstrip('/')
EXPECTED=hashlib.sha256(Path('app/market_forecasting.py').read_bytes()).hexdigest()


def get(path):
    try:
        with urlopen(Request(BASE+path),timeout=35) as r:return r.status,json.load(r)
    except HTTPError as e:return e.code,{}


def main():
    result=None
    for _ in range(30):
        try:
            code,result=get('/v1/market/forecasts/public')
            if code==200 and result.get('implementation_sha256')==EXPECTED:break
        except (URLError,TimeoutError,json.JSONDecodeError):pass
        time.sleep(10)
    else:raise AssertionError('This forecast implementation is not live')
    assert result['execution_enabled'] is False
    assert result['selected_strategy'] is None
    assert result['probability_status']=='NOT_CALIBRATED'
    assert {x['symbol'] for x in result['targets']}=={'SPY','QQQ'}
    assert 'Nasdaq-100' in result['labels']['QQQ']
    assert 'input_bars' not in json.dumps(result)
    for row in result['forecasts']:
        assert row['issued_at']<=row['cutoff_at']
        assert row['target_session']>row['reference_session']
        assert row['probability'] is None and row['selected_strategy'] is False
    for path in ('/v1/market/forecasts','/v1/market/forecasts/performance','/v1/market/experiments','/v1/market/forecasts/runs'):
        assert get(path)[0]==401,path
    print(json.dumps({'status':'LIVE_FORECAST_CONTRACT_ACCEPTED','implementation_sha256':EXPECTED,
                      'actual_forecasts':len(result['forecasts']),'window':result['window'],
                      'data_source':result['data_source'],'predictive_accuracy_claimed':False},sort_keys=True))

if __name__=='__main__':main()
