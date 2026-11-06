"""Alpaca Basic historical SIP stock/ETF history, strictly older than 15 minutes.

Read-only data.alpaca.markets host, no brokerage/order endpoints, no
unlicensed real-time consolidated quotes. Pagination capped and auditable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app.market_quotes import SYMBOL, configuration, positive, timestamp
from app import market_quotes as vendor
from app.market_technicals import Bar
from app.market import expected_completed_market_session

MAX_PAGES = 4
MAX_BARS = 1500
PAGE_LIMIT = 400
HISTORY_DAYS = 800


def request_params(*, mode: str, now: datetime, adjustment: str,
                   page_token: str | None = None) -> dict:
    if now.utcoffset() is None:
        raise ValueError("Vendor cutoff timestamp must carry timezone")
    if adjustment not in ("split", "all"):
        raise ValueError("Historical adjustment must be split or all; never silently mix")
    feed = {"basic_sip_delayed": "sip", "entitled_sip_realtime": "sip",
            "iex_realtime": "iex"}.get(mode)
    if not feed:
        raise ValueError("Market-data mode not configured")
    cutoff = now.astimezone(timezone.utc) - timedelta(minutes=16) if mode == "basic_sip_delayed" else now.astimezone(timezone.utc)
    params = {
        "feed": feed, "timeframe": "1Day", "limit": PAGE_LIMIT,
        "sort": "asc", "adjustment": adjustment,
        "start": (now - timedelta(days=HISTORY_DAYS)).date().isoformat(),
        "end": cutoff.isoformat().replace("+00:00", "Z"),
    }
    if page_token:
        params["page_token"] = page_token
    return params


def validate_equity_bars(rows: list[dict], *, expected_session) -> tuple[list[Bar], dict]:
    if not isinstance(rows, list):
        raise ValueError("Vendor daily bars must be a list")
    by_day = {}
    count_future = count_holiday = 0
    volume_values = {}
    for record in rows:
        if not isinstance(record, dict):
            raise ValueError("Vendor bar is not a record")
        # Alpaca dates daily bars in the ticker's exchange session. Some daily
        # timestamps are midnight UTC; converting to ET can shift the date.
        session = timestamp(record["t"]).date()
        if session > expected_session:
            count_future += 1
            continue
        if session.weekday() > 4:
            count_holiday += 1
            continue
        bar = Bar(session=session, open=positive(record["o"]), high=positive(record["h"]),
                  low=positive(record["l"]), close=positive(record["c"]))
        try:
            volume = Decimal(str(record["v"]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Vendor volume must be numeric") from exc
        if not volume.is_finite() or volume < 0 or volume > Decimal("1e13"):
            raise ValueError("Invalid traded volume")
        if session in by_day and (by_day[session] != bar or volume_values[session] != volume):
            raise ValueError("Conflicting duplicate daily bar")
        by_day[session] = bar
        volume_values[session] = volume
    dates = sorted(by_day)
    if len(dates) < 60:
        raise ValueError("Fewer than 60 complete equity daily bars available")
    # The data record is not silently backfilled across market closures or halts.
    ordered = [by_day[d] for d in dates]
    dollars = [by_day[d].close * volume_values[d] for d in dates[-20:]]
    return ordered, {
        "excluded_unfinished_sessions": count_future,
        "excluded_weekend_sessions": count_holiday,
        "daily_sessions": len(ordered),
        "first_session": dates[0].isoformat(),
        "last_session": dates[-1].isoformat(),
        "average_dollar_volume_20": str(sum(dollars) / Decimal(len(dollars))),
        "has_volume": True,
    }


async def fetch_historical_equity(ticker: str, *, adjustment="split",
                                  now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if not SYMBOL.fullmatch(ticker):
        raise ValueError("Invalid ticker")
    cfg = configuration()
    if not cfg["configured"]:
        return {"symbol": ticker, "status": "NOT_CONFIGURED", "execution_enabled": False,
                "reason": "Read-only market-data key and permitted mode absent"}
    if cfg["mode"] not in ("basic_sip_delayed","iex_realtime","entitled_sip_realtime"):
        raise ValueError("Unsupported market-data mode")
    if adjustment not in ("split","all"):
        raise ValueError("Unsupported corporate-action adjustment")
    expected = expected_completed_market_session(now)
    all_rows, token, seen = [], None, set()
    params_log = []
    for page in range(MAX_PAGES):
        params = request_params(mode=cfg["mode"], now=now, adjustment=adjustment,
                                page_token=token)
        response = await vendor.api_json(
            f"/v2/stocks/{ticker}/bars", params, max_bytes=1_500_000
        )
        chunk = response.get("bars")
        if not isinstance(chunk, list):
            raise ValueError("Vendor returned no valid daily bars array")
        all_rows.extend(chunk)
        if len(all_rows) > MAX_BARS:
            raise ValueError("Historical source returned more bars than bounded retention")
        params_log.append({"page":page+1,"bar_count":len(chunk)})
        token = response.get("next_page_token")
        if not token:
            break
        if not isinstance(token,str) or len(token) > 1024 or token in seen:
            raise ValueError("Vendor pagination cycle or invalid cursor")
        seen.add(token)
    else:
        raise ValueError("Vendor history exceeded maximum page count")
    bars, quality = validate_equity_bars(all_rows, expected_session=expected)
    quality["last_session_is_current"] = bars[-1].session >= expected
    return {
        "symbol":ticker, "status":"RESEARCH_ONLY" if quality["last_session_is_current"] else "STALE",
        "mode":cfg["mode"],"feed":cfg["feed"],"adjustment":adjustment,
        "adjustment_description":(
            "Split-adjusted history. Dividends are excluded from price return."
            if adjustment=="split" else
            "Split-and-dividend-adjusted historical series. Levels may not equal actual tradable prints."
        ),
        "data_delay_minutes": 16 if cfg["mode"]=="basic_sip_delayed" else 0,
        "historical_consolidated": cfg["historical_consolidated"],
        "realtime_consolidated": cfg["realtime_consolidated"],
        "first_checked_at":now.isoformat(),
        "expected_completed_session":expected.isoformat(),
        "source_url":f"https://data.alpaca.markets/v2/stocks/{ticker}/bars",
        "request_pages": params_log,
        "quality":quality,
        "bars":bars,
        "execution_enabled":False,
    }
