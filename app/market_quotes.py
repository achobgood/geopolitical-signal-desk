"""Optional Alpaca read-only market-data adapter, with explicit exchange entitlement.

Absolutely no trading API endpoints or order calls. No market-data credentials in
client HTML or returned JSON. IEX is *partial-exchange*, never consolidated SIP.
An earnings calendar is NOT connected, so this does not alone authorize BUY tickets.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import os
import re
from zoneinfo import ZoneInfo

import httpx

HOST = "https://data.alpaca.markets"
SOURCE = "https://docs.alpaca.markets/us/reference/stocklatestquotesingle-1"
NY = ZoneInfo("America/New_York")
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,11}$")
ZERO = Decimal(0)


def configuration():
    feed = os.getenv("ALPACA_DATA_FEED", "disabled").lower()
    key = os.getenv("ALPACA_MARKET_DATA_KEY_ID")
    secret = os.getenv("ALPACA_MARKET_DATA_SECRET")
    return {
        "provider": "alpaca_data_api",
        "configured": bool(key and secret and feed in ("sip", "iex")),
        "feed": feed if feed in ("sip", "iex") else "disabled",
        "scope": "read_only_data_host",
        "consolidated": feed == "sip",
        "execution_enabled": False,
        "coverage_note": ("IEX observes one trading venue, not consolidated US market prices"
                          if feed == "iex" else
                          "SIP requires correct plan/entitlement; setting this flag alone is not proof"
                          if feed == "sip" else "No licensed feed configured"),
    }


async def api_json(path: str, params: dict, *, max_bytes=1_500_000) -> dict:
    c = configuration()
    if not c["configured"]:
        raise ValueError("Licensed market data credentials or feed configuration missing")
    if not path.startswith("/v2/stocks/") or ".." in path:
        raise ValueError("Invalid market-data path")
    headers = {
        "APCA-API-KEY-ID": os.environ["ALPACA_MARKET_DATA_KEY_ID"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_MARKET_DATA_SECRET"],
        "Accept": "application/json",
    }
    async with asyncio.timeout(22):
        async with httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", HOST + path, params=params, headers=headers) as res:
                res.raise_for_status()
                raw = bytearray()
                async for chunk in res.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > max_bytes:
                        raise ValueError("Market-data payload exceeded limit")
                data = res.json() if not raw else __import__("json").loads(bytes(raw))
    if not isinstance(data, dict):
        raise ValueError("Unexpected market-data response")
    return data


def timestamp(value: str) -> datetime:
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.utcoffset() is None:
        raise ValueError("Market timestamp lacks timezone")
    return d.astimezone(timezone.utc)


def positive(value):
    try:
        d = Decimal(str(value))
        if not d.is_finite() or d <= ZERO:
            raise ValueError("Zero, negative or nonfinite quote")
        return d
    except (InvalidOperation, TypeError) as e:
        raise ValueError("Invalid quote") from e


def calculate_bars(bars: list[dict], as_of: datetime) -> dict:
    today = as_of.astimezone(NY).date()
    days = []
    for row in bars[:120]:
        try:
            t = timestamp(row["t"]).astimezone(NY)
            if t.date() >= today:
                continue  # Don't include incomplete current-session bars.
            hi, lo, close, vol = positive(row["h"]), positive(row["l"]), positive(row["c"]), positive(row["v"])
            if lo > hi or not lo <= close <= hi:
                raise ValueError("Inconsistent OHLC")
            days.append((t.date(), hi, lo, close, vol))
        except (ValueError, KeyError, TypeError):
            continue
    unique = {d[0]: d for d in days}
    values = sorted(unique.values())
    if len(values) < 51:
        raise ValueError("At least 51 completed daily bars needed for trend and volatility")
    values = values[-51:]
    closes = [v[3] for v in values]
    ranges = []
    for prev, cur in zip(values[-15:-1], values[-14:]):
        ranges.append(max(cur[1] - cur[2], abs(cur[1] - prev[3]), abs(cur[2] - prev[3])))
    if len(ranges) != 14:
        raise ValueError("Incomplete ATR window")
    return {
        "sma20": sum(closes[-20:]) / Decimal(20),
        "sma50": sum(closes[-50:]) / Decimal(50),
        "atr14": sum(ranges) / Decimal(14),
        "average_daily_dollar_volume": sum((d[3] * d[4] for d in values[-20:]), ZERO) / Decimal(20),
        "last_completed_session": values[-1][0].isoformat(),
        "daily_bars": len(values),
    }


def regular_session(now: datetime):
    local = now.astimezone(NY)
    return local.weekday() < 5 and (9, 30) <= (local.hour, local.minute) < (16, 0)


async def vendor_snapshot(ticker: str) -> dict:
    if not SYMBOL.fullmatch(ticker):
        raise ValueError("Invalid ticker")
    cfg = configuration()
    if not cfg["configured"]:
        return {"ticker": ticker, "status": "not_configured", "feed": cfg["feed"],
                "execution_enabled": False, "reason": cfg["coverage_note"]}
    now = datetime.now(timezone.utc)
    feed = cfg["feed"]
    try:
        quote, trade, bars = await asyncio.gather(
            api_json(f"/v2/stocks/{ticker}/quotes/latest", {"feed": feed}),
            api_json(f"/v2/stocks/{ticker}/trades/latest", {"feed": feed}),
            api_json(f"/v2/stocks/{ticker}/bars", {
                "feed": feed, "timeframe": "1Day", "limit": 100, "sort": "asc",
                "adjustment": "raw", "start": (now - timedelta(days=120)).date().isoformat(),
                "end": now.date().isoformat(),
            }, max_bytes=400_000),
        )
        q = quote["quote"]; tr = trade["trade"]
        bid, ask, last = positive(q["bp"]), positive(q["ap"]), positive(tr["p"])
        qt, tt = timestamp(q["t"]), timestamp(tr["t"])
        if bid > ask or last < bid * Decimal("0.97") or last > ask * Decimal("1.03"):
            raise ValueError("Invalid or crossed best bid/ask")
        trend = calculate_bars(bars["bars"], now)
        quote_age = (now - qt).total_seconds()
        trade_age = (now - tt).total_seconds()
        fresh = regular_session(now) and 0 <= quote_age <= 90 and 0 <= trade_age <= 90
        source = f"https://data.alpaca.markets/v2/stocks/{ticker}/quotes/latest"
        return {
            "ticker": ticker, "status": "fresh" if fresh else "stale_or_closed",
            "feed": feed, "consolidated": cfg["consolidated"],
            "bid": str(bid), "ask": str(ask), "last": str(last),
            "quote_at": qt.isoformat(), "trade_at": tt.isoformat(),
            "quote_age_seconds": round(quote_age), "trade_age_seconds": round(trade_age),
            "sma20": str(trend["sma20"].quantize(Decimal("0.01"))),
            "sma50": str(trend["sma50"].quantize(Decimal("0.01"))),
            "atr14": str(trend["atr14"].quantize(Decimal("0.01"))),
            "average_daily_dollar_volume": str(trend["average_daily_dollar_volume"].quantize(Decimal("0.01"))),
            "last_completed_session": trend["last_completed_session"],
            "source_url": source,
            "execution_enabled": False,
            "trade_ticket_ready": False,
            "ticket_blockers": [
                "Portfolio freshness, AI issuer credit spreads and market breadth not verified here",
                "Near-term issuer earnings calendar not connected",
                "Broker orders are disabled",
                *([] if cfg["consolidated"] else ["IEX is partial-exchange, not SIP consolidated market"]),
                *([] if fresh else ["Market session closed or quote older than 90 seconds"]),
            ],
        }
    except (httpx.HTTPError, asyncio.TimeoutError, KeyError, TypeError, ValueError):
        return {"ticker": ticker, "status": "unavailable", "feed": feed,
                "execution_enabled": False, "reason": "Vendor response/entitlement missing or invalid; no fabricated quote"}
