# Signal Desk / Market Intelligence

The market side runs in the **same Render FastAPI service and existing Supabase project** as Geopolitical Signal Desk, but uses its own tables, routes, provenance fields and assessments. Geopolitical leads and alerts stay unchanged. The existing ChatGPT Site is separately hosted and is not edited by this feature. Market web view: /market on the Render service.

## Current milestone and limits

V1 implements an append-only public-source market evidence ledger, separate reviewed downside/rebound scenarios, a source-attributed dashboard, and as-of historical replay. **There are no live quotes, credit spreads, SEC filing collectors, scheduled market sweeps, brokerage integrations, market-alert delivery, calibrated forecasts or trade execution connected yet.** Missing inputs must appear as coverage gaps, never as normal market readings.

The public dashboard and public-snapshot route only read public market research. Do not ingest personal holdings, private account statements, proprietary analyst notes, account data, or secrets.

## Temporal integrity and provenance

Every observation contains a source URL, original publisher, original publication time, separately recorded immutable server ingestion time, family, phase, pressure, explicit origin grouping, caveats and a claim status. Source groups avoid treating syndicated copies as separate primary origins. That grouping alone **does not prove independent reporting**.

* **Live mode** includes an observation only if it was both published and actually collected before the chosen cutoff.
* **Replay mode** includes a source if it was already public before cutoff, even when Signal Desk did not ingest it until later. It is a retrospective, *not a prior alert*.
* Scenario assessments must reference existing source IDs, pass a database-enforced as-of check, present a countercase, falsification conditions, next observation, data gaps and a specified trading-session horizon.
* Risk family counts are an **uncalibrated descriptive heuristic**, never a directional probability or price prediction.
* Market tables have RLS enabled and no public, anonymous or authenticated table access. Only server-side service-role operations can read/write them. A public-safe API serves the read-only dashboard without exposing database secrets.

## API surface

| Method | Path | Credentials | Role |
| --- | --- | --- | --- |
| GET | /market | None | Read-only market UI |
| GET | /v1/market/public-snapshot | None | Public market-source research |
| GET | /v1/market/snapshot | Reader/owner | Authenticated readout |
| GET | /v1/market/observations | Reader/owner | Paged evidence |
| POST | /v1/market/observations | Owner | Write reviewed source-linked observation |
| GET | /v1/market/assessments | Reader/owner | Paged hypotheses |
| POST | /v1/market/assessments | Owner | Store evidenced conditional assessment |

The narrowly scoped geopolitical search-intake writer cannot call market writes.

## November 5, 2026: reconstructed warning chain

These articles **predate** the crash, so an analyst could have investigated them beforehand. They were *not* historical Signal Desk notifications.

1. **October 14:** Reuters reported sharp declines in debt-heavy AI issuers, stressed financing conditions, and rising default-insurance costs. [Reuters](https://www.streetinsider.com/Reuters/AI%2Binfrastructure%2Bfirms%2Bslump%2Bas%2BUS%2Brate%2Bhike%2Bbets%2Brise/27186106.html).
2. **October 15:** Reuters reported CoreWeave CDS above 1,100 basis points, with some bond yields at distressed levels. [Reuters](https://www.investing.com/news/stock-market-news/ai-credit-default-swap-spreads-widen-sharply-4950549).
3. **October 16:** Reuters highlighted the lender selection and refinancing consequences of high debt for private AI infrastructure. [Reuters](https://www.investing.com/news/stock-market-news/private-credit-roundup-coreweaves-debt-stress-tests-private-credits-ai-financing-boom-4954178).
4. **October 28:** Axios detailed bondholders differentiating borrowers and re-pricing AI tenant concentration. [Axios](https://www.axios.com/2026/10/28/ai-bonds-coreweave-oracle).
5. **November 3:** Axios described higher borrowing costs even for top-rated firms and rising CDS costs for major technology borrowers. [Axios](https://www.axios.com/2026/11/03/ai-bonds-rates-cds).
6. **November 5:** AP reported a 5.5% Nasdaq loss and 4.4% S&P 500 loss amid credit, AI ROI, election and data-center siting risks. [AP](https://apnews.com/article/7ee9c212805aa5e70cf34d25aab7a36e).

The Reddit claim that circular AI financing broke down should be kept as an **unverified hypothesis** until validated against contracts, debt issuance, CDS/loan rates, liquidity, revenue concentration and first-party filings. A sharp decline is not proof of a financing collapse.

## Prioritized market coverage

1. **AI credit:** CDS, yield/spread levels, liquidity, bond issuance outcomes, debt maturities, secured lending/haircuts, data-center tenant risk and refinancing needs.
2. **AI spending economics:** capex versus incremental revenue/free cash flow, backlog quality, cash-to-lease obligations, GPU rental utilization/pricing and power/construction constraints.
3. **Policy and siting:** ballot measures, municipal restrictions, state legislation, capacity queues, utility rate proceedings and binding project impacts.
4. **Market tape:** Nasdaq, S&P, Russell 2000, semiconductor relative strength, advance-decline breadth, volatility, options positioning and cross-asset credit confirmation.
5. **Rates/macroeconomy:** real yields, Fed futures, inflation surprises, dollar funding, growth and geopolitical spillovers from the existing desk.
6. **Independent counterevidence:** improved finance terms, successful AI revenue conversion, stable orders/utilization, policy risk resolution and improving market breadth.

### Conditions to monitor

- **Further decline:** continuing AI credit spread widening, reduced primary financing access, weaker spending guidance and selling spreading to non-AI assets.
- **Stabilization:** durable credit-spread narrowing, sustained financing access, improving breadth and clearer regulatory bounds.
- **Durable rebound:** a funding recovery reinforced by earnings, real customer revenue and demand evidence, rather than just a one-day relief rally.

These are conditional observations, not calibrated forecasts. Good backtests must record actual collection times, avoid using post-event information, tally missed moves and false alarms, and compare to naive baselines.

## Rollout

1. Run Python 3.11 tests from requirements-test.txt. Review feature PR.
2. Merge the market code and apply db/market_v1.sql to the existing Supabase project; **do not** create a second paid project.
3. Verify existing authenticated /ready and geopolitical endpoints, plus /market and /v1/market/public-snapshot.
4. Check Supabase security advisors and verify anonymous database queries cannot access market tables. Seed only reviewed real public sources.
5. Build properly licensed timed market feeds, dedicated market-side collector/alerts, and a link from the separately hosted ChatGPT Site.

No market database migration or deployment should be described as live until verified separately.
