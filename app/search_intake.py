"""Receive provenance-preserving research from an authorized external search runner.

This module does not run a search engine or claim social API access. ChatGPT,
another approved search tool, or a later licensed provider performs discovery.
"""
from datetime import datetime, timezone
from typing import Literal
from pydantic import Field, field_validator, model_validator
from app.models import StrictModel, LeadIn, Provenance, public_url
from app.analysis import EventReview, persist_review, aware


class SearchQuery(StrictModel):
    id: str = Field(min_length=1,max_length=80,pattern=r"^[a-zA-Z0-9_-]+$")
    query: str = Field(min_length=1,max_length=800)
    language: str = Field(default="und",max_length=20)
    requested_window: str = Field(default="Not specified",max_length=200)
    method: Literal["web_search", "direct_url", "public_api", "mixed_recovery"] = "web_search"


class SearchHit(StrictModel):
    query_id: str = Field(min_length=1,max_length=80)
    source_url: str = Field(max_length=2000)
    source_title: str = Field(min_length=1,max_length=250)
    observation: str = Field(min_length=1,max_length=2000)
    broad_region: str = Field(default="unspecified",max_length=100)
    source_kind: Literal["official","public_social","news","aggregate","unknown"] = "unknown"
    account_type: Literal["official","journalist","organization","eyewitness","aggregator","unknown"] = "unknown"
    original_page_access: Literal["snippet_only","inaccessible","text_inspected"]
    publication_time_note: str = Field(default="Publication time unknown; discovery time is separate",max_length=500)
    original_posted_at: datetime | None = None
    claimed_event_at: datetime | None = None
    limitations: str = Field(min_length=1,max_length=800)
    permitted_public: Literal[True]
    safety_reviewed: Literal[True]
    _url=field_validator("source_url")(public_url)
    _aware=field_validator("original_posted_at", "claimed_event_at")(aware)


class SearchBatch(StrictModel):
    batch_id: str = Field(min_length=3,max_length=100,pattern=r"^[a-zA-Z0-9:_-]+$")
    provider: str = Field(min_length=1,max_length=80)
    searched_at: datetime
    search_time_precision: Literal["exact", "as_of_checkpoint"] = "exact"
    search_time_note: str = Field(default="", max_length=600)
    queries: list[SearchQuery] = Field(min_length=1,max_length=8)
    hits: list[SearchHit] = Field(default_factory=list,max_length=20)
    run_limitations: str = Field(min_length=1,max_length=1500)
    execution_context: Literal["interactive","unattended_test","scheduled"] = "interactive"
    execution_reference: str = Field(default="",max_length=200)
    purpose: Literal["monitoring", "historical_backfill"] = "monitoring"
    analyses: list[EventReview] = Field(default_factory=list, max_length=4)

    @field_validator("searched_at")
    @classmethod
    def aware(cls,value):
        if value.utcoffset() is None:raise ValueError("Search time requires a timezone")
        return value

    @model_validator(mode="after")
    def query_references(self):
        ids=[q.id for q in self.queries]
        if len(set(ids))!=len(ids):raise ValueError("Query IDs must be distinct")
        if self.search_time_precision == "as_of_checkpoint" and not self.search_time_note:
            raise ValueError("Explain unknown retrieval timing instead of inventing an exact search time")
        if any(h.query_id not in ids for h in self.hits):raise ValueError("Unknown query reference")
        urls={h.source_url for h in self.hits}
        if any(not {e.source_url for e in review.evidence} <= urls for review in self.analyses):
            raise ValueError("Search writer may assess only evidence submitted in this batch")
        if any(review.as_of > self.searched_at for review in self.analyses):
            raise ValueError("Review as-of cannot be later than research time")
        return self


def normalize_hit(batch: SearchBatch,hit: SearchHit) -> LeadIn:
    query=next(q for q in batch.queries if q.id==hit.query_id)
    time_label="Search time" if batch.search_time_precision=="exact" else "Research as-of checkpoint (capture times unknown)"
    note=(f"Discovery: {batch.provider}. {time_label}: {batch.searched_at.isoformat()}. {batch.search_time_note} "
          f"Method: {query.method}. Query/recovery request: {query.query}. Requested window: {query.requested_window}. "
          f"Original page access: {hit.original_page_access}. {hit.limitations}")
    return LeadIn(source_url=hit.source_url,source_platform="public_web_search",source_kind=hit.source_kind,
        observation=hit.observation,broad_region=hit.broad_region,
        original_posted_at=hit.original_posted_at,claimed_event_at=hit.claimed_event_at,
        capture_time_note=hit.publication_time_note,
        observed_vs_inferred=("Original page text inspected; observation remains an attributed, unverified source report." if hit.original_page_access=="text_inspected" else
                             "Search result/snippet inspected only. Original page was not inspected; the result is a discovery lead, not verification."),
        reliability_note="Search ranking, crawl time and repetition do not establish truth, publication time or independence.",
        source_independence_note="Unknown; trace the original claim and any syndication before counting independent sources.",
        provenance=Provenance(permitted_public=True,safety_reviewed=True,account_type=hit.account_type,
            language_code=query.language,topic="Search discovery: "+hit.broad_region,
            content_access="text_inspected" if hit.original_page_access=="text_inspected" else "metadata_only",
            media_access="not_inspected",time_uncertainty=hit.publication_time_note,
            safety_review_method="Authorized search caller reviewed retained public paraphrase; no media inspection claimed",
            collection_limitations=note[:2000]))


async def import_search_batch(batch,store,ingest,actor):
    details={"batch_id":batch.batch_id,"search_provider":batch.provider,"searched_at":batch.searched_at.isoformat(),
             "search_time_precision":batch.search_time_precision,"search_time_note":batch.search_time_note,
             "queries":[q.model_dump() for q in batch.queries],"results":[],"unattended":False,
             "caller_execution_context":batch.execution_context,"execution_reference":batch.execution_reference,
             "purpose":batch.purpose,"analysis_receipts":[],
             "unattended_verification":"Not verified; caller context alone is not scheduler proof",
             "runner_note":"Caller-submitted research. Background search is not activated by this endpoint."}
    run=await store.insert("collection_runs",{"collector":"external_web_search","access_limitations":batch.run_limitations,"details":details})
    new=0;status="completed";error=""
    try:
        for hit in batch.hits:
            r=await ingest(normalize_hit(batch,hit),store,actor)
            new+=int(r["created"])
            details["results"].append({"lead_id":r["lead"]["id"],"query_id":hit.query_id,
                                      "source_url":hit.source_url,"source_title":hit.source_title,
                                      "retained_observation":hit.observation,"publication_time_note":hit.publication_time_note,
                                      "original_page_access":hit.original_page_access,"created":r["created"]})
        resolved={r["source_url"]:r["lead_id"] for r in details["results"]}
        snapshots={r["source_url"]:{k:r[k] for k in ("retained_observation","publication_time_note","original_page_access")} for r in details["results"]}
        for review in batch.analyses:
            receipt=await persist_review(review,store,actor,resolved,snapshots,run["id"])
            details["analysis_receipts"].append({"review_id":review.review_id,**receipt})
    except Exception:
        status="partial" if details["results"] else "failed"
        error="Search intake incomplete; search tool credentials/content are not logged"
    finish={"finished_at":datetime.now(timezone.utc).isoformat(),"run_status":status,
            "observations_seen":len(batch.hits),"new_leads":new,"error_summary":error,"details":details}
    await store.patch("collection_runs",run["id"],finish)
    return {"run_id":run["id"],"status":status,"new_leads":new,"results":details["results"],"analysis_receipts":details["analysis_receipts"],"background_search_enabled":False}
