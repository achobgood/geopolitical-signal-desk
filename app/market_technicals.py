"""Reproducible multi-timeframe technical research, not trade instructions.

Principles: numerical formulas (Wilder RSI/ATR, SMA), completed candles only,
mathematically confirmed 2-left/2-right daily and 1-left/1-right weekly pivots,
cross-timeframe support/resistance clustering, volatility-sized zone width.
No model-generated levels, calibrated probabilities, intraday quotes, or orders.
"""
from __future__ import annotations

import csv
import hashlib
import io
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable

METHOD = "cross-timeframe-pivots-wilder-v1"
D = Decimal
ZERO = D("0")
CENT = D(".01")
MAX_BAR_ROWS = 320
MIN_BARS = 60


def _number(raw) -> Decimal:
    try:
        n = D(str(raw).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Invalid OHLC numeric input") from exc
    if not n.is_finite() or n <= ZERO or n > D("1000000"):
        raise ValueError("OHLC nonfinite or outside 0..1m")
    return n


def _fmt(value: Decimal | None) -> str | None:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP)) if value is not None else None


@dataclass(frozen=True)
class Bar:
    session: date
    close: Decimal
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None

    def __post_init__(self):
        if self.close <= ZERO or not self.close.is_finite():
            raise ValueError("Invalid closing price")
        present = (self.open is not None, self.high is not None, self.low is not None)
        if any(present) and not all(present):
            raise ValueError("Partial OHLC must not masquerade as a full bar")
        if all(present):
            if any(not p.is_finite() or p <= ZERO for p in (self.open, self.high, self.low)):
                raise ValueError("Invalid OHLC range")
            if self.low > self.high or not self.low <= self.open <= self.high or not self.low <= self.close <= self.high:
                raise ValueError("Inconsistent OHLC range")

    def as_dict(self) -> dict:
        return {"session": self.session.isoformat(), "close": str(self.close),
                "open": str(self.open) if self.open is not None else None,
                "high": str(self.high) if self.high is not None else None,
                "low": str(self.low) if self.low is not None else None}


def parse_cboe_history(blob: bytes, *, cutoff_session: date) -> list[Bar]:
    """Parse *actual* Cboe daily CSV; no fabricated high/low when close-only."""
    if not blob or len(blob) > 3_000_000:
        raise ValueError("Missing or oversized official historical CSV")
    raw = blob.decode("utf-8-sig")
    lines = raw.splitlines()
    start = next((i for i, line in enumerate(lines) if line.lstrip().upper().startswith("DATE,")), None)
    if start is None:
        raise ValueError("No Cboe DATE header")
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    reader.fieldnames = [name.strip().upper() for name in (reader.fieldnames or [])]
    columns = set(reader.fieldnames)
    if not {"DATE", "CLOSE"} <= columns:
        raise ValueError("Cboe date/close fields unavailable")
    price_columns = ("OPEN", "HIGH", "LOW")
    if any(col in columns for col in price_columns) and not all(col in columns for col in price_columns):
        raise ValueError("Ambiguous partial high/low feed")
    full_ohlc = all(col in columns for col in price_columns)
    result: dict[date, Bar] = {}
    seen_invalid = 0
    for row in reader:
        try:
            from app.market_feeds import parse_date
            session = parse_date(row["DATE"])
        except (ValueError, KeyError, AttributeError, TypeError):
            seen_invalid += 1
            continue
        if session > cutoff_session:
            continue
        try:
            close = _number(row["CLOSE"])
            bar = Bar(session=session, close=close,
                      open=_number(row["OPEN"]) if full_ohlc else None,
                      high=_number(row["HIGH"]) if full_ohlc else None,
                      low=_number(row["LOW"]) if full_ohlc else None)
        except (ValueError, KeyError, TypeError):
            seen_invalid += 1
            continue
        if session in result and result[session] != bar:
            raise ValueError("Conflicting prices for the same official session")
        result[session] = bar
    if len(result) < MIN_BARS:
        raise ValueError("Fewer than 60 valid completed daily sessions")
    if seen_invalid > max(5, len(result) // 20):
        raise ValueError("Too many invalid rows in official price history")
    return [result[x] for x in sorted(result)][-MAX_BAR_ROWS:]


def wilder_rsi(closes: list[Decimal], period: int = 14) -> Decimal:
    if len(closes) < period + 1:
        raise ValueError("Insufficient RSI lookback")
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gain = sum((max(ZERO, x) for x in diffs[:period]), ZERO) / D(period)
    loss = sum((max(ZERO, -x) for x in diffs[:period]), ZERO) / D(period)
    for x in diffs[period:]:
        gain = (gain * (period - 1) + max(ZERO, x)) / D(period)
        loss = (loss * (period - 1) + max(ZERO, -x)) / D(period)
    if gain == ZERO and loss == ZERO:
        return D("50")
    if loss == ZERO:
        return D("100")
    return D("100") - D("100") / (D("1") + gain / loss)


def wilder_atr(bars: list[Bar], period: int = 14) -> Decimal | None:
    if len(bars) < period + 1 or any(b.high is None or b.low is None for b in bars):
        return None
    ranges = [max(bar.high - bar.low, abs(bar.high - previous.close), abs(bar.low - previous.close))
              for previous, bar in zip(bars[:-1], bars[1:])]
    atr = sum(ranges[:period], ZERO) / D(period)
    for tr in ranges[period:]:
        atr = (atr * D(period - 1) + tr) / D(period)
    return atr


def _weekly(bars: list[Bar], captured_at: datetime) -> list[Bar]:
    if captured_at.utcoffset() is None:
        raise ValueError("Naive capture timestamp")
    by_week: dict[tuple[int, int], list[Bar]] = defaultdict(list)
    for bar in bars:
        iso = bar.session.isocalendar()
        by_week[(iso.year, iso.week)].append(bar)
    capture = captured_at.date().isocalendar()
    capture_week = (capture.year, capture.week)
    weeks = []
    for key in sorted(by_week):
        days = by_week[key]
        if key == capture_week and days[-1].session.weekday() < 4:
            continue  # A not-yet-finished trading week is not a closed weekly candle.
        full = all(day.high is not None and day.low is not None for day in days)
        weeks.append(Bar(session=days[-1].session, close=days[-1].close,
                         open=days[0].open if full else None,
                         high=max(x.high for x in days) if full else None,
                         low=min(x.low for x in days) if full else None))
    return weeks


def _pivots(bars: list[Bar], left: int, right: int, view: str) -> list[dict]:
    """Right-side bars are required; the latest unconfirmed pivot never appears."""
    if any(b.high is None or b.low is None for b in bars):
        return []
    points = []
    for i in range(left, len(bars) - right):
        candidate = bars[i]
        lo_neighbors = [x.low for x in bars[i-left:i+right+1] if x is not candidate]
        hi_neighbors = [x.high for x in bars[i-left:i+right+1] if x is not candidate]
        if candidate.low < min(lo_neighbors):
            points.append({"side":"support", "price":candidate.low,
                           "view":view, "session":candidate.session.isoformat()})
        if candidate.high > max(hi_neighbors):
            points.append({"side":"resistance", "price":candidate.high,
                           "view":view, "session":candidate.session.isoformat()})
    return points


def _confirmed_zones(points: list[dict], close: Decimal, atr: Decimal) -> list[dict]:
    """Group nearby reversal prices. Require daily + weekly confirmation."""
    tolerance = max(close * D(".0075"), atr * D(".75"))
    half = max(close * D(".004"), atr * D(".35"))
    zones = []
    for side in ("support", "resistance"):
        ordered = sorted((p for p in points if p["side"] == side), key=lambda p:p["price"])
        clusters: list[list[dict]] = []
        for point in ordered:
            if clusters:
                center = sum((x["price"] for x in clusters[-1]), ZERO) / D(len(clusters[-1]))
                if abs(point["price"] - center) <= tolerance:
                    clusters[-1].append(point)
                    continue
            clusters.append([point])
        for cluster in clusters:
            views = {point["view"] for point in cluster}
            if len(cluster) < 2 or views != {"daily", "weekly"}:
                continue
            center = sum((p["price"] for p in cluster), ZERO) / D(len(cluster))
            zones.append({
                "side": side, "center": _fmt(center),
                "lower": _fmt(max(CENT, center - half)),
                "upper": _fmt(center + half),
                "pivot_count": len(cluster),
                "timeframes": ["daily", "weekly"],
                "basis": "confirmed pivots with ATR-sized price zone",
                "not_probability": True,
            })
    return sorted(zones, key=lambda z:(z["side"], D(z["center"])))


def _setup(trend: str, rsi: Decimal, close: Decimal,
           atr: Decimal | None, zones: list[dict]) -> dict:
    blocked = ["This is historical/closing-price technical research, not a live trade ticket"]
    if atr is None:
        return {"status": "NO_LEVELS", "blocked_by": blocked + ["OHLC and ATR not available"]}
    if trend != "up":
        return {"status": "WAIT", "blocked_by": blocked + ["No confirmed uptrend"]}
    if rsi > D("70") or rsi < D("35"):
        return {"status": "WAIT", "blocked_by": blocked + ["Momentum outside provisional pullback range"]}
    supports = [z for z in zones if z["side"] == "support" and D(z["upper"]) < close]
    resistances = [z for z in zones if z["side"] == "resistance" and D(z["lower"]) > close]
    if not supports or not resistances:
        return {"status": "WAIT", "blocked_by": blocked + ["Both confirmed support and overhead resistance required"]}
    support = max(supports, key=lambda z:D(z["center"]))
    resistance = min(resistances, key=lambda z:D(z["center"]))
    entry = D(support["upper"])
    stop = D(support["lower"]) - atr * D(".5")
    target = D(resistance["lower"])
    if stop <= ZERO or target <= entry:
        return {"status": "WAIT", "blocked_by": blocked + ["Invalid zone risk/return geometry"]}
    rr = (target - entry) / (entry - stop)
    if rr < D("1.5"):
        return {"status": "WAIT", "blocked_by": blocked + ["Reference upside less than 1.5x proposed downside"]}
    return {
        "status": "REFERENCE_LEVELS_ONLY", "entry_zone_upper": _fmt(entry),
        "theoretical_stop": _fmt(stop), "overhead_resistance": _fmt(target),
        "gross_reward_to_risk": _fmt(rr), "execution_enabled": False,
        "not_validated_forecast": True,
        "blocked_by": blocked + ["Quote, spreads, tax, earnings, portfolio, and risk-gate checks remain separate"],
    }


def analyze_bars(bars: list[Bar], *, captured_at: datetime, symbol: str, instrument_kind: str) -> dict:
    """Return a deterministic, non-orderable technical research record."""
    if captured_at.utcoffset() is None:
        raise ValueError("Naive captured time")
    if len(bars) < MIN_BARS or len(bars) > MAX_BAR_ROWS:
        raise ValueError("Require 60–320 completed daily bars")
    if any(a.session >= b.session for a,b in zip(bars[:-1],bars[1:])):
        raise ValueError("Daily sessions must be unique, strictly ascending")
    if bars[-1].session > captured_at.date():
        raise ValueError("Future session")
    if instrument_kind not in ("volatility_index","stock"):
        raise ValueError("Unknown instrument class")
    closes = [b.close for b in bars]
    close = closes[-1]
    sma20 = sum(closes[-20:],ZERO) / D(20)
    sma50 = sum(closes[-50:],ZERO) / D(50)
    rsi = wilder_rsi(closes)
    atr = wilder_atr(bars)
    weekly = _weekly(bars,captured_at)
    weekly_up = len(weekly) >= 10 and weekly[-1].close > sum(
        (x.close for x in weekly[-10:]),ZERO)/D(10)
    weekly_down = len(weekly) >= 10 and weekly[-1].close < sum(
        (x.close for x in weekly[-10:]),ZERO)/D(10)
    trend = ("up" if close > sma20 > sma50 and weekly_up else
             "down" if close < sma20 < sma50 and weekly_down else "mixed")
    pivot_points = _pivots(bars[-180:],2,2,"daily") + _pivots(weekly[-40:],1,1,"weekly")
    zones = _confirmed_zones(pivot_points,close,atr) if atr is not None else []
    setup = _setup(trend,rsi,close,atr,zones)
    bars_json = [x.as_dict() for x in bars]
    return {
        "method_version": METHOD,
        "symbol": symbol,
        "instrument_kind": instrument_kind,
        "last_completed_session": bars[-1].session.isoformat(),
        "first_input_session": bars[0].session.isoformat(),
        "captured_at": captured_at.isoformat(),
        "daily_sessions":len(bars),
        "completed_weekly_sessions":len(weekly),
        "price_coverage":"full_ohlc" if atr is not None else "close_only",
        "close":_fmt(close), "sma20":_fmt(sma20), "sma50":_fmt(sma50),
        "rsi14":_fmt(rsi), "atr14":_fmt(atr),
        "momentum": "overbought" if rsi >= 70 else "oversold" if rsi <= 30 else "neutral",
        "trend":trend, "confirmed_zones":zones,
        "setup": setup,
        "bars_sha256":hashlib.sha256(
            __import__("json").dumps(bars_json,sort_keys=True,separators=(",",":")).encode()
        ).hexdigest(),
        "input_bars": bars_json,
        "source_is_tradeable_instrument":instrument_kind=="stock",
        "trade_ticket_approved": False,
        "limitations": (
            "VIX/VXN volatility index levels cannot be traded as equity orders or used to infer market direction."
            if instrument_kind == "volatility_index" else
            "Daily historical bar calculations only. No real-time quote/earnings/account context or validated strategy edge."
        ),
    }
