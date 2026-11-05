"""SEC metadata discovery never invents AI returns or backdates source collection."""
import asyncio
from datetime import datetime, timezone

from app.market_sec import (
    SEC_TICKERS, collect_sec_filings, company_index, configured_user_agent, filings_for_ticker
)


def index_fixture():
    return {str(i): {"cik_str": i + 100, "ticker": ticker, "title": "Example Inc"}
            for i, ticker in enumerate(SEC_TICKERS)}


def filing_fixture():
    return {"filings": {"recent": {
        "accessionNumber": ["0000000100-26-000001", "0000000100-26-000002", "bad", "0000000100-26-000003"],
        "form": ["10-Q", "8-K", "10-Q", "10-K"],
        "filingDate": ["2026-11-04", "2026-10-28", "2026-11-04", "2025-05-02"],
        "primaryDocument": ["q.html", "current8k.htm", "../bad", "old.html"],
    }}}


def test_strict_contact_required(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    assert configured_user_agent() is None
    monkeypatch.setenv("SEC_USER_AGENT", "Anonymous Bot")
    assert configured_user_agent() is None
    monkeypatch.setenv("SEC_USER_AGENT", "Signal Desk Ops helpdesk@example.com")
    assert configured_user_agent() is not None


def test_resolve_ciks_from_official_index_only():
    assert company_index(index_fixture())["NVDA"] == 100
    assert len(company_index(index_fixture())) == len(SEC_TICKERS)
    assert company_index({"0": {"cik_str": "not_an_int", "ticker": "NVDA"}}) == {}


def test_metadata_is_explicitly_not_content_or_ai_financing_conclusion():
    collected = datetime(2026,11,5,19,0,tzinfo=timezone.utc)
    rows = filings_for_ticker("NVDA", 100, filing_fixture(), collected)
    assert len(rows) == 2
    assert rows[0].source_url.endswith("/100/000000010026000001/q.html")
    assert rows[0].published_at == collected
    assert rows[0].event_at.isoformat() == "2026-11-04T00:00:00+00:00"
    assert all(r.pressure == "ambiguous" for r in rows)
    assert all("metadata" in r.summary and "not" in r.summary for r in rows)
    assert rows[0].claim_status == "primary_document"
    assert rows[0].tickers == ["NVDA"]


def test_invalid_documents_do_not_inject_urls():
    sample = filing_fixture()
    sample["filings"]["recent"]["primaryDocument"][0] = "https://evil.example/steal"
    rows = filings_for_ticker("NVDA", 100, sample, datetime(2026,11,5,19,0,tzinfo=timezone.utc))
    assert len(rows) == 1


def test_no_sec_auth_is_a_real_disabled_provider_not_success(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    result = asyncio.run(collect_sec_filings(None))
    assert result["status"] == "not_configured"
    assert result["readings"] == 0


class Store:
    def __init__(self): self.rows = []
    async def rpc(self, name, args):
        assert name == "market_record_observation_v1"
        self.rows.append(args)
        return {"created": True}


def test_sec_source_ingest_and_idempotency_keys_are_stable(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Signal Desk Ops admin@example.com")
    import app.market_sec as module
    async def fake(url, agent):
        if "company_tickers" in url:
            return index_fixture()
        return filing_fixture()
    async def zero_sleep(seconds): return None
    monkeypatch.setattr(module, "sec_json", fake)
    monkeypatch.setattr(module.asyncio, "sleep", zero_sleep)
    store = Store()
    result = asyncio.run(collect_sec_filings(store))
    assert result["status"] == "completed"
    assert result["readings"] == len(SEC_TICKERS) * 2
    assert result["new_records"] == len(SEC_TICKERS) * 2
    assert len({row["p_key"] for row in store.rows}) == 2*len(SEC_TICKERS)
    assert all("SEC EDGAR" in row["payload"]["source_title"] for row in store.rows)
