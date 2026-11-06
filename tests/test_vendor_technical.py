"""Stock technical research requires vendor data; no public access or fabricated levels."""
import asyncio
from datetime import datetime, timezone
import hashlib

from fastapi.testclient import TestClient

from app.main import app, requests
from app.market_quotes import vendor_technical_research
from test_market_technicals import candles


def test_stock_tech_with_unconfigured_vendor_cannot_generate_price(monkeypatch):
    monkeypatch.delenv("ALPACA_MARKET_DATA_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_MARKET_DATA_SECRET", raising=False)
    result=asyncio.run(vendor_technical_research("NVDA"))
    assert result["status"]=="NOT_CONFIGURED"
    assert result["execution_enabled"] is False
    assert "analysis" not in result
    assert "bid" not in result


def test_entitled_vendor_stock_bars_produce_actual_math_but_never_order(monkeypatch):
    monkeypatch.setenv("ALPACA_MARKET_DATA_KEY_ID","test-key")
    monkeypatch.setenv("ALPACA_MARKET_DATA_SECRET","test-secret")
    monkeypatch.setenv("ALPACA_DATA_FEED","iex")
    import app.market_quotes as module
    async def get_bars(path,params,**kwargs):
        assert path=="/v2/stocks/MSFT/bars"
        assert params["adjustment"]=="split"
        assert params["feed"]=="iex"
        return {"bars":[{
            "t":b.session.isoformat()+"T14:00:00Z",
            "o":str(b.open),"h":str(b.high),"l":str(b.low),"c":str(b.close),
        } for b in candles()]}
    monkeypatch.setattr(module,"api_json",get_bars)
    result=asyncio.run(vendor_technical_research("MSFT"))
    assert result["status"]=="STALE"  # Fixtures are historical, not current-market prices.
    assert result["feed"]=="iex"
    assert result["consolidated"] is False
    assert result["analysis"]["instrument_kind"]=="stock"
    assert result["analysis"]["daily_sessions"]==145
    assert result["analysis"]["trade_ticket_approved"] is False
    assert "input_bars" not in result["analysis"]
    assert result["execution_enabled"] is False
    assert any("single exchange" in x.lower() for x in result["blockers"])


def test_vendor_auth_failure_does_not_disclose_private_market_keys(monkeypatch):
    owner="tech-owner-key-"*3
    read="tech-reader-key-"*3
    monkeypatch.setenv("SIGNAL_DESK_API_KEY",owner)
    monkeypatch.setenv("SIGNAL_DESK_READ_KEY_SHA256",hashlib.sha256(read.encode()).hexdigest())
    monkeypatch.setenv("ALPACA_MARKET_DATA_KEY_ID","test-secret-id")
    monkeypatch.setenv("ALPACA_MARKET_DATA_SECRET","test-secret-value")
    monkeypatch.setenv("ALPACA_DATA_FEED","disabled")
    requests.clear()
    with TestClient(app) as client:
        assert client.get("/v1/market/vendor-technical/NVDA").status_code==401
        assert client.get("/v1/market/vendor-technical/NVDA",
            headers={"Authorization":"Bearer "+read}).status_code==403
        result=client.get("/v1/market/vendor-technical/NVDA",
            headers={"Authorization":"Bearer "+owner})
        assert result.status_code==200
        assert result.json()["status"]=="NOT_CONFIGURED"
        assert "test-secret-id" not in result.text
        assert "test-secret-value" not in result.text
        assert client.get("/v1/market/vendor-technical/%24BAD",
            headers={"Authorization":"Bearer "+owner}).status_code==422
