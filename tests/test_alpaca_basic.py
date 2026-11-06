"""Free Alpaca Basic SIP must be historical, correctly adjusted, bounded, and non-orderable."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.market_alpaca_history import (
    fetch_historical_equity, request_params, validate_equity_bars
)
from app.market_quotes import api_json, configuration, vendor_snapshot
from test_market_technicals import candles

NOW = datetime(2026, 11, 5, 21, 30, tzinfo=timezone.utc)


def fakebars(rows=None):
    source = rows or candles(150)
    return [
        {"t":b.session.isoformat()+"T14:00:00Z", "o":str(b.open),
         "h":str(b.high), "l":str(b.low),"c":str(b.close),"v":"1000000"}
        for b in source
    ]


def enable_basic(monkeypatch):
    monkeypatch.setenv("ALPACA_DATA_MODE", "basic_sip_delayed")
    monkeypatch.setenv("ALPACA_MARKET_DATA_KEY_ID", "fixture-key")
    monkeypatch.setenv("ALPACA_MARKET_DATA_SECRET", "fixture-secret")


def test_free_sip_is_explicitly_historical_and_has_timezone_aware_cutoff(monkeypatch):
    enable_basic(monkeypatch)
    cfg=configuration()
    assert cfg["configured"] is True
    assert cfg["mode"]=="basic_sip_delayed"
    assert cfg["feed"]=="sip"
    assert cfg["historical_consolidated"] is True
    assert cfg["realtime_consolidated"] is False
    p=request_params(mode="basic_sip_delayed",now=NOW,adjustment="split")
    assert p["feed"]=="sip"
    assert p["adjustment"]=="split"
    assert datetime.fromisoformat(p["end"].replace("Z","+00:00")) == NOW-timedelta(minutes=16)
    assert "page_token" not in p
    with pytest.raises(ValueError,match="split or all"):
        request_params(mode="basic_sip_delayed",now=NOW,adjustment="raw")


def test_basic_mode_never_falls_through_to_sip_latest_quotes(monkeypatch):
    enable_basic(monkeypatch)
    import app.market_quotes as m
    async def forbidden(*args,**kw):raise AssertionError("Latest live SIP must not be called")
    monkeypatch.setattr(m,"api_json",forbidden)
    r=asyncio.run(vendor_snapshot("NVDA"))
    assert r["status"]=="historical_only"
    assert r["execution_enabled"] is False
    assert "sip"==r["feed"]


def test_enforce_free_sip_entitlement_boundary_before_network(monkeypatch):
    enable_basic(monkeypatch)
    with pytest.raises(ValueError,match="15-minute entitlement"):
        asyncio.run(api_json("/v2/stocks/MSFT/bars",{
            "feed":"sip","timeframe":"1Day",
            "end":datetime.now(timezone.utc).isoformat()
        }))
    with pytest.raises(ValueError,match="historical bars only"):
        asyncio.run(api_json("/v2/stocks/MSFT/quotes/latest",{"feed":"sip"}))
    with pytest.raises(ValueError,match="conflicts"):
        asyncio.run(api_json("/v2/stocks/MSFT/bars",{
            "feed":"iex","end":(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
        }))


def test_actual_pagination_and_adjustment_are_metadata_not_fake_prices(monkeypatch):
    enable_basic(monkeypatch)
    import app.market_quotes as m
    source=fakebars()
    calls=[]
    async def mock_fetch(path,params,**kw):
        assert path=="/v2/stocks/MSFT/bars"
        calls.append(params)
        if params.get("page_token") is None:
            return {"bars":source[:80],"next_page_token":"cursor-one"}
        assert params["page_token"]=="cursor-one"
        return {"bars":source[80:],"next_page_token":None}
    monkeypatch.setattr(m,"api_json",mock_fetch)
    result=asyncio.run(fetch_historical_equity("MSFT",adjustment="all",now=NOW))
    assert result["status"]=="STALE"  # historical fixture, never represented as a current quote
    assert len(result["bars"])==150
    assert len(result["request_pages"])==2
    assert result["quality"]["daily_sessions"]==150
    assert result["quality"]["has_volume"] is True
    assert result["quality"]["average_dollar_volume_20"]!= "0"
    assert all(c["feed"]=="sip" and c["adjustment"]=="all" for c in calls)
    assert result["adjustment"]=="all"
    assert "dividend" in result["adjustment_description"].lower()
    assert result["realtime_consolidated"] is False
    assert result["execution_enabled"] is False


def test_conflicting_duplicate_and_invalid_volume_rejected():
    raw=fakebars()
    revised=dict(raw[-1]);revised["c"]=str(Decimal(revised["c"])+Decimal("0.01"))
    with pytest.raises(ValueError,match="Conflicting duplicate"):
        validate_equity_bars(raw+[revised],expected_session=NOW.date())
    negative=dict(raw[-1]);negative["v"]="-100"
    with pytest.raises(ValueError,match="Invalid traded volume"):
        validate_equity_bars(raw[:-1]+[negative],expected_session=NOW.date())


def test_repeated_cursor_aborts_without_unbounded_requests(monkeypatch):
    enable_basic(monkeypatch)
    import app.market_quotes as m
    calls=[]
    async def same_cursor(path,params,**kw):
        calls.append(params)
        return {"bars":fakebars()[:70], "next_page_token":"repeat"}
    monkeypatch.setattr(m,"api_json",same_cursor)
    with pytest.raises(ValueError,match="pagination cycle"):
        asyncio.run(fetch_historical_equity("MSFT",now=NOW))
    assert len(calls)==2


def test_incomplete_current_session_cannot_enter_historical_indicators():
    raw=fakebars()
    unfinished=dict(raw[-1]);unfinished["t"]="2026-11-06T14:00:00Z"
    items,quality=validate_equity_bars(raw+[unfinished],expected_session=NOW.date())
    assert len(items)==150
    assert quality["excluded_unfinished_sessions"]==1
    assert all(x.session<=NOW.date() for x in items)
