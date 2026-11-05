"""Trade synthesis is proposal-only; exact, size-bounded tickets require all input gates."""
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app, requests
from app.store import get_store
from app.trade_synthesis import TradeSynthesisRequest, synthesize


NOW = datetime(2026, 11, 5, 18, 30, tzinfo=timezone.utc)


def stamp(delta=0):
    return (NOW - timedelta(seconds=delta)).isoformat()


def metric(value, source, age=120):
    return {
        "value": value, "observed_at": stamp(age),
        "source_url": "https://" + source + ".example.com/market-research",
        "origin_group": source + "-verified-daily-feed",
    }


def request_dict(*, risk_on=True, exit_price=None):
    regime = {
        "ai_credit_spread_change_5d_bp": metric("-35" if risk_on else "100", "credit"),
        "market_pct_above_sma50": metric("68" if risk_on else "25", "breadth"),
        "vxn_5d_change_pct": metric("-15" if risk_on else "40", "cboe"),
        "ai_relative_strength_20d_pct": metric("5" if risk_on else "-8", "relative"),
    }
    return {
        "portfolio": {
            "as_of": stamp(30),
            "net_liquidation_value": "100000",
            "available_cash": "50000",
            "positions": [{
                "ticker": "NVDA", "shares": "200", "category": "ai",
                "account_kind": "taxable", "hard_exit_below": exit_price,
            }],
        },
        "indicators": regime,
        "quotes": [
            {"ticker": "NVDA", "category": "ai", "instrument": "stock",
             "last": "100", "bid": "99.95", "ask": "100.05", "atr14": "4",
             "sma20": "105" if not risk_on else "99", "sma50": "110" if not risk_on else "98",
             "average_daily_dollar_volume": "100000000",
             "as_of": stamp(20), "market_session": "regular", "provider": "FixtureQuoteVendor",
             "source_url": "https://example.org/quote/nvda", "earnings_within_3_sessions": False},
            {"ticker": "MSFT", "category": "ai", "instrument": "stock",
             "last": "100", "bid": "99.95", "ask": "100.10", "atr14": "3",
             "sma20": "98", "sma50": "90", "average_daily_dollar_volume": "100000000",
             "as_of": stamp(20), "market_session": "regular", "provider": "FixtureQuoteVendor",
             "source_url": "https://example.org/quote/msft", "earnings_within_3_sessions": False},
        ],
        "candidates": [{
            "ticker": "MSFT", "reason": "Research scenario assumes improving funding and real revenue conversion.",
            "thesis_invalidated_if": "Confirmed credit stress rebounds or market breadth reverses below threshold.",
        }],
        "evidence_ids": [1, 2],
    }


def test_no_trade_without_credit_or_breadth():
    body = request_dict()
    body["indicators"] = {}
    result = synthesize(TradeSynthesisRequest.model_validate(body), now=NOW)
    assert result["status"] == "NO_TRADE"
    assert not result["tickets"]
    assert any("credit" in x.lower() for x in result["blocked_by"])


def test_no_trade_if_quote_is_stale():
    body = request_dict()
    body["quotes"][0]["as_of"] = stamp(900)
    result = synthesize(TradeSynthesisRequest.model_validate(body), now=NOW)
    assert result["status"] == "NO_TRADE"
    assert any("stale" in x for x in result["blocked_by"])


def test_no_trade_on_wide_spread():
    body = request_dict()
    body["quotes"][0]["bid"] = "98.0"
    result = synthesize(TradeSynthesisRequest.model_validate(body), now=NOW)
    assert result["status"] == "NO_TRADE"
    assert any("wide" in x for x in result["blocked_by"])


def test_risk_off_trim_is_bounded_and_tax_flagged():
    body = request_dict(risk_on=False)
    result = synthesize(TradeSynthesisRequest.model_validate(body), now=NOW)
    assert result["status"] == "PROPOSAL_ONLY"
    assert result["regime"] == "risk_off"
    assert len(result["tickets"]) == 1
    ticket = result["tickets"][0]
    assert ticket["action"] == "TRIM" and ticket["symbol"] == "NVDA"
    assert ticket["shares"] == 50
    assert ticket["limit_price"] == "99.95"
    assert ticket["tax_review"] is True
    assert ticket["order_type"] == "SELL_LIMIT"
    assert result["execution"] == "manual_approval_only"
    assert "caller_supplied" in result["source_attestation"]


def test_risk_on_buys_one_candidate_not_a_fabricated_ticker():
    body = request_dict(risk_on=True)
    result = synthesize(TradeSynthesisRequest.model_validate(body), now=NOW)
    assert result["status"] == "PROPOSAL_ONLY"
    ticket = result["tickets"][0]
    assert ticket["symbol"] == "MSFT"
    assert ticket["action"] == "BUY"
    assert ticket["shares"] > 0
    assert ticket["shares"] * 100.1 <= 7000
    assert float(ticket["risk_at_proposed_stop"]) <= 500
    assert ticket["stop_price"] == "94.10"
    assert ticket["target_price"] == "112.10"
    assert ticket["expires_at"] == (NOW + timedelta(seconds=90)).isoformat()


def test_hard_exit_sells_full_position_and_is_not_duplicated_by_trim():
    body = request_dict(risk_on=False, exit_price="105")
    result = synthesize(TradeSynthesisRequest.model_validate(body), now=NOW)
    assert len(result["tickets"]) == 1
    ticket = result["tickets"][0]
    assert ticket["symbol"] == "NVDA" and ticket["action"] == "SELL"
    assert ticket["shares"] == 200 and ticket["tax_review"] is True


def test_explicit_hard_exit_still_proposed_if_credit_data_missing():
    body = request_dict(risk_on=False, exit_price="105")
    body["indicators"] = {}
    result = synthesize(TradeSynthesisRequest.model_validate(body), now=NOW)
    assert result["status"] == "PROPOSAL_ONLY"
    assert result["regime"] == "unavailable"
    assert len(result["tickets"]) == 1
    assert result["tickets"][0]["action"] == "SELL"
    assert any("credit" in x.lower() for x in result["blocked_by"])


def test_no_buys_on_election_news_alone():
    body = request_dict(risk_on=True)
    body["indicators"]["ai_credit_spread_change_5d_bp"] = None
    result = synthesize(TradeSynthesisRequest.model_validate(body), now=NOW)
    assert result["status"] == "NO_TRADE"
    assert all(ticket["action"] != "BUY" for ticket in result["tickets"])


def test_no_trade_from_future_signal_or_inconsistent_nav():
    body = request_dict(risk_on=True)
    body["indicators"]["vxn_5d_change_pct"]["observed_at"] = stamp(-300)
    result = synthesize(TradeSynthesisRequest.model_validate(body), now=NOW)
    assert result["status"] == "NO_TRADE"
    body = request_dict(risk_on=True)
    body["portfolio"]["net_liquidation_value"] = "55000"
    body["portfolio"]["available_cash"] = "50000"
    result = synthesize(TradeSynthesisRequest.model_validate(body), now=NOW)
    assert result["status"] == "NO_TRADE"


def test_no_short_or_leverage_in_contract():
    from pydantic import ValidationError
    body = request_dict()
    body["quotes"][1]["instrument"] = "leveraged_etf"
    with pytest.raises(ValidationError):
        TradeSynthesisRequest.model_validate(body)
    body = request_dict()
    body["portfolio"]["positions"][0]["shares"] = "-3"
    with pytest.raises(ValidationError):
        TradeSynthesisRequest.model_validate(body)


class Store:
    def __init__(self):
        self.queries = []
    async def request(self, method, path, **kwargs):
        self.queries.append((method, path))
        if path == "market_observations":
            return [{"id": 1, "published_at": "2026-11-03T21:00:00Z"},
                    {"id": 2, "published_at": "2026-11-03T21:00:00Z"}]
        return []


def test_trade_endpoint_requires_owner_cannot_leak_holdings(monkeypatch):
    monkeypatch.setenv("SIGNAL_DESK_API_KEY", "test-trade-owner-" * 4)
    requests.clear()
    db = Store()
    app.dependency_overrides[get_store] = lambda: db
    try:
        with TestClient(app) as client:
            assert client.get("/v1/market/trade-synthesis/status").status_code == 401
            assert client.post("/v1/market/trade-synthesis", json=request_dict()).status_code == 401
            assert db.queries == []
            headers = {"Authorization": "Bearer " + "test-trade-owner-" * 4}
            status = client.get("/v1/market/trade-synthesis/status", headers=headers).json()
            assert status["broker_order_execution"] == "disabled"
            assert "positions" not in status and "portfolio" not in status
            # Current fixture is historical. With real now, caller-supplied quotes are stale.
            result = client.post("/v1/market/trade-synthesis", headers=headers, json=request_dict())
            assert result.status_code == 200
            assert result.json()["status"] == "NO_TRADE"
            assert "portfolio" not in result.text.lower()
            assert db.queries == [("GET", "market_observations")]
    finally:
        app.dependency_overrides.clear()


def test_reader_cannot_submit_portfolio(monkeypatch):
    import hashlib
    token = "read-only-market-trade-" * 3
    monkeypatch.setenv("SIGNAL_DESK_API_KEY", "test-trade-owner-" * 4)
    monkeypatch.setenv("SIGNAL_DESK_READ_KEY_SHA256", hashlib.sha256(token.encode()).hexdigest())
    requests.clear()
    db = Store()
    app.dependency_overrides[get_store] = lambda: db
    try:
        with TestClient(app) as client:
            result = client.post(
                "/v1/market/trade-synthesis", json=request_dict(),
                headers={"Authorization": "Bearer " + token},
            )
            assert result.status_code == 403
            assert db.queries == []
    finally:
        app.dependency_overrides.clear()
