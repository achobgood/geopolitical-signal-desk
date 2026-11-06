import asyncio
from datetime import datetime, timezone
import json
import pytest

from app.market_etf_holdings import (
    ISHARES, parse_ishares_holdings, parse_invesco_holdings, parse_etf_facts,
    fetch_etf_holdings, issuer_bytes,
)

CAPTURE = datetime(2026, 11, 6, 15, tzinfo=timezone.utc)


def csv_source(weight="40", second="20"):
    return ('iShares Global Tech ETF\nFund Holdings as of,"Nov 04, 2026"\n\n'
            'Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Location,Exchange,Market Currency\n'
            f'NVDA,NVIDIA,Information Technology,Equity,100,{weight},United States,NASDAQ,USD\n'
            f'MSFT,MICROSOFT,Information Technology,Equity,50,{second},United States,NASDAQ,USD\n\n').encode()


def parse(raw):
    return parse_ishares_holdings(raw, ticker="IXN", source_url=ISHARES["IXN"]+"/latest-holdings.csv", captured_at=CAPTURE)


def test_exact_weights_and_unknown_residual_are_not_renormalized():
    result = parse(csv_source())
    assert [row["weight_pct"] for row in result["holdings"]] == ["40", "20"]
    assert result["reported_weight_sum_pct"] == "60"
    assert result["unknown_residual_pct"] == "40"
    assert result["weights_renormalized"] is False
    assert result["source_as_of"] == "2026-11-04"
    assert result["captured_at"] == CAPTURE.isoformat()
    assert len(result["document_sha256"]) == 64
    assert result["rights_scope"] == "OWNER_PRIVATE_RESEARCH_ONLY"


def test_invalid_weights_duplicate_security_future_dates_and_truncation_fail_closed():
    for raw in (csv_source("NaN"), csv_source("101"), csv_source().replace(b'MSFT,MICROSOFT', b'NVDA,NVIDIA'),
                csv_source().replace(b'Nov 04, 2026', b'Nov 09, 2026'),
                csv_source().replace(b'MSFT,MICROSOFT,Information Technology,Equity,50,20,United States,NASDAQ,USD', b'MSFT,MICROSOFT')):
        with pytest.raises(ValueError):
            parse(raw)


def test_invesco_full_count_and_missing_classifications_are_explicit():
    doc = {"cusip": "46138E339", "effectiveDate": "2026-11-05", "totalNumberOfHoldings": 1,
           "holdings": [{"ticker": "NVDA", "issuerName": "NVIDIA", "cusip": "67066G104", "percentageOfTotalNetAssets": "9.75", "currency": "USD"}]}
    result = parse_invesco_holdings(json.dumps(doc).encode(), ticker="SPMO", source_url="https://dng-api.invesco.com/", captured_at=CAPTURE)
    assert result["holdings"][0]["security_id"] == "cusip:67066G104"
    assert result["holdings"][0]["sector"] is result["holdings"][0]["country"] is None
    assert result["unknown_residual_pct"] == "90.25"
    doc["totalNumberOfHoldings"] = 2
    with pytest.raises(ValueError, match="count"):
        parse_invesco_holdings(json.dumps(doc).encode(), ticker="SPMO", source_url="https://dng-api.invesco.com/", captured_at=CAPTURE)


def test_unknown_dividend_weight_and_identifier_are_never_invented_zero():
    doc = {"cusip": "46138E339", "effectiveDate": "2026-11-05", "totalNumberOfHoldings": 2,
           "holdings": [{"ticker": "NVDA", "issuerName": "NVIDIA", "cusip": "67066G104", "percentageOfTotalNetAssets": "99.99"},
                        {"ticker": "USDPDV", "issuerName": "USD Pending Dividends", "cusip": None, "percentageOfTotalNetAssets": None}]}
    result = parse_invesco_holdings(json.dumps(doc).encode(), ticker="SPMO", source_url="https://dng-api.invesco.com/", captured_at=CAPTURE)
    assert result["holdings_count"] == 2
    assert result["holdings"][1]["weight_pct"] is None
    assert result["unknown_residual_pct"] is None
    assert result["unknown_weight_rows"] == 1
    assert result["coverage"] == "ISSUER_FULL_FILE_WITH_UNKNOWN_WEIGHTS"


def test_multiple_genuine_fx_legs_are_retained_with_distinct_source_identities():
    raw = csv_source().replace(b'NVDA,NVIDIA,Information Technology,Equity,100,40,United States,NASDAQ,USD', b'KRW,KRW/USD,Cash and/or Derivatives,FX,100,0,United States,-,USD').replace(b'MSFT,MICROSOFT,Information Technology,Equity,50,20,United States,NASDAQ,USD', b'KRW,KRW/USD,Cash and/or Derivatives,FX,50,0,United States,-,USD')
    result = parse(raw)
    assert result["holdings_count"] == 2
    assert result["holdings"][0]["security_id"] != result["holdings"][1]["security_id"]


def test_official_nav_and_market_close_retain_separate_dates_and_never_form_bars():
    raw = b'<html><p>NAV as of</p><p>Nov 05, 2026</p><p>$$121.17</p><p>Closing Price</p><p>$132.55</p><p>as of Nov 04, 2026</p></html>'
    result = parse_etf_facts(raw, ticker="IXN", source_url=ISHARES["IXN"], captured_at=CAPTURE)
    assert result["facts"]["nav"]["source_as_of"] == "2026-11-05"
    assert result["facts"]["issuer_reported_market_close"]["source_as_of"] == "2026-11-04"
    assert result["not_a_live_quote"] is True
    assert "bars" not in result


def test_vanguard_reproduction_restriction_is_a_real_block_not_top_ten_proxy():
    result = asyncio.run(fetch_etf_holdings("VOO"))
    assert result["status"] == "SOURCE_PERMISSION_REQUIRED"
    assert result["holdings"] == []
    assert result["unknown_residual_pct"] == "100"
    result = asyncio.run(fetch_etf_holdings("SPMO"))
    assert result["status"] == "SOURCE_PERMISSION_REQUIRED"
    assert "automated access" in result["reason"]


def test_no_arbitrary_hosts_or_credentials_can_be_forwarded():
    for url in ("https://evil.example/holdings.csv", "http://www.ishares.com/x", "https://key@www.ishares.com/x", "https://www.ishares.com:444/x"):
        with pytest.raises(ValueError, match="allowlisted"):
            asyncio.run(issuer_bytes(url))


def test_vanguard_automatic_facts_do_not_bypass_website_permission_gate(monkeypatch):
    import asyncio
    import app.market_etf_holdings as module
    async def forbidden(*args,**kwargs):raise AssertionError('Unpermitted request must not happen')
    monkeypatch.setattr(module,'issuer_bytes',forbidden)
    for symbol in ('VOO','MGV','VEU'):
        assert asyncio.run(module.fetch_etf_facts(symbol))['status']=='SOURCE_PERMISSION_REQUIRED'
