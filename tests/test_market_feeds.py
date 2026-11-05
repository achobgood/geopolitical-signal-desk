"""Official-source market-feed tests: no hindsight, no fabricated quotes, bounded parsing."""
import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app, requests
from app.market_feeds import (
    TREASURY_URL_TEMPLATE,
    as_observation, cboe_parse, fixed_public_data, run_market_feed, treasury_parse,
)
from app.store import get_store


def treasury_fixture():
    dates = ["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03"]
    values = [(3.5, 4.0), (3.6, 4.05), (3.7, 4.1), (3.75, 4.15), (3.8, 4.3), (3.85, 4.5)]
    body = "".join(
        '<entry><content><m:properties><d:NEW_DATE>' + day +
        'T00:00:00</d:NEW_DATE><d:BC_2YEAR>' + str(x) +
        '</d:BC_2YEAR><d:BC_10YEAR>' + str(y) +
        '</d:BC_10YEAR></m:properties></content></entry>'
        for day, (x, y) in zip(dates, values)
    )
    return ('<feed xmlns="http://www.w3.org/2005/Atom" '
            'xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices" '
            'xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">'
            + body + "</feed>").encode()


VIX_FIXTURE = b'''Market statistics - historical
DATE,OPEN,HIGH,LOW,CLOSE
10/27/2026,15,17,14,16
10/28/2026,16,18,15,17
10/29/2026,17,19,16,18
10/30/2026,18,20,17,19
11/02/2026,19,21,18,20
11/03/2026,23,27,22,26
11/06/2026,30,33,29,32
'''


def test_treasury_six_sessions_and_bp_delta():
    url = TREASURY_URL_TEMPLATE.format(month="202611")
    result = treasury_parse(treasury_fixture(), source_url=url, today=date(2026,11,3))
    assert len(result) == 3
    assert result[0].value == Decimal("3.85")
    assert result[1].value == Decimal("4.5")
    assert result[2].metric == "us_treasury_10y_5session_delta_bp"
    assert result[2].value == Decimal("50.0")
    assert all(x.pressure == "ambiguous" for x in result)


def test_treasury_rejects_bad_or_empty_values():
    with pytest.raises(ValueError):
        treasury_parse(b"<html></html>", source_url="https://home.treasury.gov/", today=date(2026,11,3))
    with pytest.raises(ValueError):
        treasury_parse(b"<feed xmlns='http://www.w3.org/2005/Atom'/>",
                       source_url="https://home.treasury.gov/", today=date(2026,11,3))


def test_volatility_is_confirmation_not_predictive_lead():
    records = cboe_parse(VIX_FIXTURE, provider="cboe_vix",
                         source_url="https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
                         today=date(2026,11,3))
    assert len(records) == 2
    assert records[0].value == Decimal("26")
    assert records[1].value == Decimal("62.50")
    assert records[1].pressure == "risk_up"
    assert records[1].phase == "confirmation"
    assert records[0].session == date(2026,11,3)


def test_official_feed_observation_has_capture_not_fake_publication_time():
    row = cboe_parse(VIX_FIXTURE, provider="cboe_vix",
                     source_url="https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
                     today=date(2026,11,3))[0]
    captured = datetime(2026,11,5,22,0,tzinfo=timezone.utc)
    entry = as_observation(row, captured)
    assert entry.published_at == captured
    assert entry.event_at.isoformat() == "2026-11-03T00:00:00+00:00"
    assert "Exact" not in entry.source_title
    assert entry.family == "market_breadth"


class FakeStore:
    def __init__(self):
        self.data = []
        self.patches = []
        self.calls = []

    async def rpc(self, name, body):
        self.calls.append((name, body))
        if name == "market_feed_claim_v1":
            return {"claimed": True, "run": {"id": 99, "run_key": body["p_key"]}}
        if name == "market_record_observation_v1":
            self.data.append(body["payload"])
            return {"created": True, "observation": {"id": len(self.data)}}
        raise AssertionError("unexpected RPC")

    async def patch(self, table, row_id, body):
        self.patches.append((table, row_id, body))
        return [body]

    async def rows(self, table, **kw):
        return []


def test_market_pipeline_persists_readings_and_run_receipt(monkeypatch):
    async def fetch(url, **kwargs):
        if "treasury" in url:
            return treasury_fixture()
        return VIX_FIXTURE
    monkeypatch.setattr("app.market_feeds.fixed_public_data", fetch)
    import app.market_feeds as feeds
    original_datetime = feeds.datetime
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026,11,5,22,30,tzinfo=timezone.utc)
    monkeypatch.setattr(feeds, "datetime", FixedDateTime)
    store = FakeStore()
    result = asyncio.run(run_market_feed(store, "test:market:20261105", "manual"))
    assert result["run"]["status"] == "partial"
    assert {p["status"] for p in result["run"]["provider_results"] if p["provider"] != "sec_edgar"} == {"stale"}
    assert any(p["provider"] == "sec_edgar" and p["status"] == "not_configured"
               for p in result["run"]["provider_results"])
    assert len(store.data) == 7
    assert len(store.patches) == 1
    assert store.patches[0][0] == "market_feed_runs"
    assert all(x["published_at"].startswith("2026-11-05") for x in store.data)
    monkeypatch.setattr(feeds, "datetime", original_datetime)


def test_cboe_source_allowlist_rejects_untrusted_hosts():
    with pytest.raises(ValueError):
        asyncio.run(fixed_public_data("https://evil.example/steal"))


def test_scheduler_is_isolated_from_general_api_and_public_route(monkeypatch):
    monkeypatch.setenv("SIGNAL_DESK_API_KEY", "test-market-feed-"*3)
    monkeypatch.setenv("SIGNAL_DESK_SCHEDULER_KEY_SHA256", "0"*64)
    requests.clear()
    store = FakeStore()
    app.dependency_overrides[get_store] = lambda: store
    try:
        with TestClient(app) as client:
            assert client.post("/v1/market/feeds/scheduled", json={"run_key":"x:20261105"}).status_code == 401
            assert client.post("/v1/market/feeds/scheduled",
                               headers={"Authorization":"Bearer garbage"}, json={"run_key":"x:20261105"}).status_code == 401
            assert not store.calls
    finally:
        app.dependency_overrides.clear()


def test_completeness_requires_latest_expected_session_not_older_business_day():
    from app.market import expected_completed_market_session
    assert expected_completed_market_session(datetime(
        2026, 11, 5, 22, 40, tzinfo=timezone.utc)).isoformat() == "2026-11-05"
    # Before the close, the previous session is the latest expected complete daily bar.
    assert expected_completed_market_session(datetime(
        2026, 11, 5, 16, 0, tzinfo=timezone.utc)).isoformat() == "2026-11-04"
    # Weekends roll back to Friday.
    assert expected_completed_market_session(datetime(
        2026, 11, 7, 23, 0, tzinfo=timezone.utc)).isoformat() == "2026-11-06"
