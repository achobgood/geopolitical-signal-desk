"""Check the actual Render API without printing credentials or intelligence content.

The optional --write creates clearly synthetic records; remove them through the
Supabase owner control plane after inspecting the reported IDs/marker.
"""
import argparse
import json
import os
from pathlib import Path
import uuid

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    token = os.getenv("SIGNAL_DESK_API_KEY", "")
    if not token:
        for line in Path(".env").read_text().splitlines():
            if line.startswith("SIGNAL_DESK_API_KEY="):
                token = line.split("=", 1)[1]
    assert len(token) >= 32, "Configure the independent API token securely"
    origin = "https://geopolitical-signal-desk.onrender.com"
    report = {"origin": origin, "checks": {}, "synthetic_ids": []}
    headers = {"Authorization": "Bearer " + token}
    with httpx.Client(timeout=120, follow_redirects=False, trust_env=False) as client:
        def status(name, path, expected, auth=None):
            r = client.get(origin + path, headers=auth)
            report["checks"][name] = r.status_code
            assert r.status_code == expected, f"{name}: unexpected status {r.status_code}"
            return r
        status("health", "/health", 200)
        status("missing_auth", "/v1/leads", 401)
        status("invalid_auth", "/ready", 401, {"Authorization": "Bearer wrong"})
        ready = client.get(origin + "/ready", headers=headers)
        report["checks"]["authenticated_readiness"] = ready.status_code
        assert ready.status_code in (200, 503), "Unexpected readiness response"
        if ready.status_code == 503:
            report["blocked"] = "Server database credential or connection not ready; persistent API tests not run"
        elif args.write:
            marker = "signal-desk-synthetic-" + uuid.uuid4().hex
            report["synthetic_marker"] = marker
            payload = {"source_url": "https://example.org/" + marker, "source_platform": "synthetic_test",
                "observation": "SYNTHETIC VALIDATION ONLY - not an intelligence claim",
                "provenance": {"permitted_public": True, "safety_reviewed": True,
                    "collection_limitations": "Synthetic validation record; delete after check"}}
            r = client.post(origin + "/v1/leads", headers=headers, json=payload)
            assert r.status_code == 200 and r.json()["created"] is True
            lead_id = r.json()["lead"]["id"]
            report["synthetic_ids"].append(lead_id)
            retry = client.post(origin + "/v1/leads", headers=headers, json=payload)
            assert retry.status_code == 200 and retry.json()["created"] is False
            assert retry.json()["lead"]["id"] == lead_id
            detail = client.get(origin + f"/v1/leads/{lead_id}", headers=headers)
            assert detail.status_code == 200
            assert detail.json()["lead"]["source_url"] == payload["source_url"]
            assert detail.json()["lead"]["status"] == "unverified"
            assert len(detail.json()["evidence"]) == 1
            report["checks"]["persistent_write_read_and_duplicate"] = "passed"
        status("authenticated_schema", "/openapi.json", 200, headers)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
