"""Prospective, immutable next-session ETF research; no orders or probabilities.

All live timestamps are acquired by the server. Historical backfills never enter
this path. Alpaca consolidated historical bars remain disabled without credentials.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from decimal import Decimal

from app.market_calendar import NY, is_expected_market_session
from app.market_quotes import configuration

MODEL = 'momentum-1-research-v1'
CONFIG = {'return_sessions': 1, 'adjustment': 'split', 'probability': None,
          'horizon_sessions': 1, 'lock_eastern': '19:00', 'selected_strategy': False}
LABELS = {'SPY': 'S&P 500 proxy (SPY)', 'QQQ': 'Nasdaq-100 proxy (QQQ; not Nasdaq Composite)'}
CALENDAR_SOURCE = 'https://www.nyse.com/trade/hours-calendars'
CLAIM = 'The model produces reproducible forecasts. Predictive performance is not yet established.'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def clock():
    return datetime.now(timezone.utc)


def next_session(day: date) -> date:
    if not 2026 <= day.year <= 2028:
        raise ValueError('Forecast calendar verified only for 2026–2028')
    for offset in range(1, 12):
        result = day + timedelta(days=offset)
        if result.year > 2028:
            raise ValueError('Next-session calendar needs official renewal')
        if is_expected_market_session(result):
            return result
    raise ValueError('No verified next exchange session')


def issuance_window(now: datetime) -> dict:
    if now.utcoffset() is None:
        raise ValueError('Server clock must carry timezone')
    local = now.astimezone(NY)
    day = local.date()
    if not 2026 <= day.year <= 2028:
        return {'status': 'CALENDAR_UNVERIFIED', 'eligible': False}
    # Do not silently roll a missed evening into a purported earlier forecast.
    if not is_expected_market_session(day):
        return {'status': 'NOT_PRECEDING_TRADING_DAY', 'eligible': False}
    target = next_session(day)
    cutoff = datetime.combine(day, time(19), NY)
    start = datetime.combine(day, time(16, 15), NY)
    return {'status': 'ELIGIBLE' if start <= local <= cutoff else
            'LOCK_MISSED' if local > cutoff else 'AWAITING_REFERENCE_CLOSE',
            'eligible': start <= local <= cutoff, 'reference_session': str(day),
            'target_session': str(target), 'cutoff_at': cutoff.isoformat(),
            'calendar_source': CALENDAR_SOURCE}


def bar_payload(history: dict) -> list[dict]:
    return [{'session': str(b.session), 'open': str(b.open), 'high': str(b.high),
             'low': str(b.low), 'close': str(b.close)} for b in history['bars']]


async def capture_price(store, symbol: str) -> dict:
    from app.market_alpaca_history import fetch_historical_equity
    import httpx
    import asyncio
    cfg = configuration()
    if not cfg['configured']:
        return {'status': 'NOT_CONFIGURED', 'reason': 'Alpaca read-only market-data credentials and explicit mode missing'}
    if not cfg['historical_consolidated']:
        return {'status': 'NONCONSOLIDATED', 'reason': 'IEX partial-exchange bars cannot establish the consolidated daily close'}
    try:
        history = await fetch_historical_equity(symbol, adjustment='split')
        if history.get('status') != 'RESEARCH_ONLY':
            return {'status': history.get('status', 'UNAVAILABLE'), 'reason': 'Current consolidated completed-session daily history unavailable'}
        bars = bar_payload(history)
        # Capture exact retrieved series and server collection time, not a claimed
        # historical first-seen time. The SQL RPC independently dates the receipt.
        result = await store.rpc('market_capture_forecast_prices_v1', {'p': {
            'symbol': symbol, 'provider': 'alpaca', 'feed': history['feed'],
            'adjustment': history['adjustment'], 'source_url': history['source_url'],
            'bars_sha256': digest(bars), 'input_bars': bars,
            'quality': history['quality'], 'data_delay_minutes': history['data_delay_minutes']}})
        return {'status': 'CAPTURED', 'capture': result}
    except (ValueError, KeyError, TypeError, httpx.HTTPError, asyncio.TimeoutError):
        return {'status': 'PROVIDER_UNAVAILABLE', 'reason': 'Provider timeout, entitlement, malformed or incomplete source; no invented close'}


def forecast_payload(symbol: str, window: dict, capture: dict | None, reason: str | None = None) -> dict:
    bars = capture.get('input_bars', []) if capture else []
    direction, momentum, majority, features = 'ABSTAIN', 'ABSTAIN', 'ABSTAIN', {}
    reasons = [reason] if reason else []
    reference_valid = False
    if bars:
        if bars[-1]['session'] != window['reference_session']:
            reasons.append('Latest daily reference is stale')
        elif len(bars) < 60:
            reasons.append('At least 60 complete daily bars are required')
        else:
            reference_valid = True
            closes = [float(b['close']) for b in bars]
            change = closes[-1] / closes[-2] - 1
            momentum = 'UP' if change > 0 else 'DOWN' if change < 0 else 'ABSTAIN'
            direction = momentum
            changes = [b - a for a, b in zip(closes, closes[1:])]
            up, down = sum(x > 0 for x in changes), sum(x < 0 for x in changes)
            majority = 'UP' if up >= down else 'DOWN'
            features = {'return_1': change, 'return_5': closes[-1]/closes[-6]-1,
                        'sma20': sum(closes[-20:])/20,
                        'source_price_capture_id': capture['id'],
                        'first_collected_at': capture['captured_at'],
                        'usable_at': capture['captured_at'],
                        'adjustment': 'split', 'regime': 'UNCLASSIFIED'}
            if momentum == 'ABSTAIN':
                reasons.append('Reference close unchanged: no directional momentum')
    if reasons:
        direction = 'ABSTAIN'
    return {'symbol': symbol, 'target_session': window['target_session'],
            'reference_session': window['reference_session'],
            'reference_capture_id': capture['id'] if reference_valid else None,
            'model_version': MODEL, 'code_hash': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'config_hash': digest(CONFIG), 'configuration': CONFIG,
            'direction': direction, 'probability': None,
            'features': features if reference_valid else {},
            'benchmarks': {'always_up': 'UP', 'momentum': momentum, 'training_majority': majority},
            'abstention_reasons': reasons,
            'supporting_evidence': ['Previous completed consolidated session price return'] if not reasons else [],
            'counterevidence': ['One-session momentum may reverse; no established predictive edge'],
            'missing_inputs': ['Calibrated independent probability', 'Validated macro/credit/intelligence model'],
            'invalidation': ['Reference price revision', 'Provider completeness failure', 'Exchange closure'],
            'research_only': True, 'selected_strategy': False}


def evaluate_pair(forecast: dict, capture: dict, now: datetime) -> dict:
    target = date.fromisoformat(forecast['target_session'])
    # A conservative ordinary-close publication buffer also safely handles half days.
    if now.astimezone(NY) < datetime.combine(target, time(16, 30), NY):
        return {'status': 'AWAITING_CLOSE'}
    first = datetime.fromisoformat(capture['captured_at'].replace('Z', '+00:00'))
    if first <= datetime.fromisoformat(forecast['issued_at'].replace('Z', '+00:00')):
        return {'status': 'MISSING_PRICE', 'reason': 'Outcome must be collected after forecast issuance'}
    by_day = {b['session']: b for b in capture['input_bars']}
    base = by_day.get(forecast['reference_session'])
    actual = by_day.get(forecast['target_session'])
    if not base or not actual:
        return {'status': 'MISSING_PRICE', 'reason': 'Exact reference or target session absent; never substitute nearest date'}
    if Decimal(base['close']) != Decimal(str(forecast['reference_close'])):
        return {'status': 'SOURCE_REVISED', 'reason': 'Split-adjusted reference close was revised; comparison unresolved'}
    if forecast.get('previous_target_close') is not None and Decimal(actual['close']) != Decimal(str(forecast['previous_target_close'])):
        return {'status': 'SOURCE_REVISED', 'reason': 'Previously observed target close revised; comparison unresolved'}
    close, ref = float(actual['close']), float(forecast['reference_close'])
    return {'status': 'OBSERVED', 'target_close': actual['close'],
            'actual_direction': 'UP' if close > ref else 'DOWN' if close < ref else 'FLAT',
            'price_return': close/ref-1}


async def run_forecast_cycle(store, run_key: str, trigger: str) -> dict:
    begin = await store.rpc('market_forecast_run_begin_v1', {'p_key': run_key, 'p_trigger': trigger})
    if not begin['created']:
        return {'status': 'ALREADY_RECORDED', 'run_id': begin['id']}
    window = issuance_window(clock())
    details = {'window': window, 'issuance': [], 'evaluation': [], 'execution_enabled': False}
    captures = {}
    if window.get('eligible'):
        for symbol in LABELS:
            existing = await store.rows('market_forecasts', limit=1, filters={
                'symbol': 'eq.' + symbol, 'target_session': 'eq.' + window['target_session'],
                'model_version': 'eq.' + MODEL})
            if existing:
                details['issuance'].append({'symbol': symbol, 'status': 'ALREADY_LOCKED', 'forecast_id': existing[0]['id']})
                continue
            price = await capture_price(store, symbol)
            captures[symbol] = price
            payload = forecast_payload(symbol, window, price.get('capture'), price.get('reason'))
            # SQL uses actual now and will reject a fetch that crosses the lock.
            if not issuance_window(clock()).get('eligible'):
                details['issuance'].append({'symbol': symbol, 'status': 'LOCK_MISSED_DURING_COLLECTION'})
                continue
            rec = await store.rpc('market_issue_forecast_v1', {'p': payload})
            details['issuance'].append({'symbol': symbol, 'status': rec['direction'], 'forecast_id': rec['id']})
    pending = await store.rpc('market_pending_forecasts_v1', {})
    for f in pending:
        symbol = f['symbol']
        if symbol not in captures:
            captures[symbol] = await capture_price(store, symbol)
        price = captures[symbol]
        if f['direction'] == 'ABSTAIN' and not f.get('reference_close'):
            result = {'status': 'UNSCORABLE_ABSTENTION', 'reason': 'No trustworthy original closing reference'}
        elif price.get('capture'):
            result = evaluate_pair(f, price['capture'], clock())
        else:
            if f.get('previous_target_close') is not None:
                details['evaluation'].append({'forecast_id':f['id'],'status':'RECHECK_PROVIDER_UNAVAILABLE'})
                continue
            result = {'status': 'MISSING_PRICE', 'reason': price.get('reason', price['status'])}
        rec = await store.rpc('market_evaluate_forecast_v1', {'p_forecast_id': f['id'],
              'p_capture_id': price.get('capture', {}).get('id'), 'p_status': result['status'],
              'p_reason': result.get('reason', '')})
        details['evaluation'].append({'forecast_id': f['id'], 'status': rec['status']})
    finish = await store.rpc('market_forecast_run_finish_v1', {'p_id': begin['id'], 'p_details': details})
    return {'status': 'RECORDED', 'run_id': begin['id'], **details, 'finished_at': finish['finished_at']}


def wilson(correct: int, n: int):
    if not n:
        return None
    z=1.959963984540054; p=correct/n; den=1+z*z/n
    mid=(p+z*z/(2*n))/den
    half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return [max(0,mid-half), min(1,mid+half)]


def classification(predictions, actuals):
    pairs=[(p,a) for p,a in zip(predictions,actuals) if p in ('UP','DOWN') and a in ('UP','DOWN')]
    n=len(pairs); correct=sum(p==a for p,a in pairs)
    ups=sum(a=='UP' for _,a in pairs); downs=n-ups
    tpr=sum(p==a=='UP' for p,a in pairs)/ups if ups else None
    tnr=sum(p==a=='DOWN' for p,a in pairs)/downs if downs else None
    return {'n':n, 'correct':correct,'accuracy':correct/n if n else None,
            'accuracy_wilson_95':wilson(correct,n),
            'balanced_accuracy':(tpr+tnr)/2 if tpr is not None and tnr is not None else None,
            'false_positive_rate':1-tnr if tnr is not None else None,
            'false_negative_rate':1-tpr if tpr is not None else None}


def summarize_performance(forecasts: list[dict], outcomes: list[dict]) -> dict:
    # Most recent state wins. A revision receipt invalidates a previously scored row.
    latest={}
    for out in sorted(outcomes,key=lambda x:x['id'],reverse=True):
        latest.setdefault(out['forecast_id'],out)
    groups=defaultdict(list)
    for f in forecasts:
        groups[(f['symbol'],f['model_version'])].append(f)
    results=[]
    for (symbol, model), rows in groups.items():
        observed=[(f,latest[f['id']]) for f in rows if latest.get(f['id'],{}).get('status')=='OBSERVED']
        scored=[(f,o) for f,o in observed if f['direction'] in ('UP','DOWN') and o['actual_direction'] in ('UP','DOWN')]
        actuals=[o['actual_direction'] for _,o in scored]
        all_bench={}
        for key in ('always_up','momentum','training_majority'):
            # Every reported paired comparison has exactly the same candidate and
            # benchmark dates; benchmark abstention shrinks BOTH denominators.
            matched=[(f,o) for f,o in scored if f['benchmarks'].get(key) in ('UP','DOWN')]
            all_bench[key]={'matched_sessions':len(matched),
                'candidate':classification([f['direction'] for f,_ in matched],[o['actual_direction'] for _,o in matched]),
                'baseline':classification([f['benchmarks'][key] for f,_ in matched],[o['actual_direction'] for _,o in matched])}
        regimes={}
        for regime in sorted({f.get('features',{}).get('regime','UNCLASSIFIED') for f,_ in scored}):
            rr=[(f,o) for f,o in scored if f.get('features',{}).get('regime','UNCLASSIFIED')==regime]
            regimes[regime]=classification([f['direction'] for f,_ in rr],[o['actual_direction'] for _,o in rr])
        results.append({'symbol':symbol,'model_version':model,'horizon_sessions':1,
            'recorded':len(rows),'observed':len(observed),'unresolved':len(rows)-len(observed),
            'abstentions':sum(f['direction']=='ABSTAIN' for f in rows),
            'abstention_rate':sum(f['direction']=='ABSTAIN' for f in rows)/len(rows),
            'flat_outcomes':sum(o['actual_direction']=='FLAT' for _,o in observed),
            'metrics':classification([f['direction'] for f,_ in scored],actuals),
            'full_directional_cohort': {'n':sum(o['actual_direction'] in ('UP','DOWN') for _,o in observed),
                'candidate_correct':sum(f['direction']==o['actual_direction'] for f,o in observed if o['actual_direction'] in ('UP','DOWN')),
                'baseline_correct':{k:sum(f['benchmarks'].get(k)==o['actual_direction'] for f,o in observed if o['actual_direction'] in ('UP','DOWN')) for k in ('always_up','momentum','training_majority')},
                'candidate_abstentions_count_as_incorrect':True},
            'benchmarks':all_bench,'regimes':regimes,'brier_score':None,'calibration_error':None,
            'probability_status':'NOT_CALIBRATED'})
    return {'basis':'PROSPECTIVELY_RECORDED','groups':results,
            'predictive_edge_established':False,'claim':CLAIM,
            'retrospective_results_included':False}


async def public_forecasts(store):
    # No caller cutoff, portfolio lookup, raw bars or private strategy results.
    rows=await store.rpc('market_public_forecasts_v1', {})
    return {'status':'RESEARCH_ONLY','forecasts':rows,'window':issuance_window(clock()),
            'implementation_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'labels':LABELS,'targets':[{'symbol':symbol,'label':label,'latest_forecast_id':next((f['id'] for f in rows if f['symbol']==symbol),None),'status':'RECORDED' if any(f['symbol']==symbol for f in rows) else 'NO_FORECAST_ISSUED'} for symbol,label in LABELS.items()],
            'execution_enabled':False,'selected_strategy':None,
            'probability_status':'NOT_CALIBRATED','claim':CLAIM,
            'data_source': {k:v for k,v in configuration().items() if k in ('configured','mode','coverage_note')}}
