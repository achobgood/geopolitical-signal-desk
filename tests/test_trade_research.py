"""Candidate generation never invents sources, symbols, orders, or private inputs."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app, requests
from app.store import get_store
from app.trade_research import (
    ResearchJudgment, ResearchRequest, public_research_packet, validate_research,
    propose_research,
)

DATE = datetime(2026, 11, 5, 22, 30, tzinfo=timezone.utc)


def request():
    return ResearchRequest(
        as_of=DATE, focus="ai_infrastructure",
        universe=["NVDA", "MSFT", "CRWV", "QQQ", "VOO"], max_ideas=3,
    )


def evidence():
    return [
        {"id": 1, "source_url": "https://example.org/article", "publisher": "Publisher",
         "published_at": "2026-11-03T22:00:00+00:00",
         "collected_at": "2026-11-04T10:00:00+00:00",
         "family": "ai_credit", "phase": "leading",
         "pressure": "risk_up", "claim_status": "reported",
         "origin_group": "source-a", "summary": "Borrower spreads were reported wider.",
         "caveats": "One source, not proof of default.", "tickers": ["CRWV"]},
        {"id": 2, "source_url": "https://example.org/future", "publisher": "Future",
         "published_at": "2026-11-06T10:00:00+00:00",
         "collected_at": "2026-11-06T12:00:00+00:00",
         "family": "ai_credit", "phase": "confirmation",
         "pressure": "risk_up", "claim_status": "reported",
         "origin_group": "future", "summary": "Future event must not contaminate replay.",
         "caveats": "Not available at as-of.", "tickers": ["NVDA"]},
        {"id": 3, "source_url": "https://example.org/not-collected", "publisher": "History",
         "published_at": "2026-10-25T10:00:00+00:00",
         "collected_at": "2026-11-06T12:00:00+00:00",
         "family": "ai_credit", "phase": "leading",
         "pressure": "risk_up", "claim_status": "reported",
         "origin_group": "late-collection", "summary": "Old publication collected after cutoff.",
         "caveats": "May be known historically but not live.", "tickers": ["CRWV"]},
    ]


def judgment(ticker="CRWV", sources=None):
    return ResearchJudgment.model_validate({
        "market_state_hypothesis": "risk_off",
        "state_rationale": "The observed credit repricing could make AI infrastructure funding fragile.",
        "assumptions_and_gaps": "There are no live quotes, and press reports do not establish a market bottom.",
        "candidate_ideas": [{
            "ticker": ticker, "bias": "watch",
            "thesis": "Credit stress deserves monitoring before considering a new equity position.",
            "competing_explanation": "Capital markets remain open for some high-quality AI borrowers.",
            "what_would_change_mind": "Issuance and spreads stabilize while cash flow improves materially.",
            "source_ids": sources or [1],
        }],
    })


def test_research_packet_never_uses_future_or_later_collected_source():
    packet = public_research_packet(evidence(), request())
    assert [r["id"] for r in packet["observations"]] == [1]
    assert "account" not in packet
    assert "portfolio" not in packet
    assert packet["max_ideas"] == 3


def test_candidate_validation_no_hallucinated_symbol_or_source():
    packet = public_research_packet(evidence(), request())
    r = validate_research(judgment(), packet)
    assert r["not_orderable"] is True
    assert r["ideas"][0]["ticker"] == "CRWV"
    with pytest.raises(ValueError):
        validate_research(judgment(ticker="TSLA"), packet)
    with pytest.raises(ValueError):
        validate_research(judgment(sources=[3]), packet)
    with pytest.raises(ValueError):
        validate_research(judgment(sources=[2]), packet)


def test_incomplete_evidence_must_not_generate_research_ideas():
    packet = public_research_packet([], request())
    with pytest.raises(ValueError):
        validate_research(judgment(), packet)


def test_research_request_rejects_duplicate_symbols_and_future_date():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ResearchRequest(as_of=DATE, focus="mixed", universe=["NVDA", "NVDA"])
    with pytest.raises(ValidationError):
        ResearchRequest(as_of=datetime(2050, 1, 1, tzinfo=timezone.utc),
                        focus="mixed", universe=["NVDA","QQQ"])


class FakeDB:
    def __init__(self):
        self.called = False
    async def rows(self, table, **kw):
        self.called = True
        return evidence()


def test_model_disabled_without_secret_and_no_remote_request(monkeypatch):
    import asyncio
    import app.trade_research as research
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(research, "_last_request_at", -research.MODEL_COOLDOWN_SECONDS)
    store = FakeDB()
    r = asyncio.run(propose_research(store, request()))
    assert r["status"] == "DISABLED"
    assert r["not_orderable"]
    assert not store.called


def test_public_callers_cannot_invoke_paid_model_or_access_private_data(monkeypatch):
    monkeypatch.setenv("SIGNAL_DESK_API_KEY", "test-research-owner-" * 4)
    requests.clear()
    db = FakeDB()
    app.dependency_overrides[get_store] = lambda: db
    body = {"as_of":"2026-11-05T22:30:00Z",
            "focus":"ai_infrastructure","universe":["CRWV","NVDA"],"max_ideas":2}
    try:
        with TestClient(app) as client:
            assert client.post("/v1/market/trade-synthesis/research",json=body).status_code == 401
            assert not db.called
            owner = {"Authorization": "Bearer " + "test-research-owner-" * 4}
            monkeypatch.delenv("OPENAI_API_KEY",raising=False)
            r = client.post("/v1/market/trade-synthesis/research",headers=owner,json=body)
            assert r.status_code == 200
            assert r.json()["status"] in ("DISABLED","PACING")
            assert not db.called
    finally:
        app.dependency_overrides.clear()
