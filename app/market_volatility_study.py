"""Retrospective Cboe volatility-index event study using ACTUALLY stored OHLC.

Counts all eligible sessions, right-censored future horizons and overlapping
event windows. This is not investment P&L, a trading strategy, a probability
forecast, nor a historical Signal Desk alert. First observed in system now.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

VERSION = "retrospective-volatility-context-v1"
HORIZONS = (5, 20)
THRESHOLD = Decimal("20")
MAX_INPUT = 320


def _p(value: Decimal) -> str:
    return str(value.quantize(Decimal(".01"), rounding=ROUND_HALF_UP))


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    a = sorted(values)
    n = len(a)
    return a[n // 2] if n % 2 else (a[n//2 - 1] + a[n//2]) / Decimal(2)


def _change(before: Decimal, after: Decimal) -> Decimal:
    return (after / before - 1) * Decimal(100)


def study_cboe_history(rows: list[dict], *, symbol: str,
                       collected_at: datetime) -> dict:
    if symbol not in ("VIX", "VXN"):
        raise ValueError("Only Cboe volatility indices supported in retrospective study")
    if collected_at.utcoffset() is None:
        raise ValueError("Actual collection time required")
    if not isinstance(rows,list) or not 100 <= len(rows) <= MAX_INPUT:
        raise ValueError("Require 100–320 historical daily observations")
    parsed = []
    for entry in rows:
        try:
            session = date.fromisoformat(entry["session"])
            amount = Decimal(str(entry["close"]))
        except (ValueError,KeyError,TypeError,InvalidOperation) as exc:
            raise ValueError("Malformed official index observation") from exc
        if not amount.is_finite() or amount <= 0:
            raise ValueError("Nonpositive or nonfinite official index value")
        if session > collected_at.date():
            raise ValueError("Future market session in source")
        if parsed and session <= parsed[-1][0]:
            raise ValueError("Daily sessions not strictly increasing")
        parsed.append((session,amount))
    result = {
        "status":"RETROSPECTIVE_DESCRIPTIVE_ONLY",
        "method_version":VERSION,
        "symbol":symbol,
        "instrument_type":"volatility_index_nontradable",
        "threshold":"20% increase over five completed sessions",
        "source_first_analyzed_at":collected_at.isoformat(),
        "first_session":parsed[0][0].isoformat(),
        "last_session":parsed[-1][0].isoformat(),
        "observed_daily_sessions":len(parsed),
        "signal_was_recorded_at_past_dates":False,
        "no_forecast_accuracy_established":True,
        "no_stock_or_portfolio_return":True,
        "horizons":{},
        "caveats":[
            "Full historical series was obtained after its earlier observation dates. All event-study dates are hindsight, not prior Signal Desk alerts.",
            "Threshold is an uncalibrated descriptive attention rule and may have been selected after seeing 2026 market events.",
            "Event windows overlap and are not independent samples; small samples are unsuitable for statistical inference.",
            "Subsequent VIX/VXN index changes are not equity returns or trading profitability.",
            "Unusual exchange holidays and later data corrections may affect daily-session alignment.",
        ],
    }
    for horizon in HORIZONS:
        eligible = max(0,len(parsed) - horizon - 5)
        if eligible < 1:
            result["horizons"][str(horizon)] = {
                "status":"INSUFFICIENT_COMPLETED_SESSIONS", "eligible":0
            }
            continue
        baseline = []
        events = []
        pending_event_windows = 0
        trigger_count = 0
        for i in range(5,len(parsed)):
            five_day = _change(parsed[i-5][1],parsed[i][1])
            hit = five_day >= THRESHOLD
            if hit:
                trigger_count += 1
            if i + horizon >= len(parsed):
                if hit:
                    pending_event_windows += 1
                continue
            future = _change(parsed[i][1],parsed[i+horizon][1])
            baseline.append(future)
            if hit:
                events.append({
                    "session":parsed[i][0].isoformat(),
                    "five_session_volatility_change_pct":_p(five_day),
                    "future_end_session":parsed[i+horizon][0].isoformat(),
                    "subsequent_index_change_pct":_p(future),
                })
        med_base = _median(baseline)
        med_cond = _median([Decimal(e["subsequent_index_change_pct"]) for e in events])
        result["horizons"][str(horizon)] = {
            "status":"DESCRIPTIVE_SAMPLES_ONLY" if len(events)>=3 else "INSUFFICIENT_TRIGGERED_SAMPLES",
            "horizon_completed_sessions":horizon,
            "all_eligible_windows":len(baseline),
            "historical_threshold_hits":trigger_count,
            "completed_triggered_windows":len(events),
            "right_censored_triggered_windows":pending_event_windows,
            "unconditional_median_index_change_pct":_p(med_base) if med_base is not None else None,
            "conditional_median_index_change_pct":_p(med_cond) if med_cond is not None else None,
            "overlapping_events_not_independent":True,
            # bounded evidence, all sample dates recoverable from persisted input
            "example_events":events[-8:],
            "source_window_count":len(parsed),
            "not_a_winning_trade_rate":True,
        }
    return result
