"""Operator-only search intake client. Never prints credentials or source text."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from app.search_intake import SearchBatch


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--input',required=True)
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    batch=SearchBatch.model_validate_json(Path(args.input).read_text())
    if args.dry_run:
        print(json.dumps({'validated':True,'batch_id':batch.batch_id,'queries':len(batch.queries),'hits':len(batch.hits),'written':False}));return
    key=os.getenv('SIGNAL_DESK_API_KEY','')
    local=Path(__file__).resolve().parents[1]/'.env'
    if not key and local.exists():
        key=next((x.split('=',1)[1] for x in local.read_text().splitlines() if x.startswith('SIGNAL_DESK_API_KEY=')),'')
    if len(key)<32:raise SystemExit('Configure the operator API credential securely; do not put it in the input file')
    try:
        with httpx.Client(timeout=120,follow_redirects=False,trust_env=False) as client:
            response=client.post('https://geopolitical-signal-desk.onrender.com/v1/search-intake',headers={'Authorization':'Bearer '+key},json=batch.model_dump(mode='json'))
        if response.status_code!=200:raise SystemExit(f'Search intake failed with HTTP {response.status_code}; inspect collection history before retrying')
        print(json.dumps(response.json(),indent=2))
    except httpx.HTTPError:
        raise SystemExit('Search intake connection failed; credentials and payload were not logged')


if __name__=='__main__':main()
