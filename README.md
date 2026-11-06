# Geopolitical Signal Desk

Provenance-first, server-only early-warning backend using the **existing** Supabase project `uzqpndpuqubfygjwnhiv`. Raw public leads remain **unverified** without requiring independent confirmation. Collection, analysis, and alert decisions are separate operations. No automatic notifications or trading decisions are implemented.

## Current intelligence milestone

A search-led intake path now accepts reviewed research from ChatGPT or another authorized search runner. It preserves the queries and inspection state, reuses persistent ingestion, and feeds the existing Site. See [search-led collection](docs/search-led-collection.md). This does not activate background web search or require a Twitter API key. Recurring search writeback remains a separate acceptance step.

The multi-source framework is implemented; it is **not a validated geopolitical prediction system**. Active adapters cover public Mastodon metadata discovery, UN Geneva geopolitical briefing metadata, and CISA KEV structured catalog data. These are separate source classes; a cyber catalog entry does not corroborate a geopolitical event. Original X/Telegram/Reddit/Meta, local-community, maritime/aviation and energy/market requirements are inventoried with exact access gaps in [the coverage matrix](docs/coverage-matrix.md). Restricted platforms are not scraped.

The pipeline records persistent lead history, cursors, failure/pacing receipts, same-origin relationships, topic-review cluster memberships and labeled automated triage. Topic grouping is not independent corroboration. An inspected historical Yemen investigation has three original evidence links, explicit source-dependence notes, a saved low-confidence analyst assessment and a watch decision; it is not a fresh escalation warning. The original Federal Reserve observations remain ingestion POC data.

Evidence-bound [research reviews](docs/research-reviews.md) now link source snapshots, documented common origins, competing hypotheses and separate alert decisions in one service-only transaction. Identical retries deduplicate; changed reasoning appends versions; older backfill cannot replace a newer current scenario. Reasoning comes from the authorized research caller, not an autonomous backend model. Historical report imports retain report provenance and add no independent evidence.

Unattended collection runs outside sleeping Render, using the **existing Supabase** pg_cron/pg_net and a narrow Vault-backed credential. The dispatcher checks every five minutes, retries partial/cold-start failures, and uses 15-minute idempotent discovery windows. Provider pacing is separate. A genuine unattended run completed on November 3 at 14:35 UTC (pipeline run 3), adding four social discovery records and one official briefing record with persistent triage. This establishes scheduled collection/triage, not notification delivery or predictive accuracy. See [pipeline boundaries](docs/pipeline-boundary.md). The existing ChatGPT watch remains enabled; on November 3 the user authorized an additive Site-writeback instruction. Its original research/alert rules and hourly schedule are preserved.

The [command-center Site](https://geopolitical-signal-command.boldbaron8.chatgpt.site) uses a server-side read-only credential and Sites-owned authentication. The user has made the Site public. Seven MCP tools read the same store; a separately pinned owner-only writer submits reviewed search batches. Other signed-in visitors retain reads. The writer uses a separate search-intake-only backend credential and cannot make other API calls. No browser credential or additional intelligence database is used. External notification delivery is not enabled.

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

Keep **Geopolitical Escalation Watch II** enabled. The user authorized appending Site writeback without changing its research, hourly schedule or alert rules. A manual Run now saved five real-source observations in collection run30; a clock-triggered run of this modified watch remains a separate acceptance step. The prior one-time cloud writer test passed; neither proves notification replacement.


## Market Intelligence (additive feature)

Market Intelligence is an evidence-led research section within the existing
Signal Desk ChatGPT Site, using dedicated source records and historical cutoff
validation. Render retains /v1/market/snapshot for authorized readers and a
bounded public projection; its legacy /market UI redirects to the unified Site.
The public readout intentionally contains only public market research. See [Market Intelligence design, rollout and coverage](docs/market-intelligence.md).

The market model tracks AI credit financing, returns on capex, permits/regulation,
earnings, rates, broad market confirmation and geopolitical spillover.
Historical replay is clearly labeled **retrospective**: earlier publication
does not mean this platform was running or alerted at the time.
Price feeds, credit feeds, autonomous market research and trade execution
are **not** currently connected. The original geopolitical tools, alerts and
Site remain unaffected.

## Prospective next-session research

The existing Render FastAPI backend now owns append-only SPY (S&P 500 proxy)
and QQQ (Nasdaq-100 proxy, not Nasdaq Composite) forecast issuance and outcome
measurement. Brokerage execution remains disabled. The existing ChatGPT Site is the unified frontend; `/market` redirects to its
Market intelligence section after live authentication and public-read parity checks.

- `GET /v1/market/forecasts/public`: sanitized research forecasts and current source/lock status.
- Owner/reader `GET /v1/market/forecasts`, `/evaluations`, `/performance`, `/runs`;
  the full paths use `/v1/market/forecasts` as their prefix.
- Owner `POST /v1/market/forecasts/run` with a unique `run_key`; scheduler-only
  `POST /v1/market/forecasts/scheduled` reuses the existing Vault credential.
- Owner `POST /v1/market/experiments/run` with `study_id` and `instrument`.
  Inputs, labels, time splits and code hashes come from server-owned immutable
  records. No caller-submitted performance or prices are accepted.
- Protected `GET /v1/market/experiments` pages through every registered
  hypothesis, parameter trial, result, rejection reason, freeze and final decision.

SQL timestamps govern the preceding trading day 16:30–19:00 Eastern issuance
window. Source requests that finish after the lock cannot create an earlier
forecast. Initial live model `momentum-1-research-v1` is explicitly an unselected,
uncalibrated research candidate. Missing consolidated Alpaca history records an
ABSTAIN with no invented reference price. IEX partial exchange data cannot
establish the required consolidated daily close. No credentials are inferred.

After each target session's 16:30 Eastern publication buffer, an independent
cycle fetches actual later consolidated history. PostgreSQL recomputes the exact
reference-to-target outcome. Missing target dates and revised reference/target
closes produce unresolved states. Recent observed forecasts are rechecked for
revisions for ten calendar days; older source revisions require an explicit
owner re-evaluation. Price changes exclude dividends and are not trading returns.

The fixed A–F experiment search records all configurations before execution,
trains chronologically, and freezes the development winner before a single final
holdout. Holdout sessions are reserved once per instrument, including across
renamed datasets and overlapping windows. A rejected holdout cannot promote a
runner-up. At least 100 held-out directional observations, both classes,
coverage, balanced accuracy, and positive paired block-bootstrap improvement
against always-up, momentum and training-majority are required. Full-cohort
comparisons count model abstentions as incorrect so selective abstention cannot
hide a baseline loss. Probabilities remain uncalibrated. An inadequate cohort
records a blocked study with zero tested hypotheses and no invented results.

The calendar is bounded to the verified NYSE 2026–2028 published schedule;
unplanned exchange closures remain an external condition requiring a calendar
update. Forecast/performance/experiment ledgers have RLS, mutation-denying
triggers, and no direct end-user privileges. Server-only write RPC wrappers call
narrow private functions; the service key cannot directly insert ledger rows.

Apply the versioned migrations in order. Activate the single
`market-forecast-cycle` job only after the live scheduled route passes checks.
Existing geopolitical, market reference and watch schedules remain unchanged.

**The model produces reproducible forecasts. Predictive performance is not yet established.**
This is a capability statement, not evidence that a live directional forecast
or its later outcome has already occurred. Actual counts come from the ledgers.

Run `python scripts/verify_forecast_database.py --isolated-local-only` with local PostgreSQL 16 to repeat SQL invariant checks. It accepts no remote database address and creates no production fixtures. The existing baseline migrations were historically applied through the connected Supabase tool; new versioned files mirror their exact hosted migration versions.

## Unified production frontend

Use [Signal Desk](https://geopolitical-signal-command.boldbaron8.chatgpt.site/#markets) for both geopolitical and market intelligence. The legacy Render `/market` route redirects to that same Site; it accepts no caller-selected redirect or forwarded credentials. The previous HTML is retained as versioned source, not served as another dashboard. FastAPI, collection, forecasting, evaluation, authorization, and future private trading infrastructure remain on Render; Supabase remains authoritative. The Site uses fixed read-only server requests and never receives private brokerage/portfolio execution capabilities.

Forecast issuance waits until 16:30 Eastern so the Basic SIP request end (actual collection time minus 16 minutes) is safely later than the regular closing print. Before that time, current-session price captures and issuance are rejected even if a provider returns a same-date partial daily bar.

The buffer follows the documented delayed-history boundary and daily-bar aggregation semantics in [Alpaca’s official market-data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq). Publication/correction timing remains explicit in immutable source receipts.
