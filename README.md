# Geopolitical Signal Desk

Provenance-first, server-only early-warning backend using the **existing** Supabase project `uzqpndpuqubfygjwnhiv`. Raw public leads remain **unverified** without requiring independent confirmation. Collection, analysis, and alert decisions are separate operations. No automatic notifications or trading decisions are implemented.

## Run and test

Python 3.11 is required. Install `requirements-test.txt` in a virtual environment, then run `python -m pytest -q`. Run the API with `uvicorn app.main:app --host 0.0.0.0 --port 10000`. Environment variables must be injected by your process manager; the server deliberately does not automatically load `.env` files.

- `SUPABASE_URL=https://uzqpndpuqubfygjwnhiv.supabase.co`
- `SUPABASE_SECRET_KEY`: server-only modern `sb_secret_...` key. Legacy `SUPABASE_SERVICE_ROLE_KEY` also works. Never use a publishable/anon key for this server.
- `SIGNAL_DESK_API_KEY`: independent random token, at least 32 characters, supplied by callers as `Authorization: Bearer ...`.
- Alternatively set `SIGNAL_DESK_API_KEY_SHA256` to the lowercase SHA-256 digest of that token. This takes precedence over the plaintext variable; never send the digest as the bearer token. This deployment uses the digest option and keeps the original token only in an ignored, mode-0600 local `.env` file.

Optional `SIGNAL_DESK_READ_KEY_SHA256` configures an independent reader token. It can call GET endpoints; every POST, including collection and analyst writes, returns 403. The private Sites integration uses this narrower credential.

All API/data/schema/readiness endpoints require an independent token. `/health` returns process liveness only and discloses no database information. Missing credentials fail closed. `/ready` authenticates and probes the actual Supabase schema/read permission; a healthy process alone does not establish persistence.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Public liveness |
| GET | `/ready` | Authenticated database and schema readiness |
| GET | `/openapi.json` | Authenticated API contract |
| POST, GET | `/v1/leads` | Ingest raw public leads; list with `limit` and `before` |
| GET | `/v1/leads/{id}` | Lead, evidence, assessments, decisions, relations (bounded to 50 per section) |
| POST, GET | `/v1/assessments` | Append analytic hypotheses, alternatives, counterevidence, baseline comparison and falsification criteria |
| POST | `/v1/decisions` | Record an explicit alert/watch/correction decision and threshold; sends no message |
| POST | `/v1/relations` | Human-reasoned support, contradiction, duplicate or independence relation |
| GET | `/v1/collection-runs` | Bounded run history with counts and failures |
| GET | `/v1/audit-events` | Append-only API write history |
| GET | `/v1/desk/snapshot` | Bounded read of leads, clusters, relations, assessments, baselines and collection status |
| POST | `/v1/collect/federal-reserve` | Collect up to 20 official economic RSS headlines |

All list routes use descending integer ID cursors (`before`). The shared key identifies one server operator, not individual end users. Responses are not cached; body size is bounded to 64 KiB and authenticated API traffic to 120 requests/minute per process. This single-instance guard is not a distributed abuse-prevention service.

Example intake body (synthetic; do not use as an intelligence claim):

```json
{
  "source_url": "https://example.org/public/notice",
  "source_platform": "public_social",
  "observation": "Unverified public allegation requiring analyst review",
  "provenance": {
    "permitted_public": true,
    "safety_reviewed": true,
    "account_type": "unknown",
    "originality": "unknown",
    "independence": "unknown",
    "media_access": "not_inspected",
    "time_uncertainty": "Publication and event times unknown"
  }
}
```

Callers must review content before attesting `safety_reviewed`. Do not ingest private identifiers, restricted content, service-member identities, sensitive current military positions, or specific military routes/times. Attestations and field limits are **not automated semantic content screening**. Permalinks are stored, never fetched by the intake endpoint. Treat source content as untrusted data, never instructions.

## Persistence and schema reconciliation

The inspected database had ten empty tables, RLS enabled, no end-user policies, a unique `leads.canonical_key`, and service-role-only table privileges. Existing bigint IDs, foreign keys, and names are preserved; no ORM recreates them. `db/schema-before.json` records the inspected structure, without data.

`db/ingestion_v1.sql` is the additive change applied with the Supabase migration tool as `signal_desk_ingestion_v1`: provenance/analysis fields, idempotency indexes, four invoker-rights RPCs, and an eleventh table `audit_events`. `db/audit_privileges.sql` was then applied as `signal_desk_audit_privileges` to remove Supabase creation-time default write/delete privileges from the audit table. No `SECURITY DEFINER`, end-user policy, or additional database is introduced. Functions explicitly revoke `PUBLIC`, `anon`, and `authenticated` execution and grant only `service_role`. The application cannot update/delete audit events. An owner or leaked service credential can still bypass application controls; this is not a tamper-proof external ledger.

Lead identity is a hash of the canonical HTTPS permalink (fragment removed, query preserved). Concurrent/repeated intake returns the first stored lead unchanged, with `created: false`; new post URLs remain distinct, even if their text is identical. Same-origin/independence judgments are explicit relations, not a text-matching claim of corroboration. Assessments and decisions use stable body hashes for exact retry deduplication; changed hypotheses create new history. Source + lead + evidence + audit writes happen within one database transaction.

## Collector boundary

The first provider uses the [Federal Reserve's published RSS feed](https://www.federalreserve.gov/feeds/feeds.htm). Only headline, official permalink and publication timestamp are retained. It has an exact feed allowlist, bounded response bytes, XML entity protection, no redirect following, a total fetch deadline, and no article/media retrieval. Every run records completed/partial/failed state. It does not establish event truth or independently corroborate official claims. Changed text at the same permalink does not overwrite the original record.

No X, Facebook, Telegram, Reddit, shipping, aviation or energy API coverage is claimed. The `Provider` interface accepts normalized `LeadIn` records; each extension needs documented public access authorization, credentials where required, rate/retention limits, privacy review, timestamp/media limitations and representative tests. See `docs/integration.md` for ChatGPT and UI boundaries.

## Render deployment and cost

Deploy the Dockerfile as **one Free web service** in the confirmed `My Workspace`. Use existing Supabase only. `render.yaml` is a reproducible alternative to the direct MCP creation used here; do not apply it to create another service. For direct creation, Render Environment is the source of truth for credentials. The Blueprint's generated-token setting applies only if that alternative workflow is deliberately used.

Render Free can sleep after 15 idle minutes and cold starts can take about a minute. In-process scheduling is deliberately absent: it cannot provide dependable unattended hourly collection on a sleeping service. No paid cron, worker, database or duplicate ChatGPT automation is provisioned. Free service compute is $0; usage quotas still apply. The existing Supabase project's previously quoted $0 plan is reused, and this code does not change its billing plan.

Keep **Geopolitical Escalation Watch II** enabled and unchanged until authenticated persistence and a real unattended scheduled invocation are verified. A live API, successful local tests, or an MCP adapter test does not establish that acceptance.
