# Search-led collection

Direct source APIs are optional extensions, not a prerequisite for a useful search-led research workflow. An authorized ChatGPT search/browsing run can discover permitted public sources, inspect available originals, retain single-source unverified claims and submit a provenance-preserving batch to the existing backend. The Site reads those persistent records and assessments.

The referenced cached chat initially omitted the existing watch's execution logs/configuration. The original watch chat and saved prompt were subsequently located and inspected. Earlier reports should not be treated as proof of full X/Telegram/Reddit access. Built-in public web search, source browsing, indexed snippets and connected platform APIs are different access paths.

## Implemented path

`POST /v1/search-intake` accepts an operator-authenticated `SearchBatch`. It records queries, query language, requested window, actual search time, provider label, source URLs, source titles, retained paraphrases, inspection state and limitations. New records use the existing unverified lead ingestion, canonical URL deduplication, evidence and audit path. Collection runs retain per-query-to-lead linkage even when a URL was already collected. No second database or public Site write action is introduced.

`app/search_intake.py` defines the validated contract. `config/search-profiles.json` prepares bounded multilingual discovery profiles for Gulf/Iran, Europe/NATO/Russia, Taiwan/China and Cuba/Caribbean. `config/search-intake.schema.json` is its generated JSON schema. The profile file is **not an enabled recurring search job**.

An authorized external caller can validate a research batch with `scripts/import_search_batch.py --input <file> --dry-run`, then submit it without `--dry-run`. The operator credential belongs in that caller's secure environment (the local operator client can reuse the ignored private `.env`); never put a credential in the research payload, public Site, public source or task prompt. The Site's reader credential and scheduler credential cannot submit batches.

## Evidence boundaries

- A search hit may be collected without corroboration. It remains an attributed, unverified observation.
- Search/crawl time is not publication time or event time. Unknown publication/event timestamps remain null.
- `snippet_only` and `inaccessible` hits never become inspected source text. No media inspection is claimed by this intake path.
- Search ranking, repeated headlines and several URLs do not establish source independence or firsthand origin.
- A broad-topic search may return older, syndicated or contradictory material. Requested time windows are recorded as requests, not evidence that every result is fresh.
- Search discovery is not comprehensive direct X/Telegram/Reddit coverage and does not grant access to private/restricted material or override source-use conditions.
- The caller must review retained material and affirm permitted public access/safety. Sensitive current military positions/routes/times and private personal information stay outside retained text.

## Scheduling choices and current boundary

1. **ChatGPT research runner:** use its available web search/browsing and connected tools, then an explicitly authorized write path into `/v1/search-intake`. The generated Site plugin exposes seven reads and `record_search_batch`, a writer limited to the authenticated owner pinned by verified email and Site-scoped user ID digests. It holds a separate backend credential accepted only for search intake. Writer deployment and actual unattended execution are separate acceptance steps. Task permissions and a real unattended run must be tested. Do not infer that every scheduled task has every tool.
2. **Backend-owned search runner:** call a supported search provider (for example the OpenAI API web-search tool) from the backend with a server-side API key and an agreed budget. This is separate from ChatGPT's interactive search entitlement. No search-provider key, billing, paid model call or background search job is activated by this change.

Keep **Geopolitical Escalation Watch II** enabled and do not create a duplicate ChatGPT automation. The user authorized only an additive Site-writeback instruction; preserve its original research, cadence and intelligence-alert rules. The existing Supabase scheduler continues the implemented public adapters; it does not magically gain ChatGPT's built-in web-search tool.

Official references checked November 3, 2026: [ChatGPT scheduled tasks](https://learn.chatgpt.com/docs/automations) and [OpenAI API web search](https://developers.openai.com/api/docs/guides/tools-web-search).


## Writer authorization

The user explicitly authorized the owner writer and one unattended test. Existing Site identity checks remain. The MCP writer additionally requires the verified caller email and Site-scoped user ID to match `SIGNAL_DESK_WRITER_OWNER_EMAIL_SHA256` and `SIGNAL_DESK_WRITER_OWNER_ID_SHA256`. Its server-only `SIGNAL_DESK_SEARCH_WRITE_TOKEN` is accepted by Render only at POST `/v1/search-intake` using `SIGNAL_DESK_SEARCH_WRITER_KEY_SHA256`; it cannot use general read/write, collection or scheduler endpoints. Runtime secrets contain no Supabase credential. Caller-declared `execution_context`/`execution_reference` are retained, but are not scheduler proof: `details.unattended` remains false until an operator matches real cloud-run receipts. Public sharing remains unchanged. The initial writer deployment did not edit the existing hourly watch; the separately authorized additive writeback is documented below.


## Observed owner-writer acceptance — November 3, 2026

The user authorized the owner writer and a one-time unattended test. The public Site preserved all existing identity checks and now verifies both pinned email and Site-scoped user ID. A live credential-scope check returned HTTP403 for general lead reads and collector triggers, while search intake returned HTTP200 (run21, existing leads100/101 deduplicated). The Site build and 14 semantic/transport/authorization tests passed; the backend suite passed 53 tests. A source/build scan found no writer token in 190 checked files.

The separate cloud task **Signal Desk — one-time owner writer acceptance** completed with collection run22 and the exact acceptance batch marker. It searched English/Arabic public web sources, submitted two findings through `record_search_batch`, retained existing GeoCables lead100, created genuine Outage.ae lead129, and read both records back. The parent independently matched the stored batch, the `api:search-writer` audit event and native readback. An operator verification event marked this receipt verified. This proves the bounded unattended search→owner write→persistent readback path, not source truth, predictive performance, ongoing scheduled research or notification replacement.

The test schedule was single-occurrence and is now disabled/completed. No recurring research watch was added, and Geopolitical Escalation Watch II was not edited. Exact scheduler timing/cold-start behavior and ongoing cadence acceptance are separate from this test.


## Existing-watch writeback — November 3, 2026

The user explicitly approved appending the prepared Site-writeback instructions to the existing hourly watch. Saved prompt readback confirmed that the original research and alert instructions were preserved, with the approved addition, the same name, Hourly recurrence, Eastern time zone, same-chat destination and enabled state. No new recurring research watch was created.

A manual **Run now** of that existing task saved collection run30, batch `geowatchii-20261103T172738Z`, at 17:28:57 UTC. Five genuine public observations became leads148–152: an inaccessible original X correction and its inspected event-register derivative, an indexed UKMTO notice, an inspected aviation correction, and inspected refrigeration-cyber reporting. These are attributed unverified observations; the X/register pair is not independent corroboration. Native readback confirmed lead148's URL, query lineage, uncertain timestamp and no media inspection.

The run's caller label `scheduled` is not proof of a clock-triggered execution: this acceptance run was manually initiated through the existing task's Run now control. Its `unattended` field remains false. The earlier one-time cloud test run22 separately verifies unattended plugin write/read access. The next naturally scheduled run of the modified existing watch still needs matching research, collection, audit and readback receipts. No replacement notification transport, independent corroboration or predictive performance is implied.
