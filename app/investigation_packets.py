"""Shared evidence packet for operator and scheduled synthesis."""

def build_packet(detail, questions, captured_at):
    cluster = detail["cluster"]
    versions = sorted(detail["assessments"], key=lambda a: (
        (a.get("analysis") or {}).get("as_of") or a.get("created_at") or "", a["id"]
    ), reverse=True)
    latest = {}
    for version in versions:
        analysis = version.get("analysis") or {}
        if analysis.get("assessment_origin") in {"automated_triage", "historical_summary"}:
            continue
        key = analysis.get("hypothesis_key") or version["id"]
        latest.setdefault(key, {
            "id": version["id"], "key": key, "scenario": version["scenario"],
            "confidence": version["confidence"], "rationale": version["rationale"],
            "as_of": analysis.get("as_of"), "evidence": analysis.get("evidence", []),
            "strengthening_conditions": analysis.get("strengthening_conditions"),
            "falsification_criteria": analysis.get("falsification_criteria"),
        })
    observations = []
    for lead in detail["observations"]:
        p = lead.get("provenance") or {}
        observations.append({
            "id": lead["id"], "url": lead["source_url"], "observation": lead["observation"],
            "claim_status": lead["status"], "published_at": lead.get("original_posted_at"),
            "event_at": lead.get("claimed_event_at"), "collected_at": lead["created_at"],
            "time_note": lead.get("capture_time_note"), "content_access": p.get("content_access"),
            "origin_group": p.get("origin_group"), "originality": p.get("originality"),
            "independence": p.get("independence"),
        })
    return {
        "captured_at": captured_at, "cluster_id": cluster["id"],
        "event_key": cluster["canonical_key"].removeprefix("event-review:"),
        "title": cluster["title"], "prior_summary": cluster["scenario"],
        "open_questions": questions, "prior_assessments": list(latest.values()),
        "observations": observations, "retrieval_limits": detail.get("limitations"),
    }


