# Market technical evidence V3 — operational runbook

**Purpose:** Quantitative, reproducible *research* based on official Cboe VIX and VXN historical daily prices. Never treat volatility-index prices as equity quotes or an instruction to purchase/sell a stock. The analytical method is our own implementation of public principles described by Barebone AI.

## Actual calculation

Code: `app/market_technicals.py`. For each Cboe official CSV with enough completed daily bars:

- Validate 60–320 chronologically ordered daily records. Reject conflicting duplicate dates, invalid OHLC, malformed price history and future/incomplete sessions. If the provider returns close-only data, do NOT invent open/high/low values: SMA/RSI can be calculated but ATR and pivot levels remain absent.
- Daily SMA20/SMA50, 14-period Wilder RSI and Wilder ATR, each calculated from numeric observations.
- Aggregate completed ISO calendar weeks into a separate series. A current partial week is excluded unless its regular Friday session has completed.
- Locate daily reversals using two bars before and two bars after the pivot, weekly reversals using one completed week before and after. A pivot at the end cannot appear without enough subsequent confirming bars.
- Group daily and weekly pivots by a numeric tolerance of max(0.75 × ATR14, 0.75% × last close). An actionable-looking zone only appears when both completed timeframes independently contribute a pivot. Zone half-width is max(0.35 × ATR14, 0.4% × last close).
- An upside *reference* setup requires confirmed close > SMA20 > SMA50, above the completed weekly SMA10, RSI between 35 and 70, a confirmed support zone below and resistance zone above, and ≥1.5× modeled gross reward/risk. Even then it is **REFERENCE_LEVELS_ONLY, NEVER AN ORDER**. If not, WAIT / NO_LEVELS.
- No probabilities, predictive confidence percentage, or claimed price edge. Confidence requires prospective evaluation with a real denominator and trading-cost data.

## Provenance and reproducibility

The existing Treasury/Cboe daily reference collector downloads the official HTTPS allowlisted CSV. It first stores the official Cboe daily closing observation, preserving its source URL, session date, actual first-collected timestamp and primary-document classification. It then parses the *same fetched raw bytes* for the quantitative engine, and persists a separate append-only `market_technical_snapshots` row with source_observation_id, method version, 60–320 input bars, SHA256 of normalized inputs, calculation timestamp, observed session and transparent math. The database RPC enforces matching source publisher/metric/closing price/session, no future timestamps, and role-restricted writes.

The public dashboard and public-snapshot expose a bounded **sanitized view** of the snapshot (never raw bars), and only after both original source collection and calculation have occurred. Replay cannot transform a subsequently generated technical result into a previously issued alert.

The original geopolitical collector and its schedule are unchanged. The market reference collector schedule continues independently. Quant processing failures must make the provider receipt PARTIAL with explicit `quantitative_analysis.status = unavailable`; a successful source fetch alone is not successful quant analysis.

## Exact acceptance

1. Python regression suite passes, plus Node dashboard JS syntax.
2. `db/market_technicals_v3.sql` applied only to existing Supabase project. Anonymous/authenticated direct table access denied and function execution service-role only.
3. An actual official Cboe fetch (not a fixture) results in at least one `market_technical_snapshots` record citing a real `market_observations` row. Verify source date and recorded calculation time; don't claim live until this holds.
4. The current Render main deployment exposes that source-linked numerical output with `trade_ticket_approved=false`. A historical cutoff earlier than the actual calculation sees no snapshot.
5. Re-run the same feed without inventing a new price or duplicate snapshot. If source content differs, append a new version and preserve old input hash.
6. When the source lacks OHLC, the correct output is a partial close-only readout with no ATR zones, not fabricated levels.
7. No real-money tickets, account data, broker orders or stock vendor entitlement implied.

## Still dependent on owner-approved external sources

- Stock and ETF technical calculations require entitled price and volume data, exchange session/quote freshness, and a real earnings calendar. The server has a read-only Alpaca adapter but no active key/entitlement has been verified.
- AI issuer credit/CDS/loan access and market-wide breadth are *not* in the official Cboe/Treasury sources. Do not substitute the VIX/VXN vol index for CDS or assume market direction from a vol spike.
- SEC filing metadata requires a real SEC contact identity in server environment. A valid integration does not itself interpret 10-Q/8-K financial statements.
- Brokerage-linked current holdings and tax lots require owner opt-in and a private session. Public Market Intelligence should never expose them.
- Outcome and strategy comparisons must use strictly forward-observed facts with at least the stated horizon, include missing windows, and distinguish volatility-index changes from tradable P&L.
- The current toolset cannot directly modify the separate ChatGPT Site. The Render `/market` interface remains a transparent transitional research client until owner-authorized Site integration is tested.

## Competitive validation

Barebone AI's publicly described methods: https://barebone.ai/resources/how-ai-technical-analysis-works and https://barebone.ai/faq. Our thresholds and algorithms are independently chosen and fully disclosed. We cannot claim the two products use equivalent proprietary formulas or that ours outperforms without repeatable, side-by-side forward results.
