# Intelligence pipeline and acceptance boundary

This project is a developing intelligence workflow, not a validated geopolitical early-warning system. A working API and populated database prove neither source coverage nor predictive performance.

## Collection framework

`app/providers.py` defines the shared provider contract: capabilities, bounded fetch, normalized `LeadIn` records, seen/dropped counts and a cursor. All providers reuse the existing transactional ingestion, provenance, canonical permalink deduplication and audit path. `app/pipeline.py` owns pacing, run leases/recovery, collection history, explicit origin/topic correlations and triage. New vendors do not replace ingestion or assessment models.

The first cross-section is deliberately limited:

- Mastodon public #Iran discovery: one instance's federated public cache. Preserve source permalinks, language, post times and engagement counts without a minimum popularity threshold. Retain **no post text, account profile or media** automatically. These are inspection candidates, not firsthand evidence or event assertions. A missing boost object proves no independence. Arabic/French language metadata does not mean translation or local-community coverage is implemented.
- UN Middle East RSS: actual institutional headlines with original story links. Headline screening excludes a conservative set of sensitive terms; it is not semantic verification. An institutional report does not establish ground truth or independent confirmation.
- CISA KEV structured catalog: a cyber/infrastructure source class. Catalog additions describe known exploitation, not a regional incident or actor attribution; they do not corroborate unrelated Gulf/Yemen reports.
- The original 20 Federal Reserve observations remain **ingestion proof of concept** and are excluded from geopolitical view counts by default.

The [coverage matrix](coverage-matrix.md) inventories all original categories and documents blocked authorization, credential, licensing, freshness and history gaps. X/Telegram/Reddit are not silently replaced with mainstream RSS. Public accessibility is not sufficient permission: X intelligence use needs written approval; Telegram AI aggregation/deployment is restricted; Reddit requires approval and OAuth. No scraping fallback is implemented.

## Correlation, hypotheses and thresholds

Leads stay `unverified` regardless of collection success. Collection does not require corroboration. Exact source-origin groups create `same_origin` relations only when explicitly recorded; unknown origins remain unknown. Automated topic queues create persistent cluster membership for analyst review, **not a shared-event or escalation conclusion**. They contain ordinary explanations and a warning about reposts/unrelated events.

Automated social triage records a request for source inspection with `assessment_origin=automated_triage`. It makes no event prediction and cannot justify an alert. Analyst assessments are separate append-only records with alternatives, source limitations, strengthening conditions, baseline comparison and falsification criteria. Stable request hashes deduplicate retries; a changed assessment creates a new hypothesis version. A reviewed genuine-source demonstration is marked as an analyst assessment, with source dates distinct from ingestion time.

Alert decisions remain separate in `alert_history`. Automatic external notification delivery is **not implemented** and no alert is sent merely because a record was collected, grouped, or triaged. A future delivery transport requires an authorized channel, credentials and delivery receipts. Keep Geopolitical Escalation Watch II unchanged until collection, analysis and notification all pass replacement acceptance.

## Unattended scheduling

Render Free sleeps. The dispatcher therefore lives in the existing Supabase database using `pg_cron` and `pg_net`. A 5-minute dispatch check retries transport/cold-start failures; a UTC 15-minute run key and provider pacing permit at most one successful discovery batch per 15-minute window. A completed run is idempotent; partial/failed or abandoned runs can be retried. HTTP timeout is 240 seconds; pipeline work is bounded to 150 seconds, leaving room for cold starts.

A dedicated scheduler secret is generated **inside Supabase Vault**. It never appears in source or a tool response. Render stores only its SHA-256 digest. It authenticates only `/v1/pipeline/scheduled`; it cannot read leads or submit arbitrary analyst writes. Private dispatch receipts, `cron.job_run_details`, HTTP response records, `pipeline_runs` and per-provider collection runs distinguish scheduling, transport, ingestion and analysis outcomes. A queued HTTP request or successful cron SQL statement alone is not pipeline success.

Social discovery runs at most once per 15-minute window; institutional briefings are paced to 30 minutes and the cyber catalog to 6 hours. This is a development collector with retries, not an SLA. Free-tier project pauses/outages, upstream access changes, a bounded feed window and credential revocation remain failure modes. No paid resource or second database is introduced. Report an actual unattended `supabase_cron` pipeline run with final provider/analysis results before calling scheduling verified. That still does not validate notification replacement or early-warning accuracy.

## Historical ChatGPT reports

Earlier ChatGPT reports are derivative **historical summaries**, never additional independent evidence. Preserve report ID/export provenance, generation/as-of dates, original URLs, original publication/event uncertainty, inspection status and corrections. Recover and inspect original public source links into separate lead/evidence records, then attach the historical summary as `assessment_origin=historical_summary` with its limitations and references in analysis metadata. A summary repeating another summary or a UN repost does not increase independence. Do not mark an inaccessible source inspected, invent missing publication dates, or label an old report a fresh signal.

The available referenced chat preview contains deployment discussion rather than the earlier intelligence reports. No report was imported or fabricated. An actual export or accessible report thread is needed for a faithful import; original claims must be reviewed for prohibited personal/tactical detail before persistence. Preserve the private report itself outside the public GitHub repository.


## Observed acceptance receipt — November 3, 2026

Pipeline run 3 (`supabase_cron`) completed at 14:35:03 UTC. Cron job 1 ran without an interactive caller; pg_net request 3 returned HTTP 200. The run created four new Mastodon metadata discoveries and one UN Geneva briefing record; CISA was correctly paced after its successful six-record catalog intake. Four topic queues and labeled triage were persisted. The earlier UN News HTTP 404 and partial attempts remain in history. Reviewed historical evidence leads 82, 83 and 84 support analyst investigation 3 and assessment 10, with same-origin and related relationships. No notification was delivered. These receipts demonstrate the collection-to-triage workflow, not validated early warning.
