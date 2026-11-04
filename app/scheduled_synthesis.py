"""Conservative assessment deltas from committed evidence and analyst versions."""
import asyncio
from datetime import datetime, timezone

from app.investigation_packets import build_packet

MAX_INVESTIGATIONS = 24
MAX_EVIDENCE = 100


def compare_packet(packet, previous, assessed_ids, metadata_only_ids):
    """Only a committed assessment can change a published hypothesis."""
    versions = {a["id"] for a in packet["prior_assessments"]}
    evidence = {a["id"] for a in packet["observations"]}
    prior = set(previous["assessment_ids"]) if previous else versions
    before = set(previous["evidence_ids"]) if previous else set()
    added = evidence - before
    pending = ((added | set(previous.get("pending_review_ids", []))) if previous else evidence)
    pending &= evidence
    pending -= assessed_ids | metadata_only_ids
    hypotheses = {str(a["key"]): {
        "scenario": a["scenario"], "confidence": a["confidence"], "rationale": a["rationale"],
    } for a in packet["prior_assessments"]}
    old_hypotheses = previous.get("hypotheses", hypotheses) if previous else hypotheses
    deltas = [
        {"hypothesis_key": key, "before": old_hypotheses.get(key), "after": hypotheses.get(key)}
        for key in sorted(hypotheses.keys() | old_hypotheses.keys())
        if hypotheses.get(key) != old_hypotheses.get(key)
    ]
    changed = sorted(versions - prior)
    removed_versions = sorted(prior - versions)
    if deltas:
        outcome = "changed"
        why = "A committed review changed a hypothesis scenario, confidence or rationale."
    elif pending:
        outcome = "review_required"
        why = "Linked source observations still need a claim-level judgment."
    else:
        outcome = "no_material_change"
        why = ("Initial baseline captured. Current versions were retained." if previous is None else
               "Recorded hypothesis content is unchanged. No new linked source observation requires a claim review.")
    return {
        "cluster_id": packet["cluster_id"], "outcome": outcome, "why": why,
        "baseline_initialized": previous is None,
        "prior_assessment_ids": sorted(prior), "assessment_ids": sorted(versions),
        "changed_assessment_ids": changed, "superseded_assessment_ids": removed_versions,
        "hypotheses": hypotheses, "assessment_deltas": deltas,
        "evidence_ids": sorted(evidence), "new_evidence_ids": sorted(added) if previous else [],
        "metadata_only_new_ids": sorted(added & metadata_only_ids) if previous else [],
        "pending_review_ids": sorted(pending),
    }


async def run_scheduled_synthesis(store, run_id):
    captured_at = datetime.now(timezone.utc).isoformat()
    past = await store.rows("pipeline_runs", limit=1, before=run_id,
                            filters={"run_status":"eq.completed", "details->analysis->investigation_synthesis":"not.is.null"})
    previous = next((r["details"]["analysis"]["investigation_synthesis"]["dispositions"]
                     for r in past if r.get("details", {}).get("analysis", {}).get("investigation_synthesis")), [])
    baselines = {r["cluster_id"]: r for r in previous}
    clusters = await store.rows("clusters", limit=MAX_INVESTIGATIONS + 1,
                                filters={"canonical_key":"like.event-review:*"})
    semaphore = asyncio.Semaphore(4)

    async def investigate(cluster):
        async with semaphore:
            cluster_filter = {"cluster_id": f"eq.{cluster['id']}"}
            links, versions = await asyncio.gather(
                store.request("GET", "cluster_leads", params={
                    **cluster_filter, "select":"lead_id", "order":"lead_id.desc", "limit":str(MAX_EVIDENCE + 1),
                }),
                store.rows("assessments", limit=MAX_EVIDENCE + 1, filters=cluster_filter),
            )
            ids = [link["lead_id"] for link in links[:MAX_EVIDENCE]]
            leads = await store.rows("leads", limit=MAX_EVIDENCE, filters={
                "id":"in.(" + ",".join(map(str, ids)) + ")",
            }) if ids else []
            truncated = len(links) > MAX_EVIDENCE or len(versions) > MAX_EVIDENCE
            assessed = {e["lead_id"] for a in versions[:MAX_EVIDENCE]
                        if (a.get("analysis") or {}).get("assessment_origin") not in {"automated_triage", "historical_summary"}
                        for e in (a.get("analysis") or {}).get("evidence", []) if e.get("lead_id")}
            metadata = {lead["id"] for lead in leads if lead["source_platform"] == "mastodon_public"
                        and (lead.get("provenance") or {}).get("content_access") == "metadata_only"}
            packet = build_packet({
                "cluster": cluster, "assessments": versions[:MAX_EVIDENCE], "observations": leads,
                "limitations": "Bounded stored evidence and assessment history. Claims retain their recorded status.",
            }, [], captured_at)
            packet["open_questions"] = list(dict.fromkeys(
                question for a in packet["prior_assessments"]
                for question in (a["strengthening_conditions"], a["falsification_criteria"]) if question
            ))
            previous_packet = baselines.get(cluster["id"])
            delta = compare_packet(packet, previous_packet, assessed, metadata)
            if truncated:
                delta.update(outcome="review_required", why="Evidence or assessment history exceeded the packet limit.")
            return packet, delta

    results = await asyncio.gather(*(investigate(c) for c in clusters[:MAX_INVESTIGATIONS]))
    return {
        "version": 1, "mode": "stored_evidence_delta", "captured_at": captured_at,
        "scope_truncated": len(clusters) > MAX_INVESTIGATIONS,
        "packets": [packet for packet, _ in results],
        "dispositions": [delta for _, delta in results],
    }
