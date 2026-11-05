"""Market quote adapter is read-only, never implies consolidated rights or ticket readiness."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from app.main import app, requests
from app.market_quotes import calculate_bars, configuration, vendor_snapshot


def bars(count=70):
    from datetime import date
    from datetime import timedelta as TD
    start = date(2026,7,1)
    data = []
    for i in range(count):
        day = start + TD(days=i)
        if day.weekday() >= 5: continue
        v = 100 + i/10
        data.append({"t": day.isoformat()+"T14:00:00Z", "h": v+3,
                     "l": v-2, "c": v, "v": 1000000})
    return data


def test_without_key_does_not_call_any_external_service(monkeypatch):
    monkeypatch.delenv("ALPACA_MARKET_DATA_KEY_ID",raising=False)
    monkeypatch.delenv("ALPACA_MARKET_DATA_SECRET",raising=False)
    monkeypatch.setenv("ALPACA_DATA_FEED","sip")
    assert configuration()["configured"] is False
    result = asyncio.run(vendor_snapshot("NVDA"))
    assert result["status"] == "not_configured"
    assert result["execution_enabled"] is False


def test_iex_is_never_labeled_consolidated(monkeypatch):
    monkeypatch.setenv("ALPACA_MARKET_DATA_KEY_ID", "fixture")
    monkeypatch.setenv("ALPACA_MARKET_DATA_SECRET", "fixture")
    monkeypatch.setenv("ALPACA_DATA_FEED", "iex")
    status = configuration()
    assert status["configured"] is True
    assert status["consolidated"] is False
    assert "partial" in status["coverage_note"].lower() or "one trading venue" in status["coverage_note"]


def test_daily_candle_must_be_complete_and_50_sessions_available():
    data = bars(120)
    calculation = calculate_bars(data, datetime(2026,11,5,19,tzinfo=timezone.utc))
    assert calculation["daily_bars"] == 51
    assert calculation["atr14"] > 0
    assert calculation["sma20"] > calculation["sma50"]
    assert calculation["average_daily_dollar_volume"] > Decimal("1000000")


def test_future_daily_candles_not_used():
    data = bars(120)
    data.append({"t":"2026-11-05T19:00:00Z","h":10000,"l":9000,"c":9500,"v":10000})
    result=calculate_bars(data,datetime(2026,11,5,20,tzinfo=timezone.utc))
    assert result["sma20"] < 1000


def test_no_crossed_quotes_and_no_fabricated_vendor_data(monkeypatch):
    monkeypatch.setenv("ALPACA_MARKET_DATA_KEY_ID", "fixture")
    monkeypatch.setenv("ALPACA_MARKET_DATA_SECRET", "fixture")
    monkeypatch.setenv("ALPACA_DATA_FEED", "sip")
    import app.market_quotes as m
    async def respond(path, params, **kwargs):
        now=datetime.now(timezone.utc).isoformat()
        if path.endswith("quotes/latest"):
            return {"quote":{"bp":101,"ap":100,"t":now}}
        if path.endswith("trades/latest"):
            return {"trade":{"p":100.5,"t":now}}
        return {"bars":bars(120)}
    monkeypatch.setattr(m,"api_json",respond)
    res=asyncio.run(vendor_snapshot("NVDA"))
    assert res["status"]=="unavailable"
    assert res["execution_enabled"] is False
    assert "bid" not in res


def test_quotes_are_owner_only(monkeypatch):
    import hashlib
    owner="verified-owner-key-"*3
    reader="research-reader-key-"*3
    monkeypatch.setenv("SIGNAL_DESK_API_KEY",owner)
    monkeypatch.setenv("SIGNAL_DESK_READ_KEY_SHA256",hashlib.sha256(reader.encode()).hexdigest())
    monkeypatch.delenv("ALPACA_MARKET_DATA_KEY_ID",raising=False)
    monkeypatch.delenv("ALPACA_MARKET_DATA_SECRET",raising=False)
    requests.clear()
    with TestClient(app) as c:
        assert c.get("/v1/market/vendor-quote/NVDA").status_code==401
        assert c.get("/v1/market/vendor-quote/NVDA", headers={"Authorization":"Bearer "+reader}).status_code==403
        out=c.get("/v1/market/vendor-quote/NVDA",headers={"Authorization":"Bearer "+owner})
        assert out.status_code==200
        assert out.json()["status"]=="not_configured"
        assert c.get("/v1/market/vendor-quote/%24NVDA",headers={"Authorization":"Bearer "+owner}).status_code==422
        status=c.get("/v1/market/data-sources/status",headers={"Authorization":"Bearer "+owner})
        assert status.status_code==200
        assert "secret" not in status.text.lower()
