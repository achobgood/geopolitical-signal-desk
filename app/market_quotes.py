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
            "iex_realtime":"IEX is partial-exchange data from one trading venue, not consolidated U.S. market quotes",
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


async def vendor_technical_research(ticker: str, adjustment: str = "split") -> dict:
    """Owner-only real-price research; never a quote, portfolio or order ticket."""
    if not SYMBOL.fullmatch(ticker):
        raise ValueError("Invalid ticker")
    cfg = configuration()
    if not cfg["configured"]:
        return {
            "symbol":ticker,"status":"NOT_CONFIGURED","feed":cfg["feed"],
            "mode":cfg["mode"],"execution_enabled":False,
            "blockers":["Read-only vendor access not configured"],
        }
    if adjustment not in ("split","all"):
        raise ValueError("Invalid corporate-action adjustment")
    try:
        from app.market_alpaca_history import fetch_historical_equity
        from app.market_technicals import analyze_bars
        history = await fetch_historical_equity(ticker, adjustment=adjustment)
        if history.get("status") == "NOT_CONFIGURED":
            return history
        bars = history.pop("bars")
        observed = datetime.fromisoformat(history["first_checked_at"])
        analysis = analyze_bars(
            bars[-320:],captured_at=observed,symbol=ticker,instrument_kind="stock"
        )
        analysis.pop("input_bars", None)
        return {
            "symbol":ticker,
            "status":history["status"],
            "analysis":analysis,
            "feed":history["feed"],
            "mode":history["mode"],
            "adjustment":history["adjustment"],
            "adjustment_description":history["adjustment_description"],
            "consolidated":history["historical_consolidated"],
            "historical_consolidated":history["historical_consolidated"],
            "realtime_consolidated":history["realtime_consolidated"],
            "last_vendor_session":history["quality"]["last_session"],
            "expected_completed_session":history["expected_completed_session"],
            "first_checked_at":history["first_checked_at"],
            "request_pages":history["request_pages"],
            "quality":history["quality"],
            "source_url":history["source_url"],
            "execution_enabled":False,
            "blockers":[
                "Research-only adjusted daily OHLC, not live executable bid/ask",
                "Live consolidated quote, broker account, tax lots and earnings calendar not verified",
                "Issuer credit spreads and broad market breadth not connected",
                *([] if history["historical_consolidated"] else
                  ["IEX research is single-venue partial-exchange data"]),
                *([] if history["status"]=="RESEARCH_ONLY" else
                  ["Latest complete vendor daily session is stale"]),
                "No automatic brokerage order execution",
            ],
        }
    except (httpx.HTTPError,asyncio.TimeoutError,KeyError,ValueError,TypeError,IndexError):
        return {
            "symbol":ticker,"status":"UNAVAILABLE",
            "mode":cfg["mode"],"execution_enabled":False,
            "reason":"Market-data source unavailable or failed validation; no fabricated prices",
        }
