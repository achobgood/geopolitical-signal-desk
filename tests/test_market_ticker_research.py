import asyncio
from datetime import datetime, timezone
import pytest
from datetime import date
from decimal import Decimal
from app.market_technicals import Bar

import app.market_ticker_research as module


class Store:
    async def rows(self, table, **kwargs):
        return []


def test_provider_failure_preserves_independent_fund_facts_and_never_invents_chart(monkeypatch):
    async def fail(ticker, **kwargs):
        raise ValueError("secret-canary-provider-body")
    async def facts(ticker):
        return {"status": "AVAILABLE", "facts": {"nav": {"value": "100", "source_as_of": "2026-11-05"}}, "not_a_live_quote": True}
    async def holdings(ticker):
        return {"status": "SOURCE_PERMISSION_REQUIRED", "holdings": [], "reason": "No permitted full source"}
    monkeypatch.setattr(module, "fetch_historical_equity", fail)
    monkeypatch.setattr(module, "fetch_etf_facts", facts)
    monkeypatch.setattr(module, "fetch_etf_holdings", holdings)
    result = asyncio.run(module.research_ticker(Store(), "VOO"))
    assert result["sections"]["etf_facts"]["facts"]["nav"]["value"] == "100"
    assert result["status"] == "PARTIAL"
    assert result["classification"]["label"] == "WATCH"
    assert "price_history" not in result["sections"]
    assert "technicals" not in result["sections"]
    assert "secret-canary" not in str(result)
    assert result["portfolio_data_used"] is False
    assert result["execution_enabled"] is False


def test_no_prices_no_fundamentals_no_numeric_entry_setup(monkeypatch):
    async def blocked(ticker, **kwargs):
        return {"status": "NOT_CONFIGURED", "reason": "Actual provider credential absent"}
    monkeypatch.setattr(module, "fetch_historical_equity", blocked)
    monkeypatch.setattr(module, "fetch_company_fundamentals", blocked)
    result = asyncio.run(module.research_ticker(Store(), "NVDA"))
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["classification"]["label"] == "INSUFFICIENT_DATA"
    assert "analysis" not in result["sections"].get("technicals", {})
    assert any("consensus" in value for value in result["missing_inputs"])


def test_cisco_is_supported_and_invalid_symbols_cannot_reach_provider():
    assert "CSCO" in module.SUPPORTED_TICKERS
    for ticker in ("../../secret", "nvda", "VIX", "https://evil.example"):
        with pytest.raises(ValueError):
            asyncio.run(module.research_ticker(Store(), ticker))


def test_delayed_partial_current_candle_is_never_presented_as_a_completed_close(monkeypatch):
    async def source(ticker):
        return {"status": "RESEARCH_ONLY", "bars": [Bar(date(2026,11,5),Decimal("100")),Bar(date(2026,11,6),Decimal("90"))],
                "first_checked_at": "2026-11-06T21:20:00Z", "source_url": "https://data.alpaca.markets/v2/stocks/NVDA/bars", "adjustment": "split"}
    async def blocked(ticker):
        return {"status": "NOT_CONFIGURED"}
    monkeypatch.setattr(module,"fetch_historical_equity",source)
    monkeypatch.setattr(module,"fetch_company_fundamentals",blocked)
    result=asyncio.run(module.research_ticker(Store(),"NVDA"))
    history=result["sections"]["price_history"]
    assert history["last_available_price"]["session"]=="2026-11-05"
    assert history["last_available_price"]["value"]=="100"
    assert history["status"]=="STALE"
    assert len(history["bars"])==1
    assert "technicals" not in result["sections"]


def test_news_respects_actual_collection_retractions_and_latest_review():
    class NewsStore:
        async def rows(self, table, **kwargs):
            if table == "market_observations":
                return [{"id": 1, "tickers": ["NVDA"], "published_at": "2026-11-04T20:00:00Z", "collected_at": "2026-11-05T20:00:00Z", "source_url": "https://example.org/original"},
                        {"id": 2, "tickers": ["NVDA"], "published_at": "2026-11-04T20:00:00Z", "collected_at": "2026-11-07T20:00:00Z", "source_url": "https://example.org/future"},
                        {"id": 3, "tickers": ["NVDA"], "published_at": "2026-11-04T20:00:00Z", "collected_at": "2026-11-05T20:00:00Z", "source_url": "https://example.org/retracted"}]
            return [{"id": 1, "observation_id": 1, "amendment_kind": "canonical_link", "corrected_source_url": "https://example.org/reviewed", "reviewed_at": "2026-11-05T22:00:00Z", "rationale": "Original source inspected"},
                    {"id": 2, "observation_id": 3, "amendment_kind": "retraction", "reviewed_at": "2026-11-05T23:00:00Z", "rationale": "Unsupported attribution"}]
    result = asyncio.run(module.retained_ticker_news(NewsStore(), "NVDA", datetime(2026, 11, 6, 15, tzinfo=timezone.utc)))
    assert [row["id"] for row in result["observations"]] == [1]
    assert result["observations"][0]["source_url"].endswith("reviewed")
    assert result["observations"][0]["original_source_url"].endswith("original")
