import asyncio
import pytest
from app.providers import MastodonDiscovery, UNMiddleEast, UNGeneva, CisaKEV, coverage
from app.pipeline import provider_run, run_pipeline


def test_social_discovery_does_not_retain_text_profiles_or_imply_firsthand():
    items=[{"id":"1234","visibility":"public","reblog":None,"url":"https://example.org/public/post",
            "created_at":"2026-11-03T12:00:00Z","language":"ar","content":"SENSITIVE MUST NOT BE RETAINED",
            "account":{"id":"private-account-id","display_name":"PRIVATE NAME"},"favourites_count":0,"reblogs_count":0}]
    batch=MastodonDiscovery().parse(items)
    assert len(batch.leads)==1
    text=batch.leads[0].model_dump_json()
    assert "SENSITIVE" not in text and "PRIVATE NAME" not in text and "private-account-id" not in text
    assert batch.leads[0].provenance.originality=="unknown"
    assert batch.leads[0].provenance.language_code=="ar"
    assert batch.cursor=={"min_id":"1234"}


def test_private_social_and_boosts_excluded():
    assert MastodonDiscovery().parse([{"visibility":"private"},{"visibility":"public","reblog":{"id":"123"}}]).leads==[]


def test_kev_is_catalog_not_regional_incident():
    b=CisaKEV().parse({"vulnerabilities":[{"cveID":"CVE-2026-12345","dateAdded":"2026-11-03"}]})
    assert len(b.leads)==1
    assert "not an incident" in b.leads[0].observation
    assert "day precision" in b.leads[0].capture_time_note


def test_un_feed_preserves_uncertainty_and_excludes_sensitive_headlines():
    data=b'<rss><channel><item><title>Diplomatic statement</title><link>https://news.un.org/feed/view/en/story/2026/10/123</link></item><item><title>Convoy coordinates</title><link>https://news.un.org/en/story/2026/10/124</link></item></channel></rss>'
    b=UNMiddleEast().parse(data)
    assert len(b.leads)==1 and b.dropped==1
    assert b.leads[0].original_posted_at is None
    assert b.leads[0].source_url=='https://news.un.org/en/story/2026/10/123'


def test_coverage_does_not_claim_restricted_sources_are_live():
    values={v['provider']:v for v in coverage()}
    assert values['x']['status']=='authorization_required'
    assert values['telegram']['status']=='authorization_required'
    assert values['reddit']['status']=='approval_required'


def test_geneva_retains_only_index_and_topic_not_full_sensitive_body():
    rss=b'<rss><channel><item><title>UN Geneva briefing</title><link>https://www.ungeneva.org/en/news-media/press-briefing/test</link><description>Yemen. PRIVATE NAMES AND TACTICAL ROUTE DETAILS</description></item></channel></rss>'
    batch=UNGeneva().parse(rss)
    assert len(batch.leads)==1
    assert 'PRIVATE NAMES' not in batch.leads[0].model_dump_json()
    assert batch.leads[0].provenance.content_access=='metadata_only'


def test_geneva_does_not_claim_unrelated_briefing_is_geopolitical_signal():
    rss=b'<rss><channel><item><title>Statistics briefing</title><link>https://www.ungeneva.org/en/news-media/press-briefing/test</link><description>Unrelated statistics</description></item></channel></rss>'
    assert UNGeneva().parse(rss).leads==[]


def test_duplicate_pipeline_lease_does_not_collect():
    class Store:
        async def rpc(self,*args):return {"claimed":False,"run":{"id":1,"run_status":"completed"}}
    async def forbidden(*args):raise AssertionError('Should not collect')
    result=asyncio.run(run_pipeline(Store(),forbidden,'same-hour','test'))
    assert result['duplicate'] is True


def test_recent_success_paces_provider_without_network():
    from datetime import datetime,timezone
    class Store:
        async def rows(self,*args,**kwargs):return [{"finished_at":datetime.now(timezone.utc).isoformat()}]
    async def forbidden(*args):raise AssertionError('Should not collect')
    result=asyncio.run(provider_run(MastodonDiscovery(),Store(),forbidden))
    assert result['status']=='paced'
