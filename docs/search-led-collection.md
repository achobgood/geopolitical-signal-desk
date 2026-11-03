# Search-led collection

Direct source APIs are optional extensions, not a prerequisite for a useful search-led research workflow. An authorized ChatGPT search/browsing run can discover permitted public sources, inspect available originals, retain single-source unverified claims and submit a provenance-preserving batch to the existing backend. The Site reads those persistent records and assessments.

The existing ChatGPT watch's exact execution logs/configuration were not available in the referenced cached chat. Earlier reports should not be treated as proof of full X/Telegram/Reddit access. Built-in public web search, source browsing, indexed snippets and connected platform APIs are different access paths.

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

1. **ChatGPT research runner:** use its available web search/browsing and connected tools, then an explicitly authorized write path into `/v1/search-intake`. The generated Site plugin currently exposes reads only; a narrowly scoped owner-authorized writer still needs implementation/acceptance before scheduled writeback. Task permissions and a real unattended run must be tested. Do not infer that every scheduled task has every tool.
2. **Backend-owned search runner:** call a supported search provider (for example the OpenAI API web-search tool) from the backend with a server-side API key and an agreed budget. This is separate from ChatGPT's interactive search entitlement. No search-provider key, billing, paid model call or background search job is activated by this change.

Keep **Geopolitical Escalation Watch II** unchanged and do not create a duplicate ChatGPT automation. The existing Supabase scheduler continues the implemented public adapters; it does not magically gain ChatGPT's built-in web-search tool.

Official references checked November 3, 2026: [ChatGPT scheduled tasks](https://learn.chatgpt.com/docs/automations) and [OpenAI API web search](https://developers.openai.com/api/docs/guides/tools-web-search).
