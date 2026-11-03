"""Evidence-bound research reviews. Reasoning is supplied by the authorized researcher.

The service validates, links, versions and audits that reasoning; it does not
turn text similarity, domain counts or search rankings into corroboration.
"""
from datetime import datetime
import hashlib
import json
from typing import Literal

from pydantic import Field, field_validator, model_validator
from app.models import StrictModel, public_url


def aware(value):
    if value is not None and value.utcoffset() is None:
        raise ValueError("Include timezone and preserve uncertainty separately")
    return value


class EvidenceUse(StrictModel):
    source_url: str = Field(max_length=2000)
    origin_group: str = Field(default="unknown", max_length=150)
    origin_basis: str = Field(min_length=1, max_length=1200)
    limitations: str = Field(min_length=1, max_length=1200)
    _url = field_validator("source_url")(public_url)


class EvidenceRelation(StrictModel):
    source_url: str
    other_source_url: str
    relation_type: Literal["same_origin", "possibly_independent", "supports", "contradicts", "related"]
    rationale: str = Field(min_length=1, max_length=1500)
    _urls = field_validator("source_url", "other_source_url")(public_url)


class Hypothesis(StrictModel):
    key: str = Field(min_length=3, max_length=100, pattern=r"^[a-zA-Z0-9:_-]+$")
    scenario: str = Field(min_length=1, max_length=2000)
    rationale: str = Field(min_length=1, max_length=4000)
    confidence: Literal["low", "medium", "high"]
    supporting_urls: list[str] = Field(default_factory=list, max_length=20)
    contradicting_urls: list[str] = Field(default_factory=list, max_length=20)
    alternative_explanations: str = Field(min_length=1, max_length=2500)
    counterevidence: str = Field(min_length=1, max_length=2500)
    strengthening_conditions: str = Field(min_length=1, max_length=2000)
    falsification_criteria: str = Field(min_length=1, max_length=2000)
    conditional_market_hypothesis: str = Field(default="", max_length=2000)

    @field_validator("supporting_urls", "contradicting_urls")
    @classmethod
    def urls(cls, values):
        return list(dict.fromkeys(public_url(v) for v in values))


class ReviewDecision(StrictModel):
    decision: Literal["alert", "watch", "no_alert", "correction", "retraction"]
    rationale: str = Field(min_length=1, max_length=2500)
    notification_threshold: str = Field(min_length=1, max_length=2000)


class ReportProvenance(StrictModel):
    report_id: str = Field(min_length=1, max_length=200)
    report_as_of: datetime
    limitations: str = Field(min_length=1, max_length=1500)
    _aware = field_validator("report_as_of")(aware)


class EventReview(StrictModel):
    review_id: str = Field(min_length=3, max_length=100, pattern=r"^[a-zA-Z0-9:_-]+$")
    event_key: str = Field(min_length=3, max_length=150, pattern=r"^[a-zA-Z0-9:_-]+$")
    title: str = Field(min_length=1, max_length=300)
    broad_region: str = Field(min_length=1, max_length=100)
    as_of: datetime
    period_start: datetime
    period_end: datetime
    historical_backfill: bool = False
    report_provenance: ReportProvenance | None = None
    summary: str = Field(min_length=1, max_length=2500)
    evidence: list[EvidenceUse] = Field(min_length=1, max_length=20)
    relations: list[EvidenceRelation] = Field(default_factory=list, max_length=30)
    hypotheses: list[Hypothesis] = Field(min_length=1, max_length=3)
    independence_assessment: str = Field(min_length=1, max_length=2500)
    baseline_comparison: str = Field(min_length=1, max_length=2500)
    analysis_threshold: str = Field(min_length=1, max_length=1500)
    decision: ReviewDecision
    _aware = field_validator("as_of", "period_start", "period_end")(aware)

    @model_validator(mode="after")
    def references(self):
        if self.period_start > self.period_end or self.period_end > self.as_of:
            raise ValueError("Review period must end no later than as-of time")
        if self.report_provenance and not self.historical_backfill:
            raise ValueError("Imported report summaries must be historical backfill")
        urls = [e.source_url for e in self.evidence]
        if len(set(urls)) != len(urls):
            raise ValueError("Evidence URLs must be distinct")
        keys = [h.key for h in self.hypotheses]
        if len(set(keys)) != len(keys):
            raise ValueError("Hypothesis keys must be distinct")
        for h in self.hypotheses:
            if not h.supporting_urls and not h.contradicting_urls:
                raise ValueError("Each hypothesis needs an explicit evidence reference")
            if set(h.supporting_urls) & set(h.contradicting_urls):
                raise ValueError("Use separate hypotheses for ambiguous evidence roles")
            if not set(h.supporting_urls + h.contradicting_urls) <= set(urls):
                raise ValueError("Hypothesis references evidence outside this review")
        for r in self.relations:
            if r.source_url == r.other_source_url or not {r.source_url, r.other_source_url} <= set(urls):
                raise ValueError("Relation must link two evidence records in this review")
        return self


async def persist_review(review, store, actor, resolved=None, evidence_snapshots=None, collection_run_id=None):
    """Resolve canonical URLs, then commit the full review in one DB transaction."""
    resolved = dict(resolved or {})
    snapshots = dict(evidence_snapshots or {})
    for e in review.evidence:
        if e.source_url not in resolved:
            url_key = "url-v1:" + hashlib.sha256(json.dumps({"url": e.source_url}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
            rows = await store.rows("leads", limit=1, filters={"canonical_key": "eq." + url_key})
            if not rows:
                raise ValueError("Collect evidence before assessing it")
            resolved[e.source_url] = rows[0]["id"]
            snapshots[e.source_url] = {"retained_observation": rows[0]["observation"],
                "provenance": rows[0].get("provenance", {}),
                "publication_time_note": rows[0].get("capture_time_note", "Unknown")}
    payload = review.model_dump(mode="json")
    for e in payload["evidence"]:
        e["lead_id"] = resolved[e["source_url"]]
        e["reviewed_snapshot"] = snapshots.get(e["source_url"], {"limitations": "No snapshot supplied"})
    for h in payload["hypotheses"]:
        h["supporting_lead_ids"] = [resolved[u] for u in h["supporting_urls"]]
        h["contradicting_lead_ids"] = [resolved[u] for u in h["contradicting_urls"]]
    relations = []
    # Only explicitly documented shared origins are linked. A different domain
    # or origin label is never an automatic independent confirmation.
    groups = {}
    for e in review.evidence:
        if e.origin_group.lower() not in ("unknown", "unspecified", ""):
            groups.setdefault(e.origin_group, []).append(e)
    for origin, members in groups.items():
        for member in members[1:]:
            a, b = sorted([resolved[members[0].source_url], resolved[member.source_url]])
            relations.append({"lead_id": a, "other_lead_id": b, "relation_type": "same_origin",
                              "rationale": f"Researcher-documented origin {origin}: {members[0].origin_basis}; {member.origin_basis}"[:3000]})
    for r in review.relations:
        relations.append({"lead_id": resolved[r.source_url], "other_lead_id": resolved[r.other_source_url],
                          "relation_type": r.relation_type, "rationale": r.rationale})
    payload["relations"] = relations
    payload["reported_origin_groups"] = sorted(groups)
    payload["independent_corroboration_count"] = None
    request_key = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    # A retried collection gets a new run receipt, but must not create another
    # identical hypothesis version solely because that receipt ID changed.
    payload["collection_run_id"] = collection_run_id
    return await store.rpc("signal_review_v1", {"payload": payload, "p_key": request_key, "p_actor": actor})
