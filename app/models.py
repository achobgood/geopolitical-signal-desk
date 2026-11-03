"""Bounded intake contracts. Raw leads never require corroboration."""
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
import ipaddress

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def public_url(value: str) -> str:
    u = urlsplit(value)
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        raise ValueError("A public HTTPS permalink without credentials is required")
    host = u.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise ValueError("Private addresses are not permitted")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise ValueError("Private addresses are not permitted")
    except ValueError as exc:
        if "Private" in str(exc):
            raise
    if u.port not in (None, 443):
        raise ValueError("Only HTTPS port 443 is permitted")
    # Preserve query parameters: they may identify the actual source post.
    return urlunsplit(("https", host, u.path or "/", u.query, ""))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Provenance(StrictModel):
    account_type: Literal["official", "journalist", "organization", "eyewitness", "aggregator", "unknown"] = "unknown"
    language_code: str = Field(default="und", max_length=20)
    originality: Literal["original", "repost", "unknown"] = "unknown"
    independence: Literal["unknown", "same_origin", "possibly_independent"] = "unknown"
    media_access: Literal["not_inspected", "metadata_only", "inaccessible", "inspected"] = "not_inspected"
    media_inspection_note: str = Field(default="", max_length=2000)
    time_uncertainty: str = Field(default="Event time unknown", max_length=1000)
    collection_limitations: str = Field(default="", max_length=2000)
    permitted_public: Literal[True]
    safety_reviewed: Literal[True]

    @model_validator(mode="after")
    def inspected_needs_note(self):
        if self.media_access == "inspected" and not self.media_inspection_note:
            raise ValueError("Describe what media was actually inspected")
        return self


class LeadIn(StrictModel):
    source_url: str = Field(min_length=10, max_length=2000)
    source_platform: str = Field(min_length=1, max_length=40)
    observation: str = Field(min_length=1, max_length=8000)
    source_kind: Literal["official", "public_social", "news", "aggregate", "unknown"] = "unknown"
    broad_region: str = Field(default="unspecified", max_length=100)
    original_posted_at: datetime | None = None
    claimed_event_at: datetime | None = None
    capture_time_note: str = Field(default="", max_length=1000)
    evidence_type: Literal["text", "image", "video", "audio", "mixed"] = "text"
    observed_vs_inferred: str = Field(default="", max_length=2000)
    alternative_explanations: str = Field(default="", max_length=2000)
    strengthening_evidence: str = Field(default="", max_length=2000)
    falsifying_evidence: str = Field(default="", max_length=2000)
    reliability_note: str = Field(default="", max_length=2000)
    source_independence_note: str = Field(default="Unknown; reposts are not independent confirmation", max_length=2000)
    provenance: Provenance

    _url = field_validator("source_url")(public_url)

    @field_validator("original_posted_at", "claimed_event_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("Include timezone; describe uncertainty separately")
        return value


class AssessmentIn(StrictModel):
    lead_id: int = Field(gt=0)
    scenario: str = Field(min_length=1, max_length=2000)
    rationale: str = Field(min_length=1, max_length=8000)
    confidence: Literal["low", "medium", "high"] = "low"
    counterevidence: str = Field(default="", max_length=4000)
    market_hypothesis: str = Field(default="", max_length=2000)
    alternative_explanations: str = Field(min_length=1, max_length=4000)
    corroborating_lead_ids: list[int] = Field(default_factory=list, max_length=30)
    contradicting_lead_ids: list[int] = Field(default_factory=list, max_length=30)
    baseline_comparison: str = Field(default="Not established", max_length=3000)
    falsification_criteria: str = Field(min_length=1, max_length=3000)
    analysis_threshold: str = Field(min_length=1, max_length=2000)


class DecisionIn(StrictModel):
    lead_id: int = Field(gt=0)
    decision: Literal["alert", "watch", "no_alert", "correction", "retraction"]
    rationale: str = Field(min_length=1, max_length=4000)
    confidence: Literal["low", "medium", "high"] = "low"
    market_scenario: str = Field(default="", max_length=2000)
    notification_threshold: str = Field(min_length=1, max_length=2000)


class RelationIn(StrictModel):
    lead_id: int = Field(gt=0)
    other_lead_id: int = Field(gt=0)
    relation_type: Literal["same_origin", "duplicate", "possibly_independent", "supports", "contradicts", "related"]
    rationale: str = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def different_leads(self):
        if self.lead_id == self.other_lead_id:
            raise ValueError("Relate two different leads")
        return self
