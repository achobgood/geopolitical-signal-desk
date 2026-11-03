import asyncio
import pytest
from pydantic import ValidationError
from app.search_intake import SearchBatch,normalize_hit,import_search_batch

def payload():
    return {"batch_id":"test:search","provider":"fixture search","searched_at":"2026-11-03T12:00:00Z",
      "queries":[{"id":"gulf","query":"public civilian outage report","language":"en","requested_window":"past 24 hours"}],
      "hits":[{"query_id":"gulf","source_url":"https://example.org/public/report","source_title":"Unverified public report",
      "observation":"A source alleges a civilian service interruption.","original_page_access":"snippet_only",
      "limitations":"Underlying page and claim not inspected", "permitted_public":True,"safety_reviewed":True}],
      "run_limitations":"Synthetic fixture only"}

def test_search_snippet_cannot_become_inspected_article_or_fresh_event():
    b=SearchBatch(**payload());lead=normalize_hit(b,b.hits[0])
    assert lead.original_posted_at is None and lead.claimed_event_at is None
    assert lead.provenance.content_access=='metadata_only'
    assert lead.provenance.media_access=='not_inspected'
    assert 'public civilian outage report' in lead.provenance.collection_limitations
    assert '2026-11-03T12:00:00+00:00' in lead.provenance.collection_limitations
    assert lead.provenance.independence=='unknown'

@pytest.mark.parametrize('change',[{'searched_at':'2026-11-03T12:00:00'}, {'queries':[]}])
def test_invalid_search_context_rejected(change):
    with pytest.raises(ValidationError):SearchBatch(**{**payload(),**change})

def test_hit_must_point_to_a_recorded_query():
    p=payload();p['hits'][0]['query_id']='unrecorded'
    with pytest.raises(ValidationError):SearchBatch(**p)

def test_search_requires_safe_public_permalink_and_review():
    p=payload();p['hits'][0]['source_url']='https://127.0.0.1/secret'
    with pytest.raises(ValidationError):SearchBatch(**p)
    p=payload();p['hits'][0]['safety_reviewed']=False
    with pytest.raises(ValidationError):SearchBatch(**p)

def test_duplicate_search_keeps_new_count_zero_and_records_query_linkage():
    class Store:
        async def insert(self,*args):return {'id':1}
        async def patch(self,*args):self.saved=args[-1]
    async def ingest(*args):return {'created':False,'lead':{'id':42}}
    s=Store();r=asyncio.run(import_search_batch(SearchBatch(**payload()),s,ingest,'test'))
    assert r['new_leads']==0 and r['results'][0]['lead_id']==42
    assert s.saved['details']['queries'][0]['id']=='gulf'
    assert s.saved['details']['unattended'] is False
    assert r['background_search_enabled'] is False

def test_partial_search_intake_records_failure_without_echoing_secret():
    p=payload();p['hits'].append({**p['hits'][0],'source_url':'https://example.org/second'})
    class Store:
        async def insert(self,*args):return {'id':1}
        async def patch(self,*args):self.saved=args[-1]
    count=0
    async def ingest(*args):
        nonlocal count;count+=1
        if count==2:raise ValueError('secret-canary')
        return {'created':True,'lead':{'id':42}}
    s=Store();r=asyncio.run(import_search_batch(SearchBatch(**p),s,ingest,'test'))
    assert r['status']=='partial' and r['new_leads']==1
    assert 'secret-canary' not in s.saved['error_summary']

def test_backfill_can_preserve_unknown_capture_time_and_direct_url_recovery():
    p=payload();p.update(search_time_precision='as_of_checkpoint',search_time_note='Sources were recovered before this clock checkpoint; per-request times unavailable')
    p['queries'][0]['method']='direct_url';p['queries'][0]['query']='https://example.org/public/report'
    b=SearchBatch(**p);lead=normalize_hit(b,b.hits[0])
    assert 'capture times unknown' in lead.provenance.collection_limitations
    assert 'Method: direct_url' in lead.provenance.collection_limitations
    p['search_time_note']=''
    with pytest.raises(ValidationError):SearchBatch(**p)
