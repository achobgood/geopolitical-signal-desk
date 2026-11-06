# Signal Desk V4 — Gauntlet Execution Contract

**Goal:** Build real free historical stock data, official SEC fundamentals and measurable forward evaluation within the existing Signal Desk. No architectural/code/design theater or invented proof.

## Responsibility boundaries
- Lead defines observable acceptance requirements and protects the existing geopolitical scheduler and Site.
- Builder writes code and tests and captures artifact commits and logs. Builder never declares its own PASS.
- Independent critic must run in a genuinely separate coding-agent context, not a self-review disguised as one. The current ChatGPT connector session cannot spawn isolated Claude/Codex agents; do not claim this is a completed independent Gauntlet.
- Integrator merges only passing reviewed code, verifies actual external source and database evidence and describes all remaining blocked inputs.

## Objective release gates
1. Alpaca Basic free stock prices: an explicit historical SIP mode requires a bounded request end at least 16 minutes before now; IEX real-time is separately labeled single-venue, never SIP or a consolidated quote. Free vs paid entitlement labels must be factual. No fallback to the wrong feed, no invented prices, no executable tickets from delayed data.
2. Prices: fetch actual stock/ETF OHLCV and paginate with hard limits, validate OHLCV, nonduplicated ascending sessions and no partial current-day bars; record adjustment policy (split vs dividends), vendor and session metadata. Malformed/stale/blocked provider must produce a clear error rather than a plausible chart.
3. SEC official XBRL: require a genuine monitored SEC_USER_AGENT in the server environment; parse actual data.sec.gov companyfacts, retain period/frame, accession, filing date, currency and first-collected timestamp. Capex, revenues, CFO, cash and debt calculations must use matching fiscal periods and return MISSING on absent numbers. Filing metadata is not financial interpretation.
4. Evaluation: retrospective analysis must never be passed off as an actual earlier prediction. Recorded recommendations require future subsequent data to be assessed. Use explicit universe, cash and index benchmarks, costs/slippage, drawdown, false positives, missed signals and all periods including missing inputs. No profitability claim without real forward observations.
5. Security: no personal holdings, tax lots, cash, keys, private trade recommendations or original proprietary documents in public snapshots, client JavaScript, model research payload or logs. No broker execution, no new paid service, same database and backend.
6. Verification: GitHub full regression CI and production smoke, Supabase RLS/permissions, actual provider response and persisted source row with first-collected time, owner-only auth and fail-closed testing. Preserve the original geopolitical cron and existing recorded data.

## Three-pass max loop
- Lead sets the testable outcome, reference behavior and pass budget.
- Builder commits a change. A fresh, separately isolated critic examines only the source artifact and acceptance rubric, runs code, adversarial cases and live acceptance and returns concrete PASS/FAIL evidence.
- Each FAIL is reproduced and revised; repeat within at most three passes. Stop for security, licensing or permission blockers, and show exactly what remains.
- Integrator verifies deployed SHA and real database data, with a release ledger showing what passed, was only unit-tested, or remains unconfigured.
- We can implement objective automated gates in this chat; independent blind-agent gauntlet requires a separate agent-capable environment. Do not call ordinary CI equivalent to independent critique.

## First tranche
Repair free Alpaca Basic historical SIP and separate real-time IEX, with truthfully labeled response and non-executable technical analysis. Then first-party SEC companyfacts fundamental extraction and nonhindsight strategy evaluation. No credential invention; request missing customer/provider permission when it becomes relevant.
