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
    """Keep historical SIP Basic separate from entitled SIP realtime.

    Legacy ALPACA_DATA_FEED is retained for older deployments. New deployments
    should set explicit ALPACA_DATA_MODE to remove any entitlement ambiguity.
    """
    requested = os.getenv("ALPACA_DATA_MODE", "").strip().lower()
    legacy = os.getenv("ALPACA_DATA_FEED", "disabled").strip().lower()
    if not requested:
        requested = {"sip":"entitled_sip_realtime", "iex":"iex_realtime"}.get(legacy,"disabled")
    modes = {"basic_sip_delayed":"sip", "iex_realtime":"iex",
             "entitled_sip_realtime":"sip"}
    feed = modes.get(requested,"disabled")
    key = os.getenv("ALPACA_MARKET_DATA_KEY_ID")
    secret = os.getenv("ALPACA_MARKET_DATA_SECRET")
    configured = bool(key and secret and requested in modes)
    return {
        "provider": "alpaca_data_api",
        "configured": configured,
        "mode": requested if requested in modes else "disabled",
        "feed": feed,
        "scope": "read_only_data_host",
        # A historical SIP series can be consolidated without granting
        # subscription rights to realtime SIP latest/stream endpoints.
        "historical_consolidated": feed == "sip",
        "realtime_consolidated": requested == "entitled_sip_realtime",
        "consolidated": feed == "sip",
        "execution_enabled": False,
        "coverage_note": {
            "basic_sip_delayed":"Free historical SIP bars, limited to data older than 15 minutes; NO SIP realtime quotes",
            "iex_realtime":"Realtime IEX is one exchange, not a consolidated U.S. market",
            "entitled_sip_realtime":"Realtime SIP requested; server must verify actual vendor entitlement",
        }.get(requested, "Market data credentials and explicit access mode not configured"),
    }

async def api_json(path: str, params: dict, *, max_bytes=1_500_000) -> dict:
    c = configuration()
    if not c["configured"]:
        raise ValueError("Licensed market data credentials or feed configuration missing")
    if not path.startswith("/v2/stocks/") or ".." in path:
        raise ValueError("Invalid market-data path")
    if params.get("feed") != c["feed"]:
        raise ValueError("Vendor feed conflicts with declared entitlement mode")
    if c["mode"] == "basic_sip_delayed":
        if not path.endswith("/bars"):
            raise ValueError("Basic free SIP mode permits historical bars only")
        end_text = params.get("end")
        if not isinstance(end_text,str):
            raise ValueError("Basic SIP historical request needs explicit nonrecent end")
        try:
            end_time = timestamp(end_text)
        except (ValueError,AttributeError) as exc:
            raise ValueError("Basic SIP request end must be timezone-aware") from exc
        if end_time > datetime.now(timezone.utc) - timedelta(minutes=16):
            raise ValueError("Basic SIP historical request would cross the vendor's 15-minute entitlement boundary")
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
    if cfg["mode"] == "basic_sip_delayed":
        return {"ticker":ticker, "status":"historical_only", "feed":"sip",
                "mode":"basic_sip_delayed", "execution_enabled":False,
                "reason":"Alpaca Basic allows older SIP history, not SIP latest quotes or executable order levels"}
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


async def vendor_technical_research(ticker: str) -> dict:
    """Optional authorized daily-OHLC technical analysis. Never a broker order.

    A real vendor response is mandatory. It remains non-orderable without
    an authenticated portfolio, earnings, liquidity and macro risk gate.
    """
    if not SYMBOL.fullmatch(ticker):
        raise ValueError("Invalid ticker")
    cfg = configuration()
    if not cfg["configured"]:
        return {"symbol": ticker, "status": "NOT_CONFIGURED",
                "execution_enabled": False, "feed": cfg["feed"],
                "blockers": ["Owner has not provided licensed market-data credentials"]}
    now = datetime.now(timezone.utc)
    try:
        from app.market import expected_completed_market_session
        from app.market_technicals import Bar, analyze_bars, _number
        expected = expected_completed_market_session(now)
        payload = await api_json(f"/v2/stocks/{ticker}/bars", {
            "feed": cfg["feed"],
            "timeframe": "1Day",
            "limit": 320,
            "sort": "asc",
            "adjustment": "split",
            "start": (now - timedelta(days=315)).date().isoformat(),
            "end": now.date().isoformat(),
        })
        if payload.get("next_page_token"):
            return {"symbol":ticker, "status":"UNAVAILABLE",
                    "execution_enabled":False,
                    "reason":"Vendor returned an incomplete paginated history"}
        series = payload.get("bars")
        if not isinstance(series, list):
            raise ValueError("Missing vendor daily bars")
        by_date = {}
        for row in series:
            session = timestamp(row["t"]).astimezone(NY).date()
            if session > expected:
                continue
            bar = Bar(session=session, open=_number(row["o"]),
                      high=_number(row["h"]), low=_number(row["l"]),
                      close=_number(row["c"]))
            if session in by_date and by_date[session] != bar:
                raise ValueError("Conflicting daily bar for one session")
            by_date[session] = bar
        ordered = [by_date[d] for d in sorted(by_date)]
        analysis = analyze_bars(
            ordered[-320:], captured_at=now, symbol=ticker, instrument_kind="stock"
        )
        analysis.pop("input_bars", None)
        last_day = ordered[-1].session
        fresh = last_day >= expected
        return {
            "symbol": ticker,
            "status": "RESEARCH_ONLY" if fresh else "STALE",
            "analysis": analysis,
            "source_url": f"{HOST}/v2/stocks/{ticker}/bars",
            "vendor": "Alpaca Market Data",
            "feed": cfg["feed"],
            "consolidated": cfg["consolidated"],
            "last_vendor_session": last_day.isoformat(),
            "expected_completed_session": expected.isoformat(),
            "first_checked_at": now.isoformat(),
            "execution_enabled": False,
            "blockers": [
                "No automatic brokerage order execution",
                "Earnings calendar and private portfolio not verified",
                "Issuer CDS and broad market breadth not connected",
                *([] if cfg["consolidated"] else
                  ["IEX is a single exchange, not a consolidated U.S. tape"]),
                *([] if fresh else ["Latest completed daily source session is stale"]),
            ],
        }
    except (httpx.HTTPError, asyncio.TimeoutError, KeyError, ValueError, TypeError, IndexError):
        return {"symbol":ticker, "status":"UNAVAILABLE",
                "execution_enabled":False,
                "reason":"Market-data vendor unreachable, insufficient bars, or invalid data; no fabricated levels"}
