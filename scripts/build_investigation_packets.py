"""Read the live desk and freeze compact synthesis inputs for selected investigations."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import httpx

ORIGIN = "https://geopolitical-signal-desk.onrender.com"


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True, help="JSON object of cluster IDs to open question lists")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    token = os.getenv("SIGNAL_DESK_API_KEY")
    if not token:
        env = Path(__file__).resolve().parents[1] / ".env"
        token = next((line.partition("=")[2] for line in env.read_text().splitlines()
                      if line.startswith("SIGNAL_DESK_API_KEY=")), "")
    if not token:
        parser.error("Configure the existing operator API credential")
    cases = json.loads(args.cases.read_text())
    packets = []
    with httpx.Client(base_url=ORIGIN, headers={"Authorization": "Bearer " + token},
                      timeout=45, trust_env=False, follow_redirects=False) as client:
        for cluster_id, questions in cases.items():
            response = client.get(f"/v1/clusters/{int(cluster_id)}")
            response.raise_for_status()
            packets.append(build_packet(response.json(), questions, datetime.now(timezone.utc).isoformat()))
    args.output.write_text(json.dumps(packets, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
