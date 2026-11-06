"""SEC fundamental calculations are grounded in one actual filing/period, never guessed."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from app.market_companyfacts import analyze_companyfacts, fetch_company_fundamentals

CAPTURE=datetime(2026,11,5,23,0,tzinfo=timezone.utc)
ACC="0000000100-26-000001"
PRIOR="0000000100-25-000001"
START="2025-01-01"
END="2025-12-31"
FORM="10-K"


def row(value, *, start=START,end=END,filed="2026-02-15",accn=ACC,form=FORM,instant=False,frame=None):
    v={"val":value,"end":end,"filed":filed,"accn":accn,
       "form":form,"fy":2025,"fp":"FY","frame":frame}
    if not instant:v["start"]=start
    return v


def concept(*records,unit="USD"):
    return {"label":"Official GAAP fact","units":{unit:list(records)}}


def raw_facts():
    prev=row(1000,start="2024-01-01",end="2024-12-31",filed="2025-02-15",accn=PRIOR)
    return {
        "cik":100,"entityName":"Example Issuer Inc",
        "facts":{"us-gaap":{
            "RevenueFromContractWithCustomerExcludingAssessedTax":
                concept(row(2000,frame="CY2025"),prev),
            "NetCashProvidedByUsedInOperatingActivities":concept(row(350)),
            "PaymentsToAcquirePropertyPlantAndEquipment":concept(row(80)),
            "NetIncomeLoss":concept(row(120)),
            "CashAndCashEquivalentsAtCarryingValue":concept(row(900,instant=True)),
            "LongTermDebtNoncurrent":concept(row(500,instant=True)),
            "LongTermDebtCurrent":concept(row(50,instant=True)),
        }},
    }


def test_sec_annual_metrics_same_filing_and_period_with_original_tags():
    r=analyze_companyfacts("NVDA",100,raw_facts(),captured_at=CAPTURE)
    assert r["status"]=="ANALYZED"
    assert r["annual_filing"]["accession"]==ACC
    assert r["annual_filing"]["end"]==END
    assert r["source_collected_at"]==CAPTURE.isoformat()
    assert r["metrics"]["revenue"]["value"]=="2000"
    assert r["metrics"]["revenue"]["frame"]=="CY2025"
    assert r["metrics"]["operating_cash_flow"]["value"]=="350"
    assert r["metrics"]["capital_expenditure"]["value"]=="80"
    assert r["metrics"]["free_cash_flow_proxy"]["value"]=="270"
    assert r["metrics"]["fcf_margin_proxy_pct"]["value"]=="13.50"
    assert r["metrics"]["revenue_yoy_pct"]["value"]=="100.00"
    assert r["metrics"]["cash_and_equivalents"]["value"]=="900"
    assert r["metrics"]["long_term_debt_current"]["value"]=="50"
    assert r["metrics"]["long_term_debt_noncurrent"]["value"]=="500"
    assert r["execution_enabled"] is False
    assert all(f["accession"]==ACC for k,f in r["metrics"].items() if f and "accession" in f and k!="revenue_yoy_pct")


def test_different_sec_accessions_must_not_combine_CFO_with_capex():
    payload=raw_facts()
    payload["facts"]["us-gaap"]["PaymentsToAcquirePropertyPlantAndEquipment"] = concept(
        row(80,accn="0000000100-26-000002",filed="2026-02-16")
    )
    result=analyze_companyfacts("NVDA",100,payload,captured_at=CAPTURE)
    assert result["metrics"]["free_cash_flow_proxy"] is None
    assert result["metrics"]["fcf_margin_proxy_pct"] is None
    assert "free_cash_flow_proxy" in result["missing"]
    assert "capex" not in str(result["metrics"]["free_cash_flow_proxy"])


def test_same_period_mismatch_wont_compute_made_up_fcf():
    payload=raw_facts()
    payload["facts"]["us-gaap"]["PaymentsToAcquirePropertyPlantAndEquipment"] = concept(
        row(80,start="2025-02-01")
    )
    result=analyze_companyfacts("NVDA",100,payload,captured_at=CAPTURE)
    assert result["metrics"]["free_cash_flow_proxy"] is None


def test_future_filed_restated_numbers_excluded():
    payload=raw_facts()
    new=row(9999,filed="2026-12-15",accn="0000000100-26-000999")
    payload["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"].append(new)
    result=analyze_companyfacts("NVDA",100,payload,captured_at=CAPTURE)
    assert result["metrics"]["revenue"]["value"]=="2000"
    assert result["annual_filing"]["filed"]=="2026-02-15"


def test_revenue_comparison_requires_identical_tag_and_comparable_year():
    payload=raw_facts()
    # Different GAAP tag cannot be used silently as comparable denominator.
    values=payload["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"]
    values.pop()
    payload["facts"]["us-gaap"]["Revenues"]=concept(
        row(1000,start="2024-01-01",end="2024-12-31",filed="2025-02-15",accn=PRIOR))
    result=analyze_companyfacts("NVDA",100,payload,captured_at=CAPTURE)
    assert result["metrics"]["revenue_yoy_pct"] is None
    assert "revenue_yoy_pct" in result["missing"]


def test_missing_capex_is_missing_not_zero():
    payload=raw_facts()
    del payload["facts"]["us-gaap"]["PaymentsToAcquirePropertyPlantAndEquipment"]
    result=analyze_companyfacts("NVDA",100,payload,captured_at=CAPTURE)
    assert result["metrics"]["capital_expenditure"] is None
    assert result["metrics"]["free_cash_flow_proxy"] is None
    assert result["missing"]["capital_expenditure"]


def test_invalid_and_mismatched_cik_fail_closed():
    payload=raw_facts()
    with pytest.raises(ValueError,match="CIK"):
        analyze_companyfacts("NVDA",101,payload,captured_at=CAPTURE)
    with pytest.raises(ValueError,match="issuer"):
        analyze_companyfacts("UNLISTED",100,payload,captured_at=CAPTURE)


def test_unknown_revenue_unit_not_assumed_dollars():
    payload=raw_facts()
    payload["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"] = concept(
        row(2000),unit="shares"
    )
    result=analyze_companyfacts("NVDA",100,payload,captured_at=CAPTURE)
    assert result["metrics"]["revenue"] is None
    assert result["metrics"]["fcf_margin_proxy_pct"] is None


def test_filing_outside_cutoff_and_missing_facts_produces_explicit_status():
    payload=raw_facts()
    for concept_data in payload["facts"]["us-gaap"].values():
        for bucket in concept_data["units"].values():
            for item in bucket:
                item["filed"]="2026-12-15"
    result=analyze_companyfacts("NVDA",100,payload,captured_at=CAPTURE)
    assert result["status"]=="NO_COMPARABLE_ANNUAL_FACTS"
    assert result["execution_enabled"] is False
    assert "metrics" not in result


def test_contact_identity_required_before_any_external_request(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT",raising=False)
    import app.market_companyfacts as m
    async def forbidden(*args,**kwargs):
        raise AssertionError("Must not fetch public SEC without real contact")
    monkeypatch.setattr(m,"sec_json",forbidden)
    result=asyncio.run(fetch_company_fundamentals("NVDA"))
    assert result["status"]=="NOT_CONFIGURED"
    assert result["execution_enabled"] is False


def test_ingest_path_requires_real_official_mapping_not_hardcoded_cik(monkeypatch):
    import app.market_companyfacts as m
    monkeypatch.setattr(m,"configured_user_agent",lambda:"Research Operations contact@example.org")
    calls=[]
    async def fake(url,agent,**kwargs):
        calls.append((url,kwargs))
        if url.endswith("company_tickers.json"):
            return {"0":{"ticker":"NVDA","cik_str":100,"title":"Example Issuer Inc"}}
        return raw_facts()
    monkeypatch.setattr(m,"sec_json",fake)
    result=asyncio.run(fetch_company_fundamentals("NVDA"))
    assert result["metrics"]["free_cash_flow_proxy"]["value"]=="270"
    assert calls[1][0]=="https://data.sec.gov/api/xbrl/companyfacts/CIK0000000100.json"
    assert calls[1][1]["max_bytes"] <= 14_000_000
    assert result["source_collected_at"] != CAPTURE.isoformat()
