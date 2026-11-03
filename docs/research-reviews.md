# Evidence-linked research reviews

The authorized research caller supplies reasoning. The backend validates references, persists source snapshots, records correlations and versions hypotheses; it does not run an autonomous language model or infer corroboration from similar text, shared topics, domains or URL counts.

`SearchBatch.analyses` accepts up to four bounded event reviews alongside collected hits. Every referenced source must occur in that batch, including older URLs revisited for an update. Raw single-source leads remain admissible without an analysis or corroboration. The owner writer cannot use the general analyst endpoints or assess arbitrary unsubmitted records. Operators can review previously collected evidence through `POST /v1/reviews`.

Each review records a stable event key, as-of time, reporting window, source-origin basis and limitations, competing hypotheses with explicit supporting/contradicting references, ordinary alternatives, counterevidence, baseline comparison, strengthening/falsification conditions, and a separate notification decision. Qualitative hypothesis confidence is separate from lead status and market implications. No observation becomes verified through this path.

Researcher-documented shared origin groups produce persistent `same_origin` relations. Unknown origins remain unknown; distinct groups do not become independent corroboration. A source can support one hypothesis and contradict a competing hypothesis. Incompatible roles within one hypothesis require an explicit narrower formulation. No independent-corroboration count is manufactured.

`signal_review_v1` commits the event membership, relations, assessment versions, decision and audit record atomically. A stable content hash deduplicates retries. Changed reasoning or new evidence produces another immutable version under the same event key. The transaction serializes updates to an event; an older as-of backfill cannot replace a newer current event scenario. Collection remains separately receipted: analysis failure preserves raw leads and marks the intake partial for retry.

The exact observation/access snapshot used in a research review is preserved in its assessment JSON, with its collection-run ID. This matters when a source URL is revisited: the original lead remains immutable, while later collected paraphrases and review snapshots retain their own history.

Historical backfill preserves publication and event timestamps independently of retrieval time. Unknown timezones/dates remain notes rather than invented UTC values. Imported ChatGPT report portions require `historical_backfill=true` and report ID/as-of/limitations. They are labeled `historical_summary` and add no evidence origin. Recovered original public links remain source observations; private report text is not copied to the public repository or Site.

The existing hourly watch may submit these reviews using the owner writer, without another research sweep or changed intelligence-alert rules. A recorded `alert` decision does not send a notification. The original watch retains its own notification behavior. No replacement external delivery is implemented.

`GET /v1/clusters/{id}` reads the persistent evidence and review history independently of the main dashboard's lead window (up to 100 links/versions per response). A lead's detail includes assessments from its linked event clusters, not only assessments anchored to that lead. The Site displays as-of dates, historical-summary labels, source-origin notes, hypotheses, decisions and original links. Topic queues remain visually distinct.

Security: no new tables, no end-user policies, no credential rotation. The review RPC is SECURITY INVOKER, has an empty search path, and grants execution only to service_role. Existing Site owner pins and server-only search-writer credentials remain.
