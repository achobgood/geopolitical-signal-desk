"""Allowlisted official RSS collector. Never follows links or inspects media."""
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from defusedxml import ElementTree

from app.models import LeadIn, Provenance

FEED_URL = "https://www.federalreserve.gov/feeds/press_all.xml"
MAX_BYTES = 512_000


class Provider(Protocol):
    """New providers need explicit public-access authorization and a bounded fetch."""
    name: str
    async def fetch(self) -> list[LeadIn]: ...


def parse_feed(data: bytes) -> list[LeadIn]:
    if len(data) > MAX_BYTES:
        raise ValueError("Feed size limit")
    root = ElementTree.fromstring(data)
    items = root.findall("./channel/item")
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("Expected RSS channel")
    leads = []
    for item in items[:20]:
        link = (item.findtext("link") or "").strip()
        title = (item.findtext("title") or "").strip()
        u = urlsplit(link)
        if u.hostname != "www.federalreserve.gov" or not u.path.startswith("/newsevents/pressreleases/"):
            continue
        posted = None
        try:
            posted = parsedate_to_datetime(item.findtext("pubDate") or "")
            if posted.tzinfo is None:
                posted = None
        except (TypeError, ValueError, OverflowError):
            pass
        leads.append(LeadIn(
            source_url=link, source_platform="official_rss", source_kind="official",
            observation=title, broad_region="United States", original_posted_at=posted,
            capture_time_note="RSS publication time, if parseable; event time not inferred",
            observed_vs_inferred="Headline observed in official RSS; underlying article and media not inspected",
            reliability_note="Official Federal Reserve feed; does not independently verify the underlying claim",
            provenance=Provenance(account_type="official", language_code="en", originality="original",
                media_access="metadata_only", permitted_public=True, safety_reviewed=True,
                collection_limitations="Allowlisted economic official headlines only; no article or media retrieval")))
    return leads


class FederalReserveProvider:
    name = "federal_reserve_press"

    async def fetch(self):
        # A total deadline bounds a peer that trickles bytes beneath the read timeout.
        async with asyncio.timeout(25):
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
                async with client.stream("GET", FEED_URL, headers={"User-Agent": "GeopoliticalSignalDesk/1.0 RSS reader"}) as r:
                    r.raise_for_status()
                    data = bytearray()
                    async for chunk in r.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise ValueError("Feed size limit")
        return parse_feed(bytes(data))


async def collect(store, ingest, provider=None):
    provider = provider or FederalReserveProvider()
    run = await store.insert("collection_runs", {"collector": provider.name,
        "access_limitations": "Only official RSS headlines; no social APIs, full articles, or media"})
    seen = new = 0
    status, error = "completed", ""
    try:
        leads = await provider.fetch()
        for lead in leads:
            result = await ingest(lead, store, "collector:federal_reserve_press")
            seen += 1
            new += int(result["created"])
    except Exception:
        # Do not persist arbitrary HTTP errors: they can contain credentials or content.
        status, error = ("partial" if seen else "failed"), "Collection failed; inspect provider access and server readiness"
    finished = {"finished_at": datetime.now(timezone.utc).isoformat(), "run_status": status,
        "observations_seen": seen, "new_leads": new, "error_summary": error}
    await store.patch("collection_runs", run["id"], finished)
    return {**run, **finished}
