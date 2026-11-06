"""SEC companyfacts financial analysis with exact filing/period provenance.

Only official entity-level us-gaap USD data. No LLM, forecasts, ratio guesswork,
mixed filing accessions, inferred zero, or historical "prior detection" claims.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.market_sec import (
    SEC_TICKERS, SEC_TICKER_INDEX, company_index, configured_user_agent, sec_json
)

# Ordered alternatives are distinct tagged definitions, not necessarily
# comparable across issuers. Every displayed value retains its original tag.
DURATION_CONCEPTS = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues", "SalesRevenueNet",
    ),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "capital_expenditure": ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "net_income": ("NetIncomeLoss",),
}
INSTANT_CONCEPTS = {
    "cash_and_equivalents": ("CashAndCashEquivalentsAtCarryingValue",),
    "long_term_debt_noncurrent": ("LongTermDebtNoncurrent",),
    "long_term_debt_current": ("LongTermDebtCurrent",),
}
FORMS = frozenset(("10-K", "10-K/A"))
MAX_CONCEPT_ENTRIES = 2500
MAX_FACTS_BYTES = 14_000_000


def _day(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _amount(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() and abs(out) <= Decimal("1e15") else None


def _facts_for_tag(us_gaap: dict, tag: str, *, instant: bool,
                   cutoff: date) -> list[dict]:
    concept = us_gaap.get(tag) or {}
    usd = (concept.get("units") or {}).get("USD") or []
    if not isinstance(usd, list) or len(usd) > MAX_CONCEPT_ENTRIES:
        return []
    rows = []
    for item in usd:
        if not isinstance(item, dict) or item.get("form") not in FORMS:
            continue
        filed, end = _day(item.get("filed")), _day(item.get("end"))
        accn = item.get("accn")
        value = _amount(item.get("val"))
        if not filed or not end or value is None or not accn or (
            not isinstance(accn, str) or len(accn) != 20
            or not accn.replace("-", "").isdigit()
        ):
            continue
        if filed > cutoff or end > cutoff or filed < end:
            continue
        start = _day(item.get("start"))
        if instant:
            if start is not None:
                continue
        else:
            if start is None or not 330 <= (end - start).days + 1 <= 400:
                continue
        rows.append({
            "tag": tag, "start": start.isoformat() if start else None,
            "end": end.isoformat(), "filed": filed.isoformat(),
            "form": item["form"], "accn": accn,
            "fy": item.get("fy"), "fp": item.get("fp"),
            "value": value, "unit": "USD",
        })
    return rows


def _select_same_filing(records: dict[str, list[dict]],
                        tag_names: tuple[str, ...], group_key: tuple,
                        *, instant=False) -> tuple[dict | None, str | None]:
    choices = []
    for rank, tag in enumerate(tag_names):
        matched = [
            x for x in records.get(tag, [])
            if ((x["accn"], x["end"]) if instant else
                (x["accn"], x["start"], x["end"])) == group_key
        ]
        if not matched:
            continue
        unique = {x["value"] for x in matched}
        if len(unique) != 1:
            return None, "Conflicting same-filing numeric values for " + tag
        choices.append((rank, sorted(matched, key=lambda x:x["filed"], reverse=True)[0]))
    if not choices:
        return None, "Not reported in same SEC filing and fiscal period"
    choices.sort(key=lambda x:x[0])
    return choices[0][1], None


def _public_fact(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "value": str(row["value"]), "unit": "USD",
        "taxonomy": "us-gaap", "tag": row["tag"],
        "start": row["start"], "end": row["end"],
        "filed": row["filed"], "accession": row["accn"],
        "form": row["form"], "fiscal_year_field": row["fy"],
        "fiscal_period_field": row["fp"],
    }


def analyze_companyfacts(ticker: str, cik: int, document: dict,
                         *, captured_at: datetime) -> dict:
    if ticker not in SEC_TICKERS or not 0 < cik < 10**10:
        raise ValueError("Unapproved issuer")
    if captured_at.utcoffset() is None:
        raise ValueError("Missing actual source-capture timezone")
    if not isinstance(document, dict) or document.get("cik") != cik:
        raise ValueError("Companyfacts CIK does not match official SEC issuer index")
    raw = (document.get("facts") or {}).get("us-gaap") or {}
    if not isinstance(raw, dict):
        raise ValueError("SEC us-gaap facts are missing or invalid")
    tags = {tag for group in (*DURATION_CONCEPTS.values(), *INSTANT_CONCEPTS.values())
            for tag in group}
    annual = {tag:_facts_for_tag(raw,tag,instant=False,cutoff=captured_at.date())
              for names in DURATION_CONCEPTS.values() for tag in names}
    instants = {tag:_facts_for_tag(raw,tag,instant=True,cutoff=captured_at.date())
                for names in INSTANT_CONCEPTS.values() for tag in names}
    # The annual grouping is by the EXACT source accession and fiscal start/end.
    # Selecting separate "latest" facts by concept risks mismatched cash flow math.
    groups = {}
    for rows in annual.values():
        for item in rows:
            key = (item["accn"], item["start"], item["end"])
            groups[key] = max(groups.get(key, ""), item["filed"])
    if not groups:
        return {
            "status":"NO_COMPARABLE_ANNUAL_FACTS",
            "ticker":ticker, "cik":cik,
            "source_collected_at":captured_at.isoformat(),
            "source_url":f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
            "explanation":"No qualifying annual us-gaap USD facts from a 10-K; no numbers inferred.",
            "execution_enabled":False,
        }
    ordered = sorted(groups, key=lambda k:(k[2],groups[k],k[0]), reverse=True)
    selected = ordered[0]
    accn, start, end = selected
    facts = {}
    missing = {}
    for label, tag_names in DURATION_CONCEPTS.items():
        row, reason = _select_same_filing(annual,tag_names,selected)
        facts[label] = _public_fact(row)
        if reason: missing[label] = reason
    instant_key = (accn,end)
    for label, tag_names in INSTANT_CONCEPTS.items():
        row, reason = _select_same_filing(instants,tag_names,instant_key,instant=True)
        facts[label] = _public_fact(row)
        if reason: missing[label] = reason
    if facts["operating_cash_flow"] and facts["capital_expenditure"]:
        ocf = Decimal(facts["operating_cash_flow"]["value"])
        capex = Decimal(facts["capital_expenditure"]["value"])
        # SEC PaymentsToAcquirePropertyPlantAndEquipment represents cash spent,
        # recorded as a non-negative payment. Don't "subtract a negative" silently.
        if capex >= 0:
            facts["free_cash_flow_proxy"] = {
                "value": str(ocf - capex), "unit":"USD",
                "calculation":"operating_cash_flow - payments_to_acquire_PPE",
                "source_accession":accn,
                "start":start, "end":end, "is_gaap_metric":False,
            }
        else:
            facts["free_cash_flow_proxy"] = None
            missing["free_cash_flow_proxy"] = "SEC capex cash payment has unexpected negative sign"
    else:
        facts["free_cash_flow_proxy"] = None
        missing["free_cash_flow_proxy"] = "CFO and capex must both exist in identical annual filing/period"
    # Ratio needs the same filing, same fiscal period and nonzero revenue.
    if facts["revenue"] and facts["free_cash_flow_proxy"] and Decimal(facts["revenue"]["value"]) > 0:
        fcf = Decimal(facts["free_cash_flow_proxy"]["value"])
        rev = Decimal(facts["revenue"]["value"])
        facts["fcf_margin_proxy_pct"] = {
            "value":str((fcf / rev * 100).quantize(Decimal(".01"))),
            "unit":"percent", "source_accession":accn,
            "is_gaap_metric":False,
        }
    else:
        facts["fcf_margin_proxy_pct"] = None
        missing["fcf_margin_proxy_pct"] = "Same-period revenue and free-cash-flow proxy required"
    # Backward comparison is restricted to annual periods from SEC filings, and
    # the exact same revenue concept tag to avoid changing revenue definitions.
    prior = None
    current_rev = facts["revenue"]
    if current_rev:
        for key in ordered[1:]:
            prev, err = _select_same_filing(annual,(current_rev["tag"],),key)
            age = (date.fromisoformat(end) - date.fromisoformat(key[2])).days
            if prev and 330 <= age <= 400:
                prior = prev
                break
    if prior and current_rev and prior["value"] != 0:
        facts["revenue_yoy_pct"] = {
            "value":str(((Decimal(current_rev["value"]) / prior["value"] - 1)*100).quantize(Decimal(".01"))),
            "unit":"percent","prior_end":prior["end"],"prior_filed":prior["filed"],
            "prior_accession":prior["accn"], "prior_tag":prior["tag"],
            "is_gaap_metric":False,
        }
    else:
        facts["revenue_yoy_pct"] = None
        missing["revenue_yoy_pct"] = "A comparable preceding annual revenue fact using the same tag is unavailable"
    return {
        "status":"ANALYZED_WITH_GAPS" if missing else "ANALYZED",
        "ticker":ticker, "cik":cik,
        "issuer_name":str(document.get("entityName") or "")[:150],
        "source_url":f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
        "source_collected_at":captured_at.isoformat(),
        "annual_filing": {
            "accession":accn, "form":"10-K/10-K/A",
            "start":start,"end":end,"filed":groups[selected],
            "filing_directory":f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-','')}/",
        },
        "metrics":facts,"missing":missing,
        "claims":[
            "SEC companyfacts are official normalized us-gaap facts, not a complete set of company-specific extensions.",
            "Latest available annual 10-K source and filing date are distinct from Signal Desk's actual collection time.",
            "FCF and margin are non-GAAP analyst proxies; not an issuer's asserted free-cash-flow definition.",
            "No issuer-specific CDS, earnings estimate, funding stress probability or trading signal inferred.",
        ],
        "execution_enabled":False,
    }


async def fetch_company_fundamentals(ticker: str) -> dict:
    if ticker not in SEC_TICKERS:
        raise ValueError("Ticker outside currently supported issuer universe")
    agent = configured_user_agent()
    if not agent:
        return {
            "status":"NOT_CONFIGURED","ticker":ticker,
            "reason":"Set genuine monitored SEC_USER_AGENT in protected Render environment",
            "execution_enabled":False,
        }
    index = company_index(await sec_json(SEC_TICKER_INDEX,agent))
    cik = index.get(ticker)
    if not cik:
        return {"ticker":ticker,"status":"NOT_IN_OFFICIAL_ISSUER_INDEX",
                "execution_enabled":False}
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
    # SEC has no API key; genuine contact UA and bounded request are mandatory.
    company = await sec_json(url,agent,max_bytes=MAX_FACTS_BYTES)
    return analyze_companyfacts(ticker,cik,company,captured_at=datetime.now(timezone.utc))
