"""Two-stage market synthesis: model only after deterministic gate approves risk-on."""
import asyncio
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app, requests
from app.store import get_store
from app.trade_orchestrator import AutoTradeRequest, run_auto_synthesis
from app.trade_synthesis import TradeSynthesisRequest
from test_trade_synthesis import NOW, request_dict


def composed(risk_on=True):
    return AutoTradeRequest.model_validate({
        "trade": request_dict(risk_on=risk_on),
        "research": {
            "as_of": (NOW - timedelta(minutes=1)).isoformat(),
            "focus": "ai_infrastructure",
            "universe": ["NVDA", "MSFT"],
            "max_ideas": 2,
        },
    })


def test_no_paid_ai_call_when_credit_breadth_regime_missing(monkeypatch):
    import app.trade_orchestrator as module
    async def forbidden(*args):
        raise AssertionError("Must not run AI model when regime is missing")
    monkeypatch.setattr(module, "propose_research", forbidden)
    body = composed()
    invalid = body.trade.model_copy(
        update={"indicators": body.trade.indicators.model_copy(
            update={"ai_credit_spread_change_5d_bp": None})}
    )
    request = body.model_copy(update={"trade": invalid})
    result = asyncio.run(run_auto_synthesis(None, request))
    assert result["status"] == "NO_TRADE"
    assert result["research"]["status"] == "SKIPPED"
    assert not result["tickets"]


def test_risk_off_trims_without_ai_model_spend(monkeypatch):
    import app.trade_orchestrator as module
    async def forbidden(*args):
        raise AssertionError("Known risk-off trim needs no LLM")
    monkeypatch.setattr(module, "propose_research", forbidden)
    result = asyncio.run(run_auto_synthesis(None, composed(risk_on=False)))
    # NOW is historical fixture: runtime would fail safe unless a fixed, explicit time
    assert result["status"] == "NO_TRADE"
    assert not result["tickets"]


def test_risk_on_model_shortlist_cannot_control_order_price(monkeypatch):
    import app.trade_orchestrator as module
    import app.trade_synthesis as gates

    real = gates.synthesize
    def at_fixed_now(request):
        return real(request, now=NOW)
    monkeypatch.setattr(module, "synthesize", at_fixed_now)

    async def mock_model(store, research):
        return {
            "status": "RESEARCH_ONLY",
            "state": "risk_on", "state_rationale": "Only fixture research context.",
            "gaps": "Simulated model evidence without a true market provider.",
            "model": "test-research-model",
            "ideas": [{
                "ticker": "MSFT", "bias": "consider_buy",
                "thesis": "Macro stress reduced, breadth improved and price trend confirmed.",
                "what_would_change_mind": "Credit widens and broad market falls below average.",
                "source_ids": [1],
            }],
        }
    monkeypatch.setattr(module, "propose_research", mock_model)
    result = asyncio.run(run_auto_synthesis(None, composed(risk_on=True)))
    assert result["status"] == "PROPOSAL_ONLY"
    assert result["tickets"][0]["symbol"] == "MSFT"
    assert result["tickets"][0]["limit_price"] == "100.10"
    assert result["research"]["candidate_symbols"] == ["MSFT"]
    assert result["automatic_execution"] is False


def test_no_buy_from_watch_or_avoid_model_bias(monkeypatch):
    import app.trade_orchestrator as module
    import app.trade_synthesis as gates
    real = gates.synthesize
    monkeypatch.setattr(module, "synthesize", lambda request: real(request, now=NOW))
    async def watch_only(store, request):
        return {
            "status": "RESEARCH_ONLY", "state": "risk_on",
            "state_rationale": "Model watch only.",
            "gaps": "No suitable entry.",
            "ideas": [{"ticker":"MSFT", "bias":"watch","thesis":"wait","what_would_change_mind":"more evidence","source_ids":[1]}],
        }
    monkeypatch.setattr(module, "propose_research", watch_only)
    result = asyncio.run(run_auto_synthesis(None, composed()))
    assert result["status"] == "NO_TRADE"
    assert result["tickets"] == []


class FakeStore:
    def __init__(self):
        self.calls = []
    async def request(self, method, path, **kw):
        self.calls.append(path)
        return [{"id": 1}, {"id": 2}]
    async def rpc(self, name, payload):
        assert name == "market_record_decision_v1"
        return {"stored_privately": True, "orders_placed": False, "decision_id": 3}


def test_auto_route_rejects_unauthorized_portfolio(monkeypatch):
    monkeypatch.setenv("SIGNAL_DESK_API_KEY", "trade-owner-key-" * 4)
    requests.clear()
    store = FakeStore()
    app.dependency_overrides[get_store] = lambda: store
    body = composed().model_dump(mode="json")
    try:
        with TestClient(app) as client:
            assert client.post("/v1/market/trade-synthesis/auto", json=body).status_code == 401
            assert store.calls == []
            owner = {"Authorization": "Bearer " + "trade-owner-key-" * 4}
            result = client.post("/v1/market/trade-synthesis/auto", json=body, headers=owner)
            assert result.status_code == 200
            assert result.json()["status"] == "NO_TRADE"
            assert result.json()["automatic_execution"] is False
            assert store.calls == ["market_observations"]
    finally:
        app.dependency_overrides.clear()
