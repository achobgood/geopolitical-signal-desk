"""Official issuer data for OWNER-ONLY personal research, never public redistribution.

Exact reported weights are retained. Missing sectors, countries and residuals
remain unknown. A top-ten list is never promoted to a complete portfolio.
"""
from __future__ import annotations

import asyncio
import csv
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import io
import json
import re
from urllib.parse import urlsplit

import httpx

ISHARES = {
    "IXN": "https://www.ishares.com/us/products/239750/ishares-global-tech-etf",
    "IOO": "https://www.ishares.com/us/products/239737/ishares-global-100-etf",
}
VANGUARD = {"VOO": "0968", "MGV": "3139", "VEU": "0991"}
INVESCO = {"SPMO": "46138E339"}
ETF_TICKERS = frozenset(("SPY", "QQQ", "VOO", "IXN", "SPMO", "MGV", "VEU", "FDVV", "IOO", "SPEU", "PPA"))
HOSTS = frozenset(("www.ishares.com",))
SCOPE = "OWNER_PRIVATE_RESEARCH_ONLY"
MAX_BYTES = 3_000_000
MAX_HOLDINGS = 10000


def now():
    return datetime.now(timezone.utc)


def number(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Boolean is not an issuer number")
    try:
        result = Decimal(str(value).strip().replace(",", "").replace("%", "").replace("$", ""))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Invalid issuer number") from exc
    if not result.is_finite() or abs(result) > Decimal("1e15"):
        raise ValueError("Nonfinite or out-of-range issuer number")
    return result


def source_day(value: str, captured_at: datetime) -> str:
    if captured_at.utcoffset() is None:
        raise ValueError("Actual timezone-aware capture time required")
    result = None
    for form in ("%Y-%m-%d", "%b %d, %Y", "%m/%d/%Y"):
        try:
            result = datetime.strptime(value.strip(), form).date()
            break
        except (ValueError, AttributeError):
            pass
    if result is None or result > captured_at.date():
        raise ValueError("Missing, invalid or future issuer as-of date")
    return result.isoformat()


async def issuer_bytes(url: str) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.username or parsed.password or parsed.port:
        raise ValueError("Issuer URL is not allowlisted")
    async with asyncio.timeout(30):
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", url, headers={"User-Agent": "SignalDesk private investment research", "Accept": "text/csv,application/json,text/html"}) as response:
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_BYTES:
                        raise ValueError("Issuer response exceeded bounded research size")
    if not raw:
        raise ValueError("Empty issuer response")
    return bytes(raw)


def _receipt(ticker, url, raw, captured_at):
    terms = ("https://www.blackrock.com/corporate/compliance/terms-and-conditions" if ticker in ISHARES else
             "https://www.invesco.com/us/en/resources/terms-of-use.html" if ticker in INVESCO else
             "https://investor.vanguard.com/terms-conditions")
    return {"ticker": ticker, "source_url": url, "captured_at": captured_at.isoformat(),
            "document_sha256": sha256(raw).hexdigest(), "rights_scope": SCOPE,
            "terms_url": terms, "publisher_attribution": "BlackRock / iShares" if ticker in ISHARES else "Invesco" if ticker in INVESCO else "Vanguard",
            "rights_note": "Issuer-published data retained for this owner's private personal research; no public or third-party redistribution license asserted.",
            "execution_enabled": False}


def _holdings_result(ticker, url, raw, captured_at, as_of, holdings, *, expected_count=None):
    if not holdings or len(holdings) > MAX_HOLDINGS:
        raise ValueError("No valid bounded issuer holdings")
    if expected_count is not None and len(holdings) != expected_count:
        raise ValueError("Issuer full holdings count does not match downloaded rows")
    identities = [row["security_id"] for row in holdings]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate security identity in source; no double-counted weights")
    total = sum((number(row["weight_pct"]) for row in holdings if row["weight_pct"] is not None), Decimal(0))
    unknown = sum(row["weight_pct"] is None for row in holdings)
    if total < 0 or total > 150:
        raise ValueError("Unexpected reported net holdings weight")
    return {**_receipt(ticker, url, raw, captured_at), "status": "AVAILABLE", "source_as_of": as_of,
            "holdings": holdings, "holdings_count": len(holdings),
            "reported_weight_sum_pct": str(total), "unknown_residual_pct": None if unknown else str(max(Decimal(0), 100-total)),
            "unknown_weight_rows": unknown,
            "gross_positive_weight_pct": str(sum((max(Decimal(0), number(row["weight_pct"])) for row in holdings if row["weight_pct"] is not None), Decimal(0))),
            "coverage": "ISSUER_FULL_FILE_WITH_UNKNOWN_WEIGHTS" if unknown else "ISSUER_FULL_FILE_NOT_RENORMALIZED", "weights_renormalized": False,
            "limitations": ["Holdings are dated issuer disclosures and may differ from today's portfolio.",
                            "Rounding, cash, derivatives and omitted classifications can leave residual or gross exposure above 100%.",
                            "Ticker-only identities are venue/currency qualified; unresolved cross-provider identity matches must remain unknown."]}


def parse_ishares_holdings(raw: bytes, *, ticker: str, source_url: str, captured_at: datetime) -> dict:
    if ticker not in ISHARES or not raw or len(raw) > MAX_BYTES:
        raise ValueError("Unsupported or oversized iShares source")
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    expected_name = {"IXN": "iShares Global Tech ETF", "IOO": "iShares Global 100 ETF"}[ticker]
    if not rows or rows[0] != [expected_name]:
        raise ValueError("Official holdings fund identity mismatch")
    heading = next((i for i, row in enumerate(rows) if row and row[0] == "Ticker" and "Weight (%)" in row), None)
    dated = next((row for row in rows[:20] if row and row[0] == "Fund Holdings as of"), None)
    if heading is None or not dated or len(dated) != 2:
        raise ValueError("Official holdings header and actual date required")
    as_of = source_day(dated[1], captured_at)
    columns = rows[heading]
    if not {"Ticker", "Name", "Sector", "Weight (%)", "Location", "Exchange", "Market Currency", "Asset Class"} <= set(columns):
        raise ValueError("Required official iShares holdings columns missing")
    holdings = []
    for values in rows[heading+1:]:
        if not values or all(not value.strip() for value in values):
            break
        if len(values) != len(columns):
            raise ValueError("Malformed holdings row; source cannot be silently truncated")
        row = dict(zip(columns, values))
        weight = number(row["Weight (%)"])
        if not -100 <= weight <= 100 or not row["Name"].strip():
            raise ValueError("Invalid holding weight or name")
        symbol, venue, currency = (row[key].strip() for key in ("Ticker", "Exchange", "Market Currency"))
        identity = "ticker:" + "|".join((symbol.upper(), venue.upper(), currency.upper()))
        basis = "ISSUER_TICKER_VENUE_CURRENCY"
        if row["Asset Class"] in ("FX", "Futures") or symbol == "-":
            identity = "source_leg:"+ticker+":"+str(len(holdings))+":"+sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:16]
            basis = "SOURCE_DERIVATIVE_LEG_NOT_CROSS_FUND_SECURITY_ID"
        holdings.append({"security_id": identity, "identity_basis": basis,
                         "ticker": symbol if symbol != "-" else None, "name": row["Name"],
                         "weight_pct": str(weight), "sector": row["Sector"] if row["Sector"] != "-" else None,
                         "country": row["Location"] if row["Location"] != "-" else None,
                         "exchange": venue if venue != "-" else None, "currency": currency,
                         "asset_class": row["Asset Class"]})
    return _holdings_result(ticker, source_url, raw, captured_at, as_of, holdings)


def parse_invesco_holdings(raw: bytes, *, ticker: str, source_url: str, captured_at: datetime) -> dict:
    if ticker not in INVESCO or len(raw) > MAX_BYTES:
        raise ValueError("Unsupported Invesco source")
    document = json.loads(raw)
    if document.get("cusip") != INVESCO[ticker] or not isinstance(document.get("holdings"), list):
        raise ValueError("Issuer holdings identity/schema mismatch")
    as_of = source_day(document.get("effectiveDate"), captured_at)
    holdings = []
    for row in document["holdings"]:
        weight = number(row["percentageOfTotalNetAssets"]) if row.get("percentageOfTotalNetAssets") is not None else None
        if (weight is not None and not -100 <= weight <= 100) or not isinstance(row.get("issuerName"), str):
            raise ValueError("Invalid Invesco holding")
        cusip, symbol = row.get("cusip"), row.get("ticker")
        identity = "cusip:"+str(cusip) if cusip else "source_row:"+ticker+":"+str(len(holdings))
        holdings.append({"security_id": identity, "identity_basis": "ISSUER_CUSIP" if cusip else "SOURCE_ROW_ONLY_NO_CROSS_FUND_ID",
                         "ticker": symbol or None, "name": unescape(row["issuerName"]), "weight_pct": str(weight) if weight is not None else None,
                         "sector": row.get("sector") or None, "country": row.get("country") or None,
                         "currency": row.get("currency"), "asset_class": row.get("securityTypeName")})
    count = document.get("totalNumberOfHoldings")
    if type(count) is not int:
        raise ValueError("Explicit full holdings count required")
    return _holdings_result(ticker, source_url, raw, captured_at, as_of, holdings, expected_count=count)


class IssuerHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.profile = None
        self.text = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in ("script", "style"):
            self.hidden += 1
        if attributes.get("id") == "vgn-funds-profile-data-ele":
            self.profile = json.loads(attributes["data-vgn-funds-profile"])

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden-1)

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data.strip())


def parse_etf_facts(raw: bytes, *, ticker: str, source_url: str, captured_at: datetime) -> dict:
    parser = IssuerHTML()
    parser.feed(raw.decode("utf-8-sig"))
    facts = {}
    if ticker in VANGUARD:
        document = parser.profile
        if not isinstance(document, dict) or document.get("overview", {}).get("fundId") != VANGUARD[ticker]:
            raise ValueError("Official Vanguard structured fund identity missing")
        overview = document["overview"]
        for key, value_key, day_key in (("nav", "navPrice", "navPriceAsOfDate"),
                                       ("issuer_reported_market_close", "marketPrice", "marketPriceAsOfDate"),
                                       ("nav_daily_change_pct", "navChangePct", "navChangeAsOfDate"),
                                       ("market_daily_change_pct", "marketPriceChangePct", "marketPriceChangeAsOfDate")):
            if overview.get(value_key) is not None and overview.get(day_key):
                facts[key] = {"value": str(number(overview[value_key])), "source_as_of": source_day(overview[day_key], captured_at),
                              "unit": "percent" if key.endswith("pct") else "USD"}
        facts["asset_class"] = overview.get("assetClass")
        facts["style"] = overview.get("category")
        facts["expense_ratio_pct"] = str(number(overview["expenseRatio"])) if overview.get("expenseRatio") else None
        facts["expense_ratio_as_of"] = source_day(overview["expenseRatioAsOfDate"], captured_at) if overview.get("expenseRatioAsOfDate") else None
    elif ticker in ISHARES:
        text = " ".join(parser.text)
        nav = re.search(r"NAV as of\s+([A-Za-z]{3} \d{1,2}, \d{4})\s+(?:\$\s*){0,2}([\d,.]+)", text)
        close = re.search(r"Closing Price\s+\$([\d,.]+)\s+as of\s+([A-Za-z]{3} \d{1,2}, \d{4})", text)
        if nav:
            facts["nav"] = {"value": str(number(nav[2])), "source_as_of": source_day(nav[1], captured_at), "unit": "USD"}
        if close:
            facts["issuer_reported_market_close"] = {"value": str(number(close[1])), "source_as_of": source_day(close[2], captured_at), "unit": "USD"}
    if not facts:
        raise ValueError("No supported dated official fund facts")
    return {**_receipt(ticker, source_url, raw, captured_at), "status": "AVAILABLE", "facts": facts,
            "not_a_live_quote": True, "limitations": ["NAV and issuer-reported market closing price are distinct values with separate dates.",
                                                        "No OHLC history, executable quote, order level or predictive probability inferred."]}


async def fetch_etf_holdings(ticker: str) -> dict:
    if ticker in VANGUARD:
        return {"ticker": ticker, "status": "SOURCE_PERMISSION_REQUIRED", "holdings": [],
                "source_url": f"https://investor.vanguard.com/investment-products/etfs/profile/{ticker.lower()}",
                "rights_scope": SCOPE, "unknown_residual_pct": "100", "weights_renormalized": False,
                "reason": "Vanguard portfolio-holdings disclosure prohibits reproduction without permission. No top-ten substitution. A permitted official filing/source is required.",
                "execution_enabled": False}
    if ticker in INVESCO:
        return {"ticker": ticker, "status": "SOURCE_PERMISSION_REQUIRED", "holdings": [],
                "source_url": "https://www.invesco.com/us/en/financial-products/etfs/invesco-sp-500-momentum-etf.html",
                "terms_url": "https://www.invesco.com/us/en/resources/terms-of-use.html",
                "rights_scope": SCOPE, "unknown_residual_pct": "100", "weights_renormalized": False,
                "reason": "Official holdings parser verified, but permission for repeated automated access is not established under Invesco's terms. Use an authorized owner-provided official file or obtain permission.",
                "execution_enabled": False}
    if ticker in ISHARES:
        url, parser = ISHARES[ticker]+"/latest-holdings.csv", parse_ishares_holdings
    else:
        return {"ticker": ticker, "status": "NOT_CONNECTED", "holdings": [], "unknown_residual_pct": "100",
                "reason": "No reviewed official holdings adapter connected for this fund", "rights_scope": SCOPE, "execution_enabled": False}
    try:
        raw = await issuer_bytes(url)
        return parser(raw, ticker=ticker, source_url=url, captured_at=now())
    except (ValueError, KeyError, TypeError, UnicodeError, httpx.HTTPError, asyncio.TimeoutError):
        return {"ticker": ticker, "status": "SOURCE_UNAVAILABLE", "source_url": url, "holdings": [],
                "unknown_residual_pct": "100", "reason": "Official issuer download unavailable or failed schema/completeness validation; no substituted holdings",
                "rights_scope": SCOPE, "execution_enabled": False}


async def fetch_etf_facts(ticker: str) -> dict:
    if ticker in VANGUARD:
        return {"ticker":ticker,"status":"SOURCE_PERMISSION_REQUIRED","execution_enabled":False,"rights_scope":SCOPE,
          "reason":"Vanguard's current terms prohibit repeated automated website access. Dated issuer-facts parsing is retained for an authorized owner-provided source, but automatic retrieval is disabled pending permission.",
          "terms_url":"https://investor.vanguard.com/terms-conditions"}
    url = ISHARES.get(ticker) or (f"https://investor.vanguard.com/investment-products/etfs/profile/{ticker.lower()}" if ticker in VANGUARD else None)
    if not url:
        return {"ticker": ticker, "status": "NOT_CONNECTED", "reason": "No reviewed dated issuer-facts parser for this fund", "execution_enabled": False}
    try:
        raw = await issuer_bytes(url)
        return parse_etf_facts(raw, ticker=ticker, source_url=url, captured_at=now())
    except (ValueError, KeyError, TypeError, UnicodeError, httpx.HTTPError, asyncio.TimeoutError):
        return {"ticker": ticker, "status": "SOURCE_UNAVAILABLE", "source_url": url,
                "reason": "Official dated fund facts unavailable or failed validation", "execution_enabled": False}
