# Free-source market research: SEC annual fundamentals + Cboe context study

## Exact capabilities (not a stock-trading product)

This release adds two genuine calculations to the SAME Signal Desk backend:

1. **SEC official companyfacts**: a guarded, owner-only reader for issuer-specific, standardized us-gaap USD financial statement facts. Selects complete annual 10-K/10-K/A periods, groups all values by exact accession, start and end dates, and refuses to combine incompatible reports. Captures primary tag, filed date, financial-report period, form, fiscal period/frame and server-observed timestamp. Derived free cash flow is operating cash flow minus payments to acquire property, plant and equipment only when both are disclosed in the same filing/period; it is not a GAAP metric or a claim of company guidance. Comparable previous annual revenue must use the same XBRL taxonomy tag and 330–400-day separation. Missing or conflicting values are explicit, never silently assumed zero. The whole official response is SHA256 hashed to distinguish changed source snapshots. Repo: app/market_companyfacts.py.

2. **Cboe historical volatility study**: uses actual RLS-protected 320 daily Cboe VIX/VXN closing data previously collected by Signal Desk, not fixtures or news articles. It calculates historical 20%+ five-session volatility-index spikes and their subsequent 5- and 20-completed-session changes, with counts of all eligible windows, overlapping triggered events and right-censored outcomes. It compares median subsequent INDEX changes after threshold hits against all windows. A retrospective scan created NOW must NEVER be displayed as an older real Signal Desk alert, predictive edge or stock investment return. Repo: app/market_volatility_study.py.

## Public vs owner-only interfaces

- Public GET /v1/market/public-snapshot: descriptive studies appear only after the SEC/Cboe source itself was captured and the corresponding quantitative run completed. Volatility numbers are from source closing levels and are unrelated to user holdings, stock orders, portfolios or probabilistic forecasts. The UI explicitly labels retrospective studies, overlapping windows and uncalibrated thresholds.
- Owner-only GET /v1/market/company-fundamentals/{ticker}: returns latest separately recorded source-linked annual analysis for the approved issuer universe, or NOT_COLLECTED. It never silently fetches SEC in a GET.
- Owner-only POST /v1/market/company-fundamentals/{ticker}/refresh: resolves CIK from official SEC company_tickers index, fetches official companyfacts with a REAL SEC_USER_AGENT, verifies the CIK, enforces the calculation contract, and stores an append-only immutable snapshot through a service-role-only Postgres RPC. The server does not log or persist the contact email, personal portfolio or any trading credential. Without the SEC contact identity it returns NOT_CONFIGURED without making a network request.
- SQL migration: db/market_companyfacts_v4.sql. RLS prevents ordinary anonymous or authenticated users from direct table access or write RPC. Only the existing server can insert/read audited facts.

## External requirements and true deployment milestones

- Official SEC documentation: https://www.sec.gov/search-filings/edgar-application-programming-interfaces . APIs have no API key, but automated access requires valid organizational name and monitored email in the User-Agent per SEC fair access. Do not invent the identity or use an unclassified bot. Set it only in Render's protected SEC_USER_AGENT environment variable after user approval. No contact identity => no real issuer fundamentals yet. A green unit test or database schema is NOT a verified live SEC fetch.
- The public Cboe study **is** grounded in already-collected real original OHLC sessions. The independent database calculation on Nov 5 found VXN 20 five-session spike windows with subsequent five sessions, and median subsequent VXN index change of -11.45%, versus +0.76% across 310 eligible windows. VIX: 25 triggered windows, -13.45% conditional and +0.12% baseline. These are historical overlapping observations of VOLATILITY INDICES, not trading returns or calibrated forecasts.
- Alpaca Basic stock quote/history adapter is independently deployed; no actual Alpaca entitlement/key is linked. The SEC and Cboe work does not fix missing issuer CDS, broad equity breadth, stock prices, earnings calendar or private brokerage-linked positions.
- Full independent Gauntlet Loop in separate Claude/Codex critic contexts has NOT run. Repo CI, authentic source checks, standalone SQL checks and separate user-visible acceptance are partially overlapping, not a substitute for independent critic.
- The original geopolitical Signal Desk, its Source/Watch schedules and Site are preserved. This release does not deploy a second frontend or put portfolios into public views.

## Hard acceptance

The minimum successful Cboe acceptance includes a new deployed API returning actual 320-source-bar derived 5/20-session descriptive results, with actual source ID, original collection time, numerical denominators and no past retroactive alert. SQL verification of the historical dataset and Python results must match. SEC schema/role test must deny direct anonymous/authenticated access, and a rollback-only test of the official-source RPC must leave ZERO synthetic issuer records. Real SEC collection remains NOT_CONFIGURED until valid identity is supplied; never mark it LIVE because an offline fixture passed.
