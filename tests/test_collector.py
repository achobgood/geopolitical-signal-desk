import asyncio
import pytest
from defusedxml.common import DefusedXmlException

from app.collector import MAX_BYTES, collect, parse_feed

RSS = b'''<rss><channel><item><title>Official economic notice</title>
<link>https://www.federalreserve.gov/newsevents/pressreleases/test.htm</link>
<pubDate>Tue, 03 Nov 2026 12:00:00 GMT</pubDate></item></channel></rss>'''


def test_official_feed_retains_provenance_without_media_claims():
    lead = parse_feed(RSS)[0]
    assert lead.provenance.media_access == "metadata_only"
    assert lead.claimed_event_at is None
    assert lead.original_posted_at.utcoffset().total_seconds() == 0


def test_feed_does_not_accept_outside_links():
    assert parse_feed(RSS.replace(b"www.federalreserve.gov/", b"evil.example/")) == []


def test_xml_entities_and_oversized_feeds_rejected():
    with pytest.raises(DefusedXmlException):
        parse_feed(b'<!DOCTYPE rss [<!ENTITY ex "test">]><rss><channel><item>&ex;</item></channel></rss>')
    with pytest.raises(ValueError):
        parse_feed(b"x" * (MAX_BYTES + 1))
    with pytest.raises(ValueError):
        parse_feed(b"<html/>")


def test_missing_publication_date_is_unknown():
    lead = parse_feed(RSS.replace(b"Tue, 03 Nov 2026 12:00:00 GMT", b"invalid"))[0]
    assert lead.original_posted_at is None


def test_partial_failure_retains_run_counts():
    class Store:
        async def insert(self, *args): return {"id": 1}
        async def patch(self, table, row_id, body): self.result = body
    class Provider:
        name = "fixture"
        async def fetch(self): return parse_feed(RSS) * 2
    count = 0
    async def ingest(*args):
        nonlocal count
        count += 1
        if count == 2: raise ValueError("private-key-canary")
        return {"created": True}
    store = Store()
    result = asyncio.run(collect(store, ingest, Provider()))
    assert result["run_status"] == "partial"
    assert result["new_leads"] == 1
    assert "private-key-canary" not in result["error_summary"]
