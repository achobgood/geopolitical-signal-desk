"""No personal portfolio in audit logs and no historical lookahead in link repairs."""
import asyncio
from datetime import datetime, timezone
import hashlib

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from app.main import app, requests
from app.market import MarketAmendment, snapshot
from app.trade_audit import prepare_decision_receipt, persist_trade_result
from app.store import get_store


def decision():
    return {
        "status": "PROPOSAL_ONLY", "regime": "risk_on",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "tickets": [{
            "symbol": "MSFT", "action": "BUY", "shares": 3,
            "limit_price": "123.45", "order_type": "BUY_LIMIT",
            "portfolio": {"account_id": "DO-NOT-SAVE"},
            "available_cash": "DO-NOT-SAVE",
        }],
        "evidence_ids": [3], "blocked_by": [],
        "source_attestation": "caller_supplied_not_independently_verified",
        "research": {"model": "test-model"},
    }


def test_trade_receipt_strips_private_account_data():
    bundle = prepare_decision_receipt(decision(),method="ai_researched")
    payload = bundle["payload"]
    assert payload["tickets"][0]["symbol"]=="MSFT"
    assert "portfolio" not in str(payload)
    assert "DO-NOT-SAVE" not in str(payload)
    assert "available_cash" not in str(payload)
    assert len(bundle["p_key"])==64
    assert payload["model_label"]=="test-model"


def test_no_trade_cannot_have_order_and_no_orders_claimed():
    bad=decision();bad["status"]="NO_TRADE"
    with pytest.raises(ValueError):
        prepare_decision_receipt(bad,method="rule_based")


def test_confirm_persistence_and_owner_only_history():
    class Store:
        async def rpc(self,name,data):
            if name=="market_decision_history_v1":
                return [{"id": 1, "status": "NO_TRADE", "tickets": []}]
            assert name=="market_record_decision_v1"
            assert "DO-NOT-SAVE" not in str(data)
            return {"stored_privately":True,"orders_placed":False,"decision_id":1}
    store=Store()
    recorded=asyncio.run(persist_trade_result(store,decision(),method="ai_researched"))
    assert recorded["audit"]["decision_id"]==1
    assert recorded["automatic_execution"] is False
    owner="test-audit-owner-"*3
    read="test-audit-reader-"*3
    import os
    # TestClient does not receive any market holdings in list response.
    from unittest.mock import patch
    with patch.dict(os.environ,{"SIGNAL_DESK_API_KEY":owner,
                                "SIGNAL_DESK_READ_KEY_SHA256":hashlib.sha256(read.encode()).hexdigest()}):
        requests.clear()
        app.dependency_overrides[get_store]=lambda:store
        try:
            with TestClient(app) as c:
                assert c.get("/v1/market/trade-synthesis/history").status_code==401
                assert c.get("/v1/market/trade-synthesis/history",
                             headers={"Authorization":"Bearer "+read}).status_code==403
                got=c.get("/v1/market/trade-synthesis/history",
                          headers={"Authorization":"Bearer "+owner})
                assert got.status_code==200
                assert got.json()[0]["status"]=="NO_TRADE"
        finally:
            app.dependency_overrides.clear()


def test_invalid_market_amendment_cannot_redirect_to_private_domain():
    with pytest.raises(ValidationError):
        MarketAmendment.model_validate({
            "observation_id":1, "amendment_kind":"canonical_link",
            "corrected_source_url":"https://127.0.0.1/secret",
            "rationale":"Private loopback must not appear as a public source."
        })
    with pytest.raises(ValidationError):
        MarketAmendment.model_validate({
            "observation_id":1, "amendment_kind":"canonical_link",
            "rationale":"A correction requires a documented canonical link."
        })


def test_retraction_applies_only_after_it_was_known():
    now=datetime(2026,11,5,23,30,tzinfo=timezone.utc)
    observed={
        "id":7,"source_url":"https://example.org/published",
        "source_title":"An uncertain market claim","publisher":"Example",
        "published_at":"2026-11-05T20:00:00Z",
        "collected_at":"2026-11-05T20:15:00Z",
        "family":"ai_credit","phase":"leading","pressure":"risk_up",
        "origin_group":"example-primary-7","summary":"Attribution uncertain","claim_status":"unverified",
    }
    corrected={
        "id":11,"observation_id":7,"amendment_kind":"retraction",
        "reviewed_at":"2026-11-05T23:00:00Z",
        "rationale":"Publisher formally withdrew this reported claim",
    }
    class Store:
        async def rows(self,table,**kwargs):
            if table=="market_observations":return [observed]
            if table=="market_source_amendments":return [corrected]
            return []
    early=asyncio.run(snapshot(Store(),as_of=datetime(
        2026,11,5,22,0,tzinfo=timezone.utc),mode="live"))
    assert early["radar"]["eligible_observations"]==1
    late=asyncio.run(snapshot(Store(),as_of=now,mode="live"))
    assert late["radar"]["eligible_observations"]==0
    assert late["observations"]==[]
