"""Bounded, first-party market reference feeds. Not equity quotes or AI issuer CDS.

Treasury yields and Cboe daily volatility CLOSE are macro/price-confirmation context.
Public timestamps use the time the feed was ACTUALLY fetched because the exact
upstream publication minute is not provided; session event dates are day precision.
Never backdate a publication based on the price bar's session date.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable
from urllib.parse import urlsplit

import httpx
from defusedxml import ElementTree

from app.market import MarketObservation, expected_completed_market_session

TREASURY_URL_TEMPLATE = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value_month={month}"
)
CBOE_URLS = {
    "cboe_vix": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    "cboe_vxn": "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VXN_History.csv",
}
ALLOWED_HOSTS = frozenset(("home.treasury.gov", "cdn-api.cboe.com"))
ALLOWED_FORMS = frozenset(("application/xml", "text/xml", "text/csv", "application/csv", "text/plain", "application/octet-stream"))


@dataclass(frozen=True)
class Quote:
    session: date
    value: Decimal


@dataclass(frozen=True)
class SourceReading:
    provider: str
    session: date
    metric: str
    value: Decimal
    unit: str
    source_url: str
    summary: str
    phase: str = "confirmation"
    pressure: str = "ambiguous"
    caveats: str = ""


def decimal_value(text: str, minimum: Decimal, maximum: Decimal) -> Decimal:
    try:
        d = Decimal(text.strip().replace(",", ""))
        if not d.is_finite() or not minimum <= d <= maximum:
            raise ValueError("Out-of-range market feed value")
        return d
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError("Invalid market feed numeric value") from exc


def parse_date(text: str) -> date:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError("Unrecognized market session date")


def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def treasury_parse(blob: bytes, *, source_url: str, today: date) -> list[SourceReading]:
    root = ElementTree.fromstring(blob)
    if localname(root.tag).lower() != "feed":
        raise ValueError("Treasury returned an unexpected XML envelope")
    entries = [child for child in root if localname(child.tag) == "entry"]
    if not entries:
        raise ValueError("Treasury monthly feed has no rows")
    results = []
    for entry in entries:
        values = {}
        for prop in entry.iter():
            name = localname(prop.tag)
            if name in ("NEW_DATE", "BC_2YEAR", "BC_10YEAR"):
                values[name] = (prop.text or "").strip()
        if not all(values.get(k) for k in ("NEW_DATE", "BC_2YEAR", "BC_10YEAR")):
            continue
        try:
            session = parse_date(values["NEW_DATE"])
            two_year = decimal_value(values["BC_2YEAR"], Decimal("-2"), Decimal("30"))
            ten_year = decimal_value(values["BC_10YEAR"], Decimal("-2"), Decimal("30"))
        except ValueError:
            continue
        if session > today:
            continue
        results.append((session, two_year, ten_year))
    if not results:
        raise ValueError("Treasury feed has no valid observed sessions")
    results.sort(key=lambda row: row[0])
    session, two_year, ten_year = results[-1]
    result = []
    for label, value, maturity in (
        ("us_treasury_2y_yield_pct", two_year, "2-year"),
        ("us_treasury_10y_yield_pct", ten_year, "10-year"),
    ):
        result.append(SourceReading(
            provider="us_treasury", session=session, metric=label, value=value,
            unit="percent annual yield", source_url=source_url,
            summary=f"U.S. Treasury reported the {maturity} par yield for {session.isoformat()} as {value}% at its published daily session.",
            phase="leading", pressure="ambiguous",
            caveats="Official daily par yield, not an AI company borrowing cost. Exact public release minute is unknown; captured timestamp is used as publication availability.",
        ))
    if len(results) >= 6:
        old_date, _, previous_ten = results[-6]
        delta = (ten_year - previous_ten) * Decimal("100")
        result.append(SourceReading(
            provider="us_treasury", session=session, metric="us_treasury_10y_5session_delta_bp",
            value=delta, unit="basis points", source_url=source_url,
            summary=f"Ten-year par yield changed {delta:+} basis points over five published Treasury sessions ({old_date.isoformat()} to {session.isoformat()}).",
            phase="leading", pressure="ambiguous",
            caveats="Computed from the source's last six valid observed sessions. Does not isolate AI company credit spreads or determine equity direction.",
        ))
    return result


def cboe_parse(blob: bytes, *, provider: str, source_url: str, today: date) -> list[SourceReading]:
    text = blob.decode("utf-8-sig", errors="strict")
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.lstrip().upper().startswith("DATE,")), None)
    if start is None:
        raise ValueError("Cboe CSV header missing")
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    if reader.fieldnames is not None:
        reader.fieldnames = [name.strip().upper() for name in reader.fieldnames]
    quotes = {}
    for item in reader:
        try:
            session = parse_date(item["DATE"])
            close = decimal_value(item["CLOSE"], Decimal("0"), Decimal("500"))
        except (ValueError, KeyError, TypeError):
            continue
        if session <= today:
            quotes[session] = close
    if not quotes:
        raise ValueError("Cboe published no valid close observations")
    series = sorted(quotes.items())
    session, close = series[-1]
    index = "VIX" if provider == "cboe_vix" else "VXN"
    records = [SourceReading(
        provider=provider, session=session, metric=index.lower() + "_daily_close",
        value=close, unit="index points", source_url=source_url,
        summary=f"Cboe published the {index} historical daily close of {close} index points for {session.isoformat()}.",
        phase="confirmation", pressure="ambiguous",
        caveats="Official daily historical volatility index close, not an equity closing price or a real-time quote. High implied volatility predicts neither direction nor a market bottom.",
    )]
    if len(series) >= 6 and series[-6][1] != 0:
        start_day, old = series[-6]
        pct_change = (close / old - Decimal(1)) * Decimal(100)
        pct_change = pct_change.quantize(Decimal(".01"))
        records.append(SourceReading(
            provider=provider, session=session, metric=index.lower() + "_5session_change_pct",
            value=pct_change, unit="percent", source_url=source_url,
            summary=f"Cboe {index} close changed {pct_change:+}% over five source-reported sessions ({start_day.isoformat()} to {session.isoformat()}).",
            phase="confirmation", pressure="risk_up" if pct_change >= Decimal("20") else "ambiguous",
            caveats="Volatility-price confirmation, not an advance warning. A 20% five-session rise is a descriptive attention threshold, not a backtested market direction predictor.",
        ))
    return records


async def fixed_public_data(url: str, *, max_bytes=2_200_000) -> bytes:
    """Allowlisted addresses only; no redirects, no proxies or request-supplied URL."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.port not in (None, 443):
        raise ValueError("Market feed endpoint outside reviewed source allowlist")
    async with asyncio.timeout(27):
        async with httpx.AsyncClient(timeout=18, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", url, headers={"User-Agent":"SignalDeskMarketResearch/1.0 (public daily data)"}) as response:
                response.raise_for_status()
                if response.status_code != 200:
                    raise ValueError("Market feed did not return 200")
                content = bytearray()
                async for part in response.aiter_bytes():
                    content.extend(part)
                    if len(content) > max_bytes:
                        raise ValueError("Market source response exceeded size limit")
                return bytes(content)


def as_observation(reading: SourceReading, captured: datetime) -> MarketObservation:
    value = str(reading.value)
    return MarketObservation(
        source_url=reading.source_url,
        source_title=f"{reading.provider.upper()} · {reading.metric.replace('_',' ')} · {reading.session.isoformat()}",
        publisher="U.S. Department of the Treasury" if reading.provider == "us_treasury" else "Cboe Global Markets",
        published_at=captured,
        event_at=datetime.combine(reading.session, datetime.min.time(), tzinfo=timezone.utc),
        family="rates_liquidity" if reading.provider == "us_treasury" else "market_breadth",
        phase=reading.phase, pressure=reading.pressure, claim_status="primary_document",
        origin_group=f"{reading.provider}:{reading.session.isoformat()}",
        summary=reading.summary, metric_code=reading.metric, metric_value=reading.value,
        metric_unit=reading.unit, caveats=reading.caveats,
    )


async def run_market_feed(store, run_key: str, trigger: str) -> dict:
    """Run all scheduled first-party sources. Partial failures remain explicit."""
    claim = await store.rpc("market_feed_claim_v1", {"p_key": run_key, "p_trigger": trigger})
    if not claim["claimed"]:
        return {"duplicate": True, "run": claim["run"]}
    results = []
    status = "completed"
    try:
        now = datetime.now(timezone.utc)
        today = now.date()
        month = today.strftime("%Y%m")
        providers = [("us_treasury", TREASURY_URL_TEMPLATE.format(month=month))]
        providers.extend(CBOE_URLS.items())
        async with asyncio.timeout(115):
            for provider, url in providers:
                count = added = 0
                try:
                    blob = await fixed_public_data(url)
                    readings = treasury_parse(blob, source_url=url, today=today) if provider == "us_treasury" else cboe_parse(
                        blob, provider=provider, source_url=url, today=today
                    )
                    for reading in readings:
                        # The source snapshot is captured only at ingestion time.
                        # A later backfill cannot pretend the record was known earlier.
                        when = datetime.now(timezone.utc)
                        entry = as_observation(reading, when)
                        body = entry.model_dump(mode="json")
                        key_payload = {
                            "provider": provider, "date": reading.session.isoformat(),
                            "metric": reading.metric, "value": str(reading.value),
                        }
                        key = "market-feed-v1:" + hashlib.sha256(
                            json.dumps(key_payload, sort_keys=True).encode()
                        ).hexdigest()
                        result = await store.rpc("market_record_observation_v1", {
                            "payload": body, "p_key": key, "p_actor": "collector:" + provider
                        })
                        count += 1
                        added += int(result.get("created", False))
                    session = max(r.session for r in readings)
                    stale = session < expected_completed_market_session(datetime.now(timezone.utc))
                    source_status = "stale" if stale else "completed"
                    results.append({"provider": provider, "status": source_status,
                                    "readings": count, "new_records": added,
                                    "latest_session": session.isoformat(),
                                    "validation": "expected_latest_completed_us_session_weekday_only"})
                    if stale:
                        status = "partial"
                except Exception as exc:
                    # Do not leak third-party body or tokens; preserve source-level failure.
                    error = "http_or_timeout" if isinstance(exc, (httpx.HTTPError, asyncio.TimeoutError)) else "invalid_or_unavailable_feed"
                    results.append({"provider": provider, "status": "failed", "reason": error})
                    status = "partial"
            from app.market_sec import collect_sec_filings
            sec = await collect_sec_filings(store)
            results.append(sec)
            # Not configured is explicit coverage gap, not a failure of enabled feeds.
            if sec["status"] in ("partial", "failed"):
                status = "partial"
    except Exception:
        results.append({"provider": "collector", "status": "failed", "reason": "total_run_bound"})
        status = "partial"
    if not any(x["status"] == "completed" for x in results):
        status = "failed"
    receipt = {"status": status, "finished_at": datetime.now(timezone.utc).isoformat(),
               "provider_results": results, "error_summary": "" if status == "completed" else "One or more public feeds unavailable"}
    await store.patch("market_feed_runs", claim["run"]["id"], receipt)
    return {"duplicate": False, "run": {**claim["run"], **receipt}}
