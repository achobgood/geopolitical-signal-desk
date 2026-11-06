"""A later calculation must never be backdated into historical market alerts."""
import asyncio
from datetime import datetime, timezone
import json
from app.market import snapshot
from test_market_volatility_study import recorded_history


def test_source_first_seen_and_calculation_times_gate_historical_study():
    source={
        "id":18,"source_url":"https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VXN_History.csv",
        "source_title":"Official Cboe VXN daily close",
        "publisher":"Cboe Global Markets",
        "published_at":"2026-11-05T22:40:27Z",
        "collected_at":"2026-11-05T22:40:27Z",
        "event_at":"2026-11-05T00:00:00Z",
        "family":"market_breadth","phase":"confirmation",
        "pressure":"ambiguous","claim_status":"primary_document",
        "origin_group":"cboe_vxn:2026-11-05",
        "metric_code":"vxn_daily_close","metric_value":"38.26",
        "summary":"Observed volatility index level, not an equity price",
        "caveats":"Not an order.",
    }
    technical={
        "id":2,"symbol":"VXN","source_observation_id":18,
        "provider":"cboe_vxn","instrument_kind":"volatility_index",
        "observed_session":"2026-11-05",
        "captured_at":"2026-11-06T00:15:02Z",
        "calculated_at":"2026-11-06T00:15:08Z",
        "method_version":"cross-timeframe-pivots-wilder-v1",
        "analysis":{"trade_ticket_approved":False,"close":"38.26"},
        "input_bars":recorded_history(160),
    }
    class Store:
        async def rows(self,table,**kw):
            if table=="market_observations":return [source]
            if table=="market_technical_snapshots":return [technical]
            return []
    def at(s):return datetime.fromisoformat(s.replace("Z","+00:00"))
    before=asyncio.run(snapshot(Store(),as_of=at("2026-11-05T23:00:00Z"),mode="replay"))
    assert before["retrospective_volatility_studies"]==[]
    after=asyncio.run(snapshot(Store(),as_of=at("2026-11-06T00:30:00Z"),mode="live"))
    assert len(after["retrospective_volatility_studies"])==1
    item=after["retrospective_volatility_studies"][0]
    assert item["source_observation_id"]==18
    assert item["source_first_collected_at"]=="2026-11-05T22:40:27Z"
    assert item["source_first_analyzed_at"]=="2026-11-06T00:15:08+00:00"
    assert item["signal_was_recorded_at_past_dates"] is False
    assert item["no_stock_or_portfolio_return"] is True
    assert item["horizons"]["5"]["all_eligible_windows"]==150
    assert "input_bars" not in json.dumps(item)
