# Market Intelligence V2 — capability and acceptance map

This release is an **incremental, independently tested step** toward an evidence-to-trade system, NOT a claim of a profitable or fully automated investment strategy. All statements below describe the code, not provisioned vendor entitlements.

## What is implemented

- First-party Treasury and Cboe daily reference collection persists timestamped source rows and provider receipts.
- New: SEC EDGAR filing **metadata** discovery for an allowlisted AI issuer set. No financial statement interpretation or issuer CDS data is invented from a filing title.
- New: optional **read-only** Alpaca market-data connector at fixed data.alpaca.markets host. Supports SIP if customer has that entitlement; IEX is labeled single-venue partial coverage. Computes SMA20/SMA50/ATR14 from historical completed daily bars and checks quote recency. It does **not** place orders or silently treat IEX as SIP.
- New: append-only market source amendment/retraction ledger and correction-aware dashboard/radar. Original evidence remains available for forensic review, including original URL.
- New: private-only immutable trade recommendation receipts in Supabase private schema. Owner-only API, exact decision timestamp, supporting evidence IDs, ticket proposals, abstention reasons. No full portfolio, NAV, cash, passwords, account IDs or key material retained.
- New: remove future scenario creations and future source-health receipts from historical live views; replay remains explicitly retrospective.
- New: completed-session freshness assessment using U.S. market-close calendar approximation, including stale after-close daily sources.
- Existing AI source-grounded candidate generation and deterministic buy/sell/trim/hold gate remain subject to actual missing-data checks; no brokerage execution.

## Required configuration, not yet installed by the code

1. **SEC EDGAR**: a genuine company + monitored contact address in Render environment variable SEC_USER_AGENT, for example "Company Research contact@yourcompany.example". Only an actual working contact identity is acceptable. Do not commit it. The source is https://www.sec.gov/search-filings/edgar-application-programming-interfaces and its user-agent fair-access policy must be followed. If missing, SEC provider status remains not_configured.
2. **Alpaca market data**: ALPACA_MARKET_DATA_KEY_ID and ALPACA_MARKET_DATA_SECRET in **server environment only**; ALPACA_DATA_FEED=sip only with an existing authorized SIP market-data subscription, or =iex for partial-exchange research. Never use trading API endpoints. Never expose the key pair in browser/site code. If absent, quote route returns not_configured. It cannot be an executable ticket solely because a quote arrives: issuer earnings and macro/breadth checks remain disconnected.
3. **AI model**: OPENAI_API_KEY, server-side only, is needed for the optional bounded candidate-research route. Without it the route reports DISABLED. It is not a ChatGPT subscription token.
4. **Private portfolio**: the current owner-only synthesis endpoint requires a **fresh account snapshot supplied by the owner**. ChatGPT Finances being connected does NOT provide an API credential or direct consent for the hosted backend. The public Site must never receive account data, owner tokens, or private trade receipts.
5. **Issuer credit and breadth**: qualified, permitted issuer-specific bond/CDS and broad-market breadth sources are required before the full BUY/trim gates can operate reliably. Cboe/Treasury are contextual and cannot substitute.
6. **ChatGPT Sites**: the original geopolitical Site remains the user-facing primary. This release provides API contracts but does not claim to edit the separate ChatGPT Site. The standalone /market page is transitional.
7. **Notification delivery and evaluation**: no verified external market push service, order execution, position synchronization or walk-forward predictive performance. Requires separately tested transport, real historical quote/benchmark data and source coverage.

## HTTP and trust boundaries

- GET /v1/market/public-snapshot: publicly shareable research only, no private portfolio. Corrections show only once actually known at the selected cutoff.
- GET /v1/market/vendor-quote/{ticker}: **owner-only**, explicit SIP/IEX, source timestamps, OHLC/ATR data and blockers; no order creation.
- GET /v1/market/data-sources/status: server-side credential-presence and coverage descriptions, never secret values.
- POST /v1/market/amendments: **owner-only**, append corrections or retractions with an explanation and permanent record.
- GET /v1/market/trade-synthesis/history: **owner-only** private decision receipts.
- POST /v1/market/trade-synthesis and /auto: **owner-only** in-memory private portfolio evaluation; every decision is persisted via a restricted Postgres RPC as a receipt. The database rejects future-only evidence and backdated decisions. Zero broker-order calls.
- Existing public market research and original geopolitical routes stay on the same Render API with distinct credentials and data models.

## Acceptance gates and proof, not presentation

1. Unit and UI syntax tests pass on Python 3.11 CI, including no lookahead and fail-closed parsers.
2. Additive migration db/market_audit_decisions_v2.sql applied to the same Supabase project, all new tables RLS enabled; no anonymous/authenticated access or public RPC execution.
3. Original geopolitical /ready, scheduled cron and Site unaffected.
4. /market public read, source correction evidence and /v1/market/data-sources/status deployed and verified; no client secrets.
5. End-to-end issuer filings only counted after a real SEC provider run using approved SEC contact headers; credential absence must remain not_configured.
6. End-to-end stock price status only counted after an authorized SIP data provider responds with fresh quotes and enough completed daily bars. Until then: NO_TRADE for price-dependent tickets.
7. Decision receipt stored privately without positions, survives server restart, reads only with owner credential; public readers cannot access.
8. An actual **clock-fired** market cron with completed source receipt; a one-off owner manual function call tagged supabase_cron is not adequate proof.
9. Forward research evaluations measured against benchmark outcomes on sources captured before the decision, including false positives, false negatives and the denominator of all scanned periods.
10. The original ChatGPT Site's UI/permissions tested before claiming consolidation, not guessed.

This is an engineering acceptance document, not a financial projection.
