"""Read the live desk and freeze compact synthesis inputs for selected investigations."""
import argparse
from datetime import datetime, timezone
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.investigation_packets import build_packet

ORIGIN = "https://geopolitical-signal-desk.onrender.com"


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
