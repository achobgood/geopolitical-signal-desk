"""Market watch receipts are actual, timestamped detections, not retroactive forecasts."""
import asyncio
from datetime import datetime, timezone
import hashlib

from fastapi.testclient import TestClient
from app.main import app, requests
from app.market import snapshot
from app.store import get_store

SOURCE = {
    "id": 19, "source_url": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VXN_History.csv",
    "source_title": "CBOE VXN five-session change 2026-11-05",
    "publisher": "Cboe Global Markets",
    "published_at": "2026-11-05T22:40:27Z",
    "collected_at": "2026-11-05T22:40:27Z",
    "event_at": "2026-11-05T00:00:00Z",
    "origin_group": "cboe_vxn:2026-11-05",
    "family": "market_breadth",
    "phase": "confirmation",
    "pressure": "risk_up",
    "claim_status": "primary_document",
    "metric_code": "vxn_5session_change_pct", "metric_value": 51.95,
    "metric_unit": "percent", "summary": "Measured volatility change",
    "caveats": "Not a stock quote or market forecast.",
}
WATCH = {
    "id": 1, "observation_id": 19,
    "rule_id": "vxn_5day_spike", "observed_session": "2026-11-05",
    "source_first_seen_at": "2026-11-05T22:40:27Z",
    "first_detected_at": "2026-11-05T23:15:00Z",
    "observed_value": 51.95, "threshold": 20,
    "unit": "percent", "direction": "risk_up",
    "limitations": "Descriptive attention threshold only.",
}


class Store:
    async def rows(self, table, **kw):
        if table == "market_observations":
            return [SOURCE]
        if table == "market_watch_signals":
            return [WATCH]
        return []


def as_of(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_actual_first_detection_never_backdates_to_publication_or_source_ingest():
    before = asyncio.run(snapshot(Store(), as_of=as_of("2026-11-05T22:50:00Z"), mode="live"))
    assert before["radar"]["eligible_observations"] == 1
    assert before["watch_signals"] == []
    after = asyncio.run(snapshot(Store(), as_of=as_of("2026-11-05T23:30:00Z"), mode="live"))
    signal = after["watch_signals"][0]
    assert signal["source_first_seen_at"] == "2026-11-05T22:40:27Z"
    assert signal["first_detected_at"] == "2026-11-05T23:15:00Z"
    assert signal["not_an_advance_alert"] is True
    assert signal["signal_type"] == "market_confirmation_only"
    assert signal["source_observation_id"] == 19
    assert signal["source_url"].startswith("https://cdn-api.cboe.com/")


def test_retrospective_replay_does_not_invent_prior_signal():
    early = asyncio.run(snapshot(Store(), as_of=as_of("2026-11-04T20:00:00Z"), mode="replay"))
    assert early["watch_signals"] == []
    same_day = asyncio.run(snapshot(Store(), as_of=as_of("2026-11-05T23:00:00Z"), mode="replay"))
    assert same_day["watch_signals"] == []


def test_private_watch_table_route_requires_read_only_or_owner_auth(monkeypatch):
    owner = "watch-owner-secret-" * 3
    read = "watch-reader-secret-" * 3
    monkeypatch.setenv("SIGNAL_DESK_API_KEY", owner)
    monkeypatch.setenv("SIGNAL_DESK_READ_KEY_SHA256", hashlib.sha256(read.encode()).hexdigest())
    requests.clear()
    app.dependency_overrides[get_store] = Store
    try:
        with TestClient(app) as client:
            assert client.get("/v1/market/watch-signals").status_code == 401
            response = client.get("/v1/market/watch-signals", headers={
                "Authorization": "Bearer " + read,
            })
            assert response.status_code == 200
            assert response.json()[0]["rule_id"] == "vxn_5day_spike"
            assert client.get("/v1/market/public-snapshot?mode=replay&as_of=2026-11-04T20:00:00Z").json()["watch_signals"] == []
    finally:
        app.dependency_overrides.clear()
