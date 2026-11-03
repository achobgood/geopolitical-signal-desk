# ChatGPT and Site integration

## Live Site and native MCP

The existing [Signal Desk Site](https://geopolitical-signal-command.boldbaron8.chatgpt.site) uses Sites-managed ChatGPT authentication and the existing Render API/Supabase database. The user changed Site sharing to public. Data views still require sign-in; other signed-in visitors can read the retained intelligence. No D1/R2 intelligence store or second backend was created.

The Site plugin exposes seven read tools: `get_situation_overview`, `list_recent_leads`, `investigate_lead`, `get_event_cluster`, `find_related_observations`, `get_market_scenarios`, `get_collection_status`. Native reads are verified. If a chat has a stale tool catalog, select the installed Geopolitical Signal Desk plugin in a fresh chat.

The user explicitly authorized `record_search_batch`, an owner-only writer. Existing identity checks remain; verified email and Site-scoped user ID must both match server-configured owner digests. Other visitors cannot write. `SIGNAL_DESK_SEARCH_WRITE_TOKEN` stays in Sites runtime secrets and authenticates only POST `/v1/search-intake`; Render stores its `SIGNAL_DESK_SEARCH_WRITER_KEY_SHA256` digest. The token cannot read leads, call general analyst-write endpoints or trigger collection. No credential is exposed to browser code or task prompts.

The writer stores reviewed public search batches through existing unverified lead ingestion and provenance/dedup/audit logic. Its write annotation is explicit. Search snippets, source timestamps and unknown independence remain separate from inspected originals and established facts.

## Observed unattended test

A single-occurrence Site-linked cloud task completed the search → owner writer → persistent readback path. Collection run22 retained existing lead100 and created genuine lead129; the parent independently matched the completed run chat, `api:search-writer` audit record and native readback. The test is now disabled/completed. Its operator-verified receipt is in collection history. See [search-led collection](search-led-collection.md) for the boundary and configuration names.

This verifies bounded cloud search intake, not predictive intelligence, substantive autonomous assessment, exact scheduler timing, cold-start SLA or notification replacement. No duplicate ongoing ChatGPT research schedule was created. The user subsequently authorized appending Site writeback to **Geopolitical Escalation Watch II**. Its original instructions, hourly recurrence, Eastern time zone and same-chat destination were preserved; readback confirmed the approved addition. A manual Run now completed collection run30 with five new leads (148–152). Independent native readback of lead148 confirmed its original URL, unverified status, inaccessible original-page disclosure and query provenance. This is observed manual execution of the existing watch; clock-triggered recurring writeback remains unverified.

## Server-only configuration

Sites uses `SIGNAL_DESK_READ_TOKEN`, `SIGNAL_DESK_SEARCH_WRITE_TOKEN`, `SIGNAL_DESK_WRITER_OWNER_EMAIL_SHA256` and `SIGNAL_DESK_WRITER_OWNER_ID_SHA256` as secret runtime bindings. Render holds the corresponding read/write token digests plus its existing server-only Supabase key. Owner identity is pinned from an authenticated owner read; writing fails closed if either identity pin is absent or mismatched. The public Site audience is preserved.

## Optional local adapter

`integrations/mcp_server.py` remains an operator-managed local stdio adapter with its own secure environment. It does not replace the native Site plugin or prove unattended cloud behavior. Never give it a Supabase secret.

## Remaining product work

Evidence-bound event correlation and versioned researcher-supplied hypotheses are implemented and exercised with genuine sources. The authorized hourly watch is configured to save these reviews; its next clock-triggered collection/analysis receipt is a separate acceptance check. Broader authorized firsthand/local-language discovery, comprehensive original-source tracing, activity baselines, retrospective metrics, licensed market/transport datasets and external notification delivery remain incomplete. The UI distinguishes automated topic queues and triage from analyst investigations and conditional hypotheses; missing scores/prices are not fabricated.
