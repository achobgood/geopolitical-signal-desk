"""Shared bounded public provider interface. Restricted platforms are never scraped.

Adapters emit the existing LeadIn contract; ingestion/dedup stays provider-neutral.
Public-social discovery retains metadata only until an analyst inspects the post.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from defusedxml import ElementTree
from app.models import LeadIn, Provenance


@dataclass(frozen=True)
class Capabilities:
    name: str
    category: str
    limitations: str
    max_items: int = 20
    min_interval_seconds: int = 3600
    original_discovery: bool = False


@dataclass
class Batch:
    leads: list[LeadIn]
    seen: int
    dropped: int = 0
    cursor: dict = field(default_factory=dict)


class Provider(Protocol):
    capabilities: Capabilities
    async def fetch(self, cursor: dict) -> Batch: ...


async def public_bytes(url, max_bytes=600_000):
    # Every caller supplies a constant URL from a reviewed adapter, never intake URLs.
    async with asyncio.timeout(30):
        async with httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", url, headers={"User-Agent":"GeopoliticalSignalDesk/1.0 permitted-public-source reader"}) as r:
                r.raise_for_status()
                data=bytearray()
                async for chunk in r.aiter_bytes():
                    data.extend(chunk)
                    if len(data)>max_bytes: raise ValueError("Source response exceeds bound")
                return bytes(data)


def stamp(value):
    try:
        d=parsedate_to_datetime(value)
        return d if d.utcoffset() is not None else None
    except (ValueError,TypeError,OverflowError): return None


SENSITIVE = re.compile(r"\b(convoy|battalion|brigade|checkpoint|coordinates|live location|troops moving|deployment route|target coordinates)\b",re.I)


class UNMiddleEast:
    capabilities=Capabilities("un_middle_east","official_geopolitical","UN institutional headline window only; no comprehensive geography, firsthand social discovery or article/media inspection")
    async def fetch(self,cursor):
        return self.parse(await public_bytes("https://news.un.org/feed/subscribe/en/news/region/middle-east/feed/rss.xml"))
    def parse(self,data):
        root=ElementTree.fromstring(data)
        if root.tag!="rss":raise ValueError("Expected RSS")
        leads=[];items=root.findall("./channel/item")[:20]
        for item in items:
            url=(item.findtext("link") or "").strip().replace("/feed/view/en/story/","/en/story/")
            title=(item.findtext("title") or "").strip()
            u=urlsplit(url)
            if u.scheme!="https" or u.hostname!="news.un.org" or not u.path.startswith("/en/story/") or not title or SENSITIVE.search(title):continue
            leads.append(LeadIn(source_url=url,source_platform="un_news_rss",source_kind="official",observation=title,
                broad_region="Middle East",original_posted_at=stamp(item.findtext("pubDate")),
                capture_time_note="RSS publication metadata; event time not inferred",
                observed_vs_inferred="Headline observed in UN RSS. Article text and media not inspected by collector.",
                reliability_note="Institutional report; attribution is not independent verification",
                provenance=Provenance(permitted_public=True,safety_reviewed=True,account_type="official",language_code="en",
                    topic="Middle East institutional reporting",origin_group="UN News; underlying origin not yet traced",
                    safety_review_method="Bounded public headline and sensitive-term exclusion; not semantic verification",
                    media_access="metadata_only",collection_limitations=self.capabilities.limitations)))
        return Batch(leads,len(items),len(items)-len(leads))


class MastodonDiscovery:
    capabilities=Capabilities("mastodon_iran","public_social_discovery","One instance's known public #Iran cache, max20/poll; metadata only. No text, account profile or media retained. Not all Mastodon, not X/Telegram, no guarantee of firsthand origin.",original_discovery=True)
    async def fetch(self,cursor):
        suffix="&min_id="+str(int(cursor["min_id"])) if cursor.get("min_id") else ""
        return self.parse(json.loads(await public_bytes("https://mastodon.social/api/v1/timelines/tag/iran?limit=20"+suffix)))
    def parse(self,items):
        if not isinstance(items,list):raise ValueError("Expected statuses")
        leads=[];seen=items[:20];ids=[]
        for x in seen:
            if not isinstance(x,dict) or x.get("visibility")!="public" or x.get("reblog") is not None:continue
            url=x.get("url","");u=urlsplit(url)
            if u.scheme!="https" or not u.hostname or u.username or u.password:continue
            # Do not persist arbitrary text, public/private account IDs or tactical claims.
            # Original-source review is a separate analyst action, not corroboration.
            try:posted=datetime.fromisoformat(x["created_at"].replace("Z","+00:00"));ids.append(int(x["id"]))
            except (ValueError,KeyError,TypeError):continue
            leads.append(LeadIn(source_url=url,source_platform="mastodon_public",source_kind="public_social",
                observation="Public #Iran social observation discovered; post text withheld pending original-source review.",
                broad_region="Iran / Gulf (tag-based, location not verified)",original_posted_at=posted,
                capture_time_note="Platform post timestamp; author location/event time not established",
                observed_vs_inferred="Only public visibility, permalink, timestamp, language and engagement metadata were collected. No firsthand claim inferred.",
                source_independence_note="No boost object does not establish originality. Underlying sources and independence unassessed.",
                provenance=Provenance(permitted_public=True,safety_reviewed=True,language_code=x.get("language") or "und",
                    topic="Iran public discovery",origin_group="unknown",provider_item_id=str(x["id"]),
                    media_access="not_inspected",safety_review_method="Metadata-only: no post text, profile or media retained",
                    engagement_counts={"favourites":max(0,int(x.get("favourites_count",0))),"boosts":max(0,int(x.get("reblogs_count",0))),"replies":max(0,int(x.get("replies_count",0)))},
                    collection_limitations=self.capabilities.limitations)))
        return Batch(leads,len(seen),len(seen)-len(leads),{"min_id":str(max(ids))} if ids else {})


class CisaKEV:
    capabilities=Capabilities("cisa_kev","cyber_infrastructure","Latest six known-exploited catalog additions, not incident telemetry, attribution, exploit timing or regional targeting",max_items=6)
    async def fetch(self,cursor):
        return self.parse(json.loads(await public_bytes("https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",3_000_000)))
    def parse(self,data):
        items=sorted(data["vulnerabilities"],key=lambda x:x["dateAdded"],reverse=True)[:6];leads=[]
        for x in items:
            cve=x["cveID"]
            if not re.fullmatch(r"CVE-\d{4}-\d{4,10}",cve):continue
            leads.append(LeadIn(source_url="https://www.cisa.gov/known-exploited-vulnerabilities-catalog?search_api_fulltext="+cve,
                source_platform="cisa_kev",source_kind="official",broad_region="Global cyber / infrastructure",
                observation=f"CISA KEV catalog lists {cve} as known exploited. This is a catalog entry, not an incident or regional attribution.",
                original_posted_at=datetime.fromisoformat(x["dateAdded"]).replace(tzinfo=timezone.utc),
                capture_time_note="Catalog dateAdded has day precision; midnight UTC is representational, not an exploitation time",
                observed_vs_inferred="CVE identifier and catalog addition date read from CISA structured catalog; no target systems inspected",
                provenance=Provenance(permitted_public=True,safety_reviewed=True,account_type="official",language_code="en",origin_group="CISA KEV:"+cve,
                    topic="Known exploited vulnerability catalog",provider_item_id=cve,media_access="metadata_only",
                    safety_review_method="Structured CVE/date metadata only",collection_limitations=self.capabilities.limitations)))
        return Batch(leads,len(items))


PROVIDERS={p.capabilities.name:p for p in [MastodonDiscovery(),UNMiddleEast(),CisaKEV()]}
BLOCKED={
    "x":{"category":"public X","status":"authorization_required","reason":"Written X approval for intelligence/sensitive-event monitoring plus an entitled API account required; no scraping fallback"},
    "telegram":{"category":"public Telegram","status":"authorization_required","reason":"AI aggregation/deployment restrictions require documented permission/consent; API credentials alone are insufficient"},
    "reddit":{"category":"Reddit/community","status":"approval_required","reason":"Approved use case and OAuth required; new public API approval availability restricted"},
    "meta":{"category":"Facebook/Instagram","status":"approval_required","reason":"Approved Meta interfaces and appropriate account/permissions; no private groups or scraping"},
    "maritime":{"category":"maritime aggregates","status":"not_configured","reason":"Licensed aggregate/port access needed; no live military/vessel tracks"},
    "aviation":{"category":"aviation aggregates","status":"not_configured","reason":"Authorized aggregate interface needed; no individual military routes/times"},
    "energy_market":{"category":"energy/market","status":"not_configured","reason":"Authorized market/energy dataset and key/usage terms needed; Fed RSS is only ingestion POC"},
    "local_community":{"category":"local-language/community","status":"partial","reason":"Mastodon preserves language metadata only; no comprehensive local communities, translation or firsthand verification"}}


def coverage():
    return [{"provider":p.capabilities.name,"category":p.capabilities.category,"status":"implemented","scope":p.capabilities.limitations,
             "max_items":p.capabilities.max_items,"min_interval_seconds":p.capabilities.min_interval_seconds,
             "original_discovery_potential":p.capabilities.original_discovery,"schedule_verified":False} for p in PROVIDERS.values()]+[{"provider":k,**v} for k,v in BLOCKED.items()]
