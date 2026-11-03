import asyncio
import copy
import pytest
from pydantic import ValidationError
from app.analysis import EventReview, persist_review
from app.search_intake import SearchBatch, import_search_batch, normalize_hit


URL='https://example.org/public/original'
REPOST='https://example.net/public/repost'


def review_payload():
    return {'review_id':'review:fixture','event_key':'civilian-service-test','title':'Synthetic local fixture',
      'broad_region':'Test region','as_of':'2026-11-03T17:00:00Z','period_start':'2026-11-01T17:00:00Z','period_end':'2026-11-03T17:00:00Z',
      'summary':'One source alleges a disruption; event truth remains unknown.',
      'evidence':[{'source_url':URL,'origin_group':'unknown','origin_basis':'No source tracing established','limitations':'Original text not inspected'}],
      'hypotheses':[{'key':'disruption','scenario':'Possible civilian service interruption','rationale':'A specific public allegation warrants examination',
        'confidence':'low','supporting_urls':[URL],'alternative_explanations':'Routine maintenance or inaccurate allegation',
        'counterevidence':'No measurements or independent report available','strengthening_conditions':'Dated service-provider report',
        'falsification_criteria':'Original source retracts the alleged incident'}],
      'independence_assessment':'Single unknown-origin source; no corroboration established',
      'baseline_comparison':'No comparable activity baseline yet','analysis_threshold':'One specific public allegation is enough to assess',
      'decision':{'decision':'watch','rationale':'Uncertain event with possible significance','notification_threshold':'Consequential timely specific signal, assessed separately from collection'}}


def batch_payload():
    return {'batch_id':'fixture:review','provider':'local test','searched_at':'2026-11-03T17:00:00Z',
      'queries':[{'id':'q','query':'public service conditions'}],
      'hits':[{'query_id':'q','source_url':URL,'source_title':'Public source','observation':'An unverified service allegation',
        'original_page_access':'snippet_only','limitations':'No original-page inspection','permitted_public':True,'safety_reviewed':True}],
      'analyses':[review_payload()],'run_limitations':'Synthetic local fixture; never externally submitted'}


def test_single_source_review_is_valid_without_corroboration():
    r=EventReview(**review_payload())
    assert len(r.evidence)==1 and r.hypotheses[0].confidence=='low'


@pytest.mark.parametrize('case',['unknown_hypothesis_source','ambiguous_role','unknown_relation_source','duplicate_evidence','future_period','naive_time','summary_not_historical'])
def test_review_rejects_broken_evidence_or_time_contract(case):
    r=review_payload()
    if case=='unknown_hypothesis_source':r['hypotheses'][0]['supporting_urls']=[REPOST]
    if case=='ambiguous_role':r['hypotheses'][0]['contradicting_urls']=[URL]
    if case=='unknown_relation_source':r['relations']=[{'source_url':URL,'other_source_url':REPOST,'relation_type':'same_origin','rationale':'Uncollected'}]
    if case=='duplicate_evidence':r['evidence']*=2
    if case=='future_period':r['period_end']='2026-11-04T17:00:00Z'
    if case=='naive_time':r['as_of']='2026-11-03T17:00:00'
    if case=='summary_not_historical':r['report_provenance']={'report_id':'old-report','report_as_of':'2026-11-02T17:00:00Z','limitations':'Derivative report'}
    with pytest.raises(ValidationError):EventReview(**r)


def test_search_writer_cannot_assess_unsubmitted_sources():
    b=batch_payload();b['hits']=[]
    with pytest.raises(ValidationError):SearchBatch(**b)


def test_source_dates_survive_backfill_without_becoming_collection_time():
    b=batch_payload();b['purpose']='historical_backfill';b['hits'][0]['original_posted_at']='2026-11-01T19:45:00+01:00'
    b=SearchBatch(**b);lead=normalize_hit(b,b.hits[0])
    assert lead.original_posted_at.isoformat()=='2026-11-01T19:45:00+01:00'
    assert lead.claimed_event_at is None
    assert b.searched_at!=lead.original_posted_at


def test_shared_origin_trace_never_counts_as_independent_confirmation():
    r=review_payload();r['evidence'][0].update(origin_group='reported-interview',origin_basis='Original interview')
    r['evidence'].append({'source_url':REPOST,'origin_group':'reported-interview','origin_basis':'Explicitly cites the original interview','limitations':'Derivative report'})
    class Store:
        async def rpc(self,name,body):self.saved=body;return {'created':True,'cluster_id':1}
    s=Store();asyncio.run(persist_review(EventReview(**r),s,'research:test',{URL:1,REPOST:2}))
    p=s.saved['payload']
    assert p['relations'][0]['relation_type']=='same_origin'
    assert p['reported_origin_groups']==['reported-interview']
    assert p['independent_corroboration_count'] is None
    assert p['hypotheses'][0]['supporting_lead_ids']==[1]


def test_unknown_origins_are_never_automatically_linked():
    r=review_payload();r['evidence'].append({'source_url':REPOST,'origin_group':'unknown','origin_basis':'Untraced','limitations':'Unknown'})
    class Store:
        async def rpc(self,name,body):self.saved=body;return {'created':True}
    s=Store();asyncio.run(persist_review(EventReview(**r),s,'research:test',{URL:1,REPOST:2}))
    assert s.saved['payload']['relations']==[]


def test_identical_review_retry_has_same_key_changed_reasoning_versions():
    class Store:
        def __init__(self):self.keys=[]
        async def rpc(self,name,body):self.keys.append(body['p_key']);return {'created':True}
    r=review_payload();s=Store()
    for _ in range(2):asyncio.run(persist_review(EventReview(**r),s,'test',{URL:1}))
    assert s.keys[0]==s.keys[1]
    r['hypotheses'][0]['counterevidence']='Later source correction weakens this scenario'
    asyncio.run(persist_review(EventReview(**r),s,'test',{URL:1}))
    assert s.keys[2]!=s.keys[1]


def test_analysis_failure_preserves_collected_lead_and_partial_run():
    class Store:
        async def insert(self,*args):return {'id':1}
        async def patch(self,*args):self.saved=args[-1]
        async def rpc(self,*args):raise RuntimeError('secret-canary')
    async def ingest(*args):return {'created':True,'lead':{'id':42}}
    s=Store();r=asyncio.run(import_search_batch(SearchBatch(**batch_payload()),s,ingest,'test'))
    assert r['status']=='partial' and r['new_leads']==1
    assert r['results'][0]['lead_id']==42 and not r['analysis_receipts']
    assert 'secret-canary' not in str(s.saved)

def test_snapshot_and_run_lineage_retained_without_retry_changing_review_identity():
    class Store:
        def __init__(self):self.calls=[]
        async def rpc(self,name,body):self.calls.append(body);return {'created':True}
    s=Store();r=EventReview(**review_payload());snapshot={URL:{'retained_observation':'Source originally alleged an interruption','original_page_access':'snippet_only'}}
    asyncio.run(persist_review(r,s,'test',{URL:1},snapshot,9))
    asyncio.run(persist_review(r,s,'test',{URL:1},snapshot,10))
    assert s.calls[0]['p_key']==s.calls[1]['p_key']
    assert s.calls[0]['payload']['collection_run_id']==9
    assert s.calls[0]['payload']['evidence'][0]['reviewed_snapshot']['original_page_access']=='snippet_only'
