"""Market domain contract, temporal replay and API authorization regression tests."""
import copy
import asyncio
from datetime import datetime, timezone
import hashlib

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app, requests
from app.market import MarketAssessment, MarketObservation, PUBLIC_REQUESTS, eligible, radar, snapshot
from app.store import get_store

AUTH = {"Authorization": "Bearer " + "test-market-key-" * 3}
OBSERVATION = {
    "source_url": "https://example.org/finance/credit-warns",
    "source_title": "AI bond market tests financing demand",
    "publisher": "Example Financial Journal",
    "published_at": "2026-10-15T12:00:00Z",
    "family": "ai_credit",
    "phase": "leading",
    "pressure": "risk_up",
    "claim_status": "reported",
    "origin_group": "example-original-2026-10-15",
    "summary": "AI credit-market financing costs widened as lenders became more selective.",
    "tickers": ["CRWV", "ORCL"],
    "caveats": "A reported market price is not independent proof of systemic collapse.",
}
EARLY = {
    **OBSERVATION, "id": 1, "collected_at": "2026-11-05T22:00:00Z",
}
CUT = datetime.fromisoformat("2026-11-03T20:00:00+00:00")


class FakeMarketStore:
    def __init__(self, observations=None, assessments=None):
        self.observations = observations or []
        self.assessments = assessments or []
        self.calls = []

    async def rows(self, table, **kwargs):
        self.calls.append(("rows", table, kwargs))
        if table == "market_observations":
            return copy.deepcopy(self.observations)
        if table == "market_assessments":
            return copy.deepcopy(self.assessments)
        return []

    async def request(self, method, path, **kwargs):
        self.calls.append(("request", path, kwargs))
        if path == "market_observations":
            ids = kwargs["params"]["id"].removeprefix("in.(").removesuffix(")").split(",")
            return [{k: row[k] for k in ("id", "published_at", "collected_at")}
                    for row in self.observations if str(row["id"]) in ids]
        return []

    async def rpc(self, name, payload):
        self.calls.append(("rpc", name, payload))
        if name == "market_record_assessment_v1":
            return {"created": True, "assessment": {"id": 4, **payload["payload"]}}
        return {"created": True, "observation": {"id": 7}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SIGNAL_DESK_API_KEY", "test-market-key-" * 3)
    monkeypatch.delenv("SIGNAL_DESK_SEARCH_WRITER_KEY_SHA256", raising=False)
    monkeypatch.delenv("SIGNAL_DESK_READ_KEY_SHA256", raising=False)
    requests.clear()
    PUBLIC_REQUESTS.clear()
    fake = FakeMarketStore([EARLY])
    app.dependency_overrides[get_store] = lambda: fake
    with TestClient(app) as c:
        c.fake = fake
        yield c
    app.dependency_overrides.clear()


def test_all_private_market_operations_require_writer_or_reader_auth(client):
    for path in ("/v1/market/snapshot", "/v1/market/observations", "/v1/market/assessments"):
        assert client.get(path).status_code == 401
    for path, body in (
        ("/v1/market/observations", OBSERVATION),
        ("/v1/market/assessments", {
            "as_of": "2026-11-03T20:00:00Z", "mode": "replay",
            "horizon_sessions": 5, "scenario": "further_downside", "confidence": "low",
            "thesis": "Credit stress may broaden into equity repricing.",
            "countercase": "Cash flow and issuance demand may remain resilient.",
            "falsification": "Credit spreads tighten durably and projects remain funded.",
            "next_check": "Compare new bond yields and actual issuance access.",
            "coverage_gaps": "No broad live price data or full capex disclosure available.",
            "evidence_ids": [1],
        }),
    ):
        assert client.post(path, json=body).status_code == 401
    assert client.fake.calls == []


def test_public_market_only_queries_market_tables(client):
    assert client.get("/market").status_code == 200
    r = client.get("/v1/market/public-snapshot?mode=replay&as_of=2026-11-03T20:00:00Z")
    assert r.status_code == 200
    assert r.json()["desk"] == "market"
    assert all(call[1].startswith("market_") for call in client.fake.calls)
    assert r.json()["radar"]["posture"] == "CONCENTRATED WATCH"


@pytest.mark.parametrize("field,value", [
    ("source_url", "http://example.org/finance"), ("source_url", "https://127.0.0.1/finance"),
    ("published_at", "2026-10-15T12:00:00"), ("metric_value", "Not a price"),
    ("publisher", ""), ("caveats", ""), ("tickers", ["CRWV", "crwv"]),
    ("claim_status", "confirmed-collapse"), ("hypothetical_portfolio_id", "secret"),
])
def test_market_observations_reject_invalid_provenance(field, value):
    values = {**OBSERVATION, field: value}
    with pytest.raises(ValidationError):
        MarketObservation.model_validate(values)


def test_metric_value_requires_code_and_unit():
    with pytest.raises(ValidationError):
        MarketObservation.model_validate({**OBSERVATION, "metric_value": "1200"})
    valid = MarketObservation.model_validate({
        **OBSERVATION, "metric_code": "crwv_cds_bps", "metric_value": "1200",
        "metric_unit": "bps",
    })
    assert valid.metric_value == 1200


def test_source_historical_replay_is_not_live_prior_detection():
    assert eligible(EARLY, CUT, "replay")
    assert not eligible(EARLY, CUT, "live")
    simulated = radar([EARLY], as_of=CUT, mode="replay")
    actual = radar([EARLY], as_of=CUT, mode="live")
    assert simulated["eligible_observations"] == 1
    assert simulated["actual_earlier_alert"] is False
    assert actual["eligible_observations"] == 0


def test_source_lineage_deduplicates_reposts_and_never_creates_odds():
    duplicate = {**EARLY, "id": 2, "source_url": "https://syndicator.example.net/another-link"}
    r = radar([EARLY, duplicate], as_of=CUT, mode="replay")
    assert r["risk_families"][0]["distinct_origin_groups"] == 1
    assert "probability" not in r
    assert r["posture"] == "CONCENTRATED WATCH"


def test_published_future_evidence_excluded_even_during_replay():
    later = {**EARLY, "published_at": "2026-11-05T20:00:00Z"}
    r = radar([EARLY, later], as_of=CUT, mode="replay")
    assert r["eligible_observations"] == 1
    assert r["observations_from_future_excluded"] == 1


def test_assessment_requires_available_evidence(client):
    body = {
        "as_of": "2026-11-03T20:00:00Z", "mode": "live", "horizon_sessions": 5,
        "scenario": "further_downside", "confidence": "low",
        "thesis": "Credit funding conditions may constrain infrastructure buildout.",
        "countercase": "Large investment-grade issuance may continue at workable terms.",
        "falsification": "CoreWeave and peers regain funding access on improved terms.",
        "next_check": "Inspect latest credit spreads and primary issuance results.",
        "coverage_gaps": "No independent live credit feed and incomplete market breadth.",
        "evidence_ids": [1],
    }
    assert client.post("/v1/market/assessments", headers=AUTH, json=body).status_code == 409
    assert not any(c[0] == "rpc" for c in client.fake.calls)
    body["mode"] = "replay"
    result = client.post("/v1/market/assessments", headers=AUTH, json=body)
    assert result.status_code == 200
    assert result.json()["assessment"]["mode"] == "replay"


def test_read_only_and_search_writer_cannot_mutate_market(client, monkeypatch):
    readonly = "readonly-market-token-" * 3
    writer = "search-only-market-token-" * 3
    monkeypatch.setenv("SIGNAL_DESK_READ_KEY_SHA256", hashlib.sha256(readonly.encode()).hexdigest())
    monkeypatch.setenv("SIGNAL_DESK_SEARCH_WRITER_KEY_SHA256", hashlib.sha256(writer.encode()).hexdigest())
    assert client.get("/v1/market/snapshot", headers={"Authorization": "Bearer " + readonly}).status_code == 200
    assert client.post("/v1/market/observations",
                       headers={"Authorization": "Bearer " + readonly}, json=OBSERVATION).status_code == 403
    assert client.post("/v1/market/observations",
                       headers={"Authorization": "Bearer " + writer}, json=OBSERVATION).status_code == 403


def test_replay_fails_closed_on_naive_or_future_cutoff(client):
    assert client.get("/v1/market/public-snapshot?mode=replay&as_of=2026-11-03T20:00:00").status_code == 422
    assert client.get("/v1/market/public-snapshot?mode=replay&as_of=2050-01-01T00:00:00Z").status_code == 422


def test_no_market_claims_when_data_missing():
    r = radar([], as_of=datetime.now(timezone.utc), mode="live")
    assert r["posture"] == "INSUFFICIENT OBSERVATIONS"
    assert not r["risk_families"]


def test_public_endpoint_bounded_for_free_service(client):
    for _ in range(60):
        assert client.get("/v1/market/public-snapshot?mode=replay&as_of=2026-11-03T20:00:00Z").status_code == 200
    assert client.get("/v1/market/public-snapshot").status_code == 429


def test_live_historical_view_never_shows_backfilled_future_assessment_or_feed():
    class TimedStore(FakeMarketStore):
        async def rows(self, table, **kwargs):
            if table == "market_feed_runs":
                return [{
                    "finished_at": "2026-11-05T22:00:00Z",
                    "status": "completed",
                    "provider_results": [{"provider": "cboe_vix", "status": "completed", "latest_session": "2026-11-05"}],
                }]
            return await super().rows(table, **kwargs)

    scenarios = [
        {
            "id": 1, "as_of": "2026-11-02T20:00:00Z",
            "created_at": "2026-11-05T22:00:00Z",
            "mode": "live", "scenario": "further_downside",
            "thesis": "Retrospective later assessment must not masquerade as a live decision.",
        },
        {
            "id": 2, "as_of": "2026-11-02T20:00:00Z",
            "created_at": "2026-11-05T22:00:00Z",
            "mode": "replay", "scenario": "further_downside",
            "thesis": "Explicit retrospective case study.",
        },
    ]
    store = TimedStore(observations=[EARLY], assessments=scenarios)
    live_then = asyncio.run(snapshot(store, as_of=CUT, mode="live"))
    assert live_then["scenarios"] == {}
    assert live_then["feed_health"] == {}
    replay_then = asyncio.run(snapshot(store, as_of=CUT, mode="replay"))
    assert replay_then["scenarios"]["further_downside"]["mode"] == "replay"
    assert replay_then["feed_health"] == {}
    live_now = asyncio.run(snapshot(
        store, as_of=datetime(2026, 11, 5, 23, 0, tzinfo=timezone.utc), mode="live"
    ))
    assert live_now["scenarios"]["further_downside"]["id"] == 1
    assert live_now["feed_health"]["cboe_vix"]["status"] == "completed"
