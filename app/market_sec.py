"""SEC EDGAR public issuer filing discovery, metadata only.

No article/full-document meaning is inferred from an 8-K/10-Q/10-K's existence.
SEC_USER_AGENT must identify a real organization and monitored contact address
as required by the SEC. No invented credentials or anonymous-agent bypass.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
import json
import os
import re
from urllib.parse import urlsplit

import httpx

from app.market import MarketObservation

SEC_TICKERS = ("NVDA", "MSFT", "GOOGL", "AMZN", "META", "ORCL", "CRWV", "AMD")
SEC_TICKER_INDEX = "https://www.sec.gov/files/company_tickers.json"
SEC_FORMS = frozenset(("10-K", "10-Q", "8-K", "10-K/A", "10-Q/A", "8-K/A"))
SEC_LIMIT = 24
SEC_MAX_BYTES = 3_000_000


def configured_user_agent():
    raw = os.getenv("SEC_USER_AGENT", "").strip()
    if len(raw) > 160 or not re.fullmatch(r"[A-Za-z0-9 ._\-/]{3,80}\s+[^\s@]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", raw):
        return None
    return raw


async def sec_json(url: str, user_agent: str, *, max_bytes: int = SEC_MAX_BYTES) -> dict:
    if max_bytes < 1 or max_bytes > 14_000_000:
        raise ValueError("SEC response size cap invalid")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ("www.sec.gov", "data.sec.gov"):
        raise ValueError("SEC URL not allowlisted")
    async with asyncio.timeout(22):
        async with httpx.AsyncClient(follow_redirects=False, timeout=15, trust_env=False) as client:
            async with client.stream("GET", url, headers={"User-Agent": user_agent, "Accept": "application/json"}) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError("Oversized SEC response")
                payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("SEC response is not a JSON object")
    return payload


def company_index(payload: dict) -> dict:
    """Resolve symbols through the SEC issuer index instead of guessing CIKs."""
    result = {}
    for item in list(payload.values())[:10000]:
        if not isinstance(item, dict):
            continue
        ticker = item.get("ticker")
        cik = item.get("cik_str")
        if ticker in SEC_TICKERS and isinstance(cik, int) and 0 < cik < 10**10:
            result[ticker] = cik
    return result


def filings_for_ticker(ticker: str, cik: int, payload: dict, captured: datetime, max_days: int = 14):
    if ticker not in SEC_TICKERS or not 0 < cik < 10**10 or captured.utcoffset() is None:
        raise ValueError("Invalid ticker/CIK/collection time")
    recent = (payload.get("filings") or {}).get("recent") or {}
    names = ("accessionNumber", "form", "filingDate", "primaryDocument")
    arrays = [recent.get(n) for n in names]
    if not all(isinstance(a, list) for a in arrays):
        raise ValueError("Missing SEC recent-filings index")
    if len(set(map(len, arrays))) != 1:
        raise ValueError("SEC recent-filings fields are misaligned")
    result = []
    for accession, form, filing_day, document in zip(*arrays):
        if len(result) >= SEC_LIMIT:
            break
        if form not in SEC_FORMS or not isinstance(filing_day, str):
            continue
        try:
            day = datetime.strptime(filing_day, "%Y-%m-%d").date()
        except ValueError:
            continue
        age = (captured.date() - day).days
        if not 0 <= age <= max_days:
            continue
        if not isinstance(accession, str) or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
            continue
        if not isinstance(document, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", document):
            continue
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{document}"
        if urlsplit(url).hostname != "www.sec.gov":
            continue
        result.append(MarketObservation(
            source_url=url,
            source_title=f"SEC EDGAR: {ticker} Form {form} filed {filing_day}",
            publisher="U.S. Securities and Exchange Commission",
            published_at=captured,
            event_at=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
            family="earnings_demand" if form.startswith("10-") else "other",
            phase="structural", pressure="ambiguous", claim_status="primary_document",
            origin_group=f"sec-edgar:{cik}:{accession}",
            summary=(f"SEC EDGAR filings index lists a Form {form} for {ticker} dated {filing_day}. "
                     "This collector inspected only filing metadata, not the document's financial statements, "
                     "earnings, AI capacity, financing commitments or management guidance."),
            tickers=[ticker],
            caveats=("First observed by this server at the recorded publication time; filing day is event metadata "
                     "and does not imply the source was collected or examined earlier. "
                     "No bullish or bearish direction inferred from the mere existence of a filing."),
        ))
    return result


async def collect_sec_filings(store) -> dict:
    agent = configured_user_agent()
    if not agent:
        return {"provider": "sec_edgar", "status": "not_configured",
                "reason": "SEC_USER_AGENT with real organization and contact email required",
                "readings": 0, "new_records": 0}
    try:
        idx = company_index(await sec_json(SEC_TICKER_INDEX, agent))
        if len(idx) < len(SEC_TICKERS) - 1:
            raise ValueError("SEC CIK mapping incomplete")
        seen, created, issuer_results = 0, 0, []
        # Conservative sequential traffic, under SEC fair-access guidance.
        for ticker in SEC_TICKERS:
            if ticker not in idx:
                issuer_results.append({"ticker": ticker, "status": "not_listed"})
                continue
            cik = idx[ticker]
            try:
                data = await sec_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", agent)
                collected = datetime.now(timezone.utc)
                items = filings_for_ticker(ticker, cik, data, collected)
                added = 0
                for item in items:
                    digest = item.source_url.rsplit("/", 2)[-2]
                    result = await store.rpc("market_record_observation_v1", {
                        "payload": item.model_dump(mode="json"),
                        "p_key": f"sec-edgar-v1:{cik}:{digest}",
                        "p_actor": "collector:sec_edgar",
                    })
                    seen += 1
                    added += int(result.get("created", False))
                created += added
                issuer_results.append({"ticker": ticker, "status": "completed",
                                       "filings": len(items), "new_records": added})
            except Exception:
                issuer_results.append({"ticker": ticker, "status": "failed"})
            await asyncio.sleep(0.15)
        failures = [x for x in issuer_results if x["status"] != "completed"]
        return {"provider": "sec_edgar", "status": "partial" if failures else "completed",
                "readings": seen, "new_records": created,
                "issuer_results": issuer_results,
                "limitations": "Metadata discovery only; no analysis of filing text or economic exposure"}
    except (httpx.HTTPError, ValueError, asyncio.TimeoutError):
        return {"provider": "sec_edgar", "status": "failed",
                "reason": "SEC index unavailable or access denied; no access-control bypass",
                "readings": 0, "new_records": 0}
