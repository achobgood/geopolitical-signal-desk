"""Real live Cboe source acceptance. No fixtures, accounts, broker, or cached data.

Exit nonzero if official source cannot be fetched or cannot be processed.
Daily source freshness is separately reported, not falsely labeled success.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app.market import expected_completed_market_session
from app.market_feeds import CBOE_URLS, fixed_public_data
from app.market_technicals import analyze_bars, parse_cboe_history


async def main():
    captured = datetime.now(timezone.utc)
    cutoff = expected_completed_market_session(captured)
    records = []
    for provider, url in CBOE_URLS.items():
        raw = await fixed_public_data(url)
        bars = parse_cboe_history(raw, cutoff_session=cutoff)
        symbol = "VIX" if provider == "cboe_vix" else "VXN"
        analysis = analyze_bars(
            bars, captured_at=datetime.now(timezone.utc), symbol=symbol,
            instrument_kind="volatility_index"
        )
        assert analysis["daily_sessions"] >= 60
        assert analysis["trade_ticket_approved"] is False
        assert analysis["source_is_tradeable_instrument"] is False
        assert analysis["bars_sha256"] and len(analysis["bars_sha256"]) == 64
        last = bars[-1].session
        records.append({
            "provider": provider,
            "url": url,
            "current_expected_complete_session": cutoff.isoformat(),
            "source_last_session": last.isoformat(),
            "freshness": "current_complete_session" if last >= cutoff else "stale",
            "daily_bars": analysis["daily_sessions"],
            "input_sha256": analysis["bars_sha256"],
            "close": analysis["close"],
            "rsi14": analysis["rsi14"],
            "atr14": analysis["atr14"],
            "sma20": analysis["sma20"],
            "sma50": analysis["sma50"],
            "confirmed_daily_weekly_zones": len(analysis["confirmed_zones"]),
            "setup_state": analysis["setup"]["status"],
            "trade_execution_enabled": False,
        })
    if len(records) != 2:
        raise AssertionError("Both actual Cboe VIX/VXN sources required")
    print("LIVE_OFFICIAL_CBOE_QUANT_ACCEPTED")
    print(json.dumps(records,sort_keys=True,indent=2))


if __name__ == "__main__":
    asyncio.run(main())
