# Trade Synthesis: exact, conditional buy/sell tickets

This implementation adds **decision support** to Market Intelligence. It must make
the **what, when, size, entry, exit and why** explicit whenever sufficient timely
information is present. It must not fabricate numbers or imply a guaranteed
market bottom. It never executes orders.

## Operating pipeline

1. **Evidence:** ingest source-provenance records on AI financing, 5-day credit
   changes, earnings/capex/FCF, market breadth, volatility, rates, permitting and
   geopolitics. Distinguish firsthand filings, news accounts and hypotheses.
2. **Synthesis:** research candidate instruments in a defined universe. This is a
   separate, bounded analyst stage that develops bullish and bearish theses,
   alternatives, origin dependence, and invalidation. Its narrative is **not**
   itself an executable trading instruction.
3. **Quantitative confirmation:** verify freshly timestamped stock/ETF quotes,
   bid/ask spreads, liquidity, SMA20/SMA50/ATR14 and separately sourced
   5-session AI credit-spread change, % of market above SMA50, and 5-session
   Nasdaq VXN change. Company-specific earnings blackout information required.
4. **Account fit:** verify a fresh owner-only portfolio, account NAV, cash,
   exposure, and concentration. One account/request at a time: avoid combining
   taxable with Roth or traditional retirement when tax and execution rules differ.
5. **Deterministic risk gate:** compute the effective market regime, reject stale
   or incomplete data, and size approved instruments using dollar risk, cash
   reserves, and sector/symbol caps. Support owner-defined absolute SELL stops
   even when macro data is unavailable, provided quotes and account data are fresh.
6. **Human-reviewed proposal:** emit ticker, BUY/SELL/TRIM, shares, limit,
   expiry, trigger, protective stop/target for BUY, estimated notional, risk,
   evidence IDs and tax-review flag. *No broker execution or automatic order
   submission is available.*
7. **Learning/evaluation:** eventually record whether proposed levels were
   touched and the next-day, next-week and 20-day outcomes. Compare against
   index buy-and-hold, naive momentum and randomized/no-trade baselines; report
   false positives and all transaction costs. Do not backtest on future
   publication or first-observed data.

## Owner-only API

- POST /v1/market/trade-synthesis
  - Authorized **owner key only**, never the read-only key or the special
    geopolitical search intake key.
  - Consumes a JSON request validated by TradeSynthesisRequest in
    app/trade_synthesis.py.
  - Requires existing market observation IDs to anchor analysis; the backend
    checks those references against the store.
  - Processes the portfolio **in memory only**. No private holdings, cash,
    NAV or tax lots go into the public market database, UI or source ledger.
  - Returns a NO_TRADE reason list or the exact reviewable ticket levels.
  - Caller-supplied data is explicitly labeled
    caller_supplied_not_independently_verified. It must not be called an
    exchange-verified real-time recommendation until licensed vendor
    integrations and authenticated entitlements are actually implemented.
- GET /v1/market/trade-synthesis/status
  - Authenticated read-only readiness metadata; no personal data or positions.

### Market regime defaults, explicitly uncalibrated

Risk-off when at least **two** of:
- AI-issuer credit spreads widen at least 50 basis points in five sessions.
- Less than or equal to 35% of the broad market is above its 50-session average.
- VXN rises 20% or more in five sessions.
- AI basket relative strength falls 5% or more versus its broad benchmark.

Risk-on only when at least three confirming improving signals, **including**
credit spreads narrowing by 20 or more basis points and breadth above 60%.
These are provisional decision rules, not statistically validated thresholds.

Otherwise return MIXED or UNAVAILABLE. The default is **NO_TRADE**, not
"buy all the way down." A proposed TRIM requires risk-off, an AI position
below SMA50, and a breached AI concentration or single-symbol risk cap.
A proposed BUY requires risk-on, price > SMA20 > SMA50, bounded overextension,
adequate liquidity, no earnings within three sessions, and 90-second-fresh
regular-session quotes.

Proposed BUY ticket sizing uses the minimum of:
- Remaining symbol concentration capacity (default 7% of account NAV).
- Remaining AI sector exposure capacity (default 30% NAV).
- 25% of currently available cash per candidate.
- Cash remaining after a 10% NAV reserve.
- The share size that risks no more than 0.5% NAV at a 2x ATR proposed stop.

Defaults should be customized and validated with the owner before any
real-money trade. No shorting, leverage or leveraged ETFs are admitted.
A proposed SELL in a taxable account is flagged for tax review, because
tax lots, wash-sale periods and realization consequences are not modeled.

## Examples are simulated, NOT current market recommendations

A hypothetical $100,000 account holding 200 shares of an AI stock at $100,
with a fictional risk-off regime, quoted bid $99.95 and price below SMA50
would receive a conditional 50-share TRIM SELL LIMIT at $99.95, expiring
after 90 seconds. This is a deterministic test case, not a real Nvidia signal.

A hypothetical risk-on input set with a $100.10 ask, $3 ATR, SMA20 of $98
and SMA50 of $90 can produce a BUY LIMIT with $94.10 proposed stop and
$112.10 target. Actual position size depends on the account's cash and
remaining sector/symbol limits. Simulated model results are never represented
as real market data.

## Critical gaps to close before using this for live decisions

- A contracted or otherwise explicitly licensed consolidated quotes/bar feed
  that supplies real bid/ask, earnings calendar and reliably timestamped bars.
- AI issuer-specific CDS and bond-spread data, plus broad-market breadth.
  Treasury yields and VIX/VXN alone are not adequate substitutes.
- A private portfolio connector for current stock, ETF and cash holdings,
  including account classification and, for taxable sells, tax-lot awareness.
  ChatGPT's Finances connector can provide analysis context once connected,
  but that does not automatically grant the deployed backend access to it.
- A historical dataset for walk-forward calibration, execution slippage and
  tax/transaction-cost assumptions.
- A private UI and authenticated owner session beyond the shared owner API key
  before exposing account-specific trade tickets in a browser.

Do not paste owner API keys into the public market dashboard. Do not transmit
holdings or accounts to unverified third-party research services. No real
trade is to be placed by Signal Desk without a separate explicit execution
integration and authorization.

## Owner-private audit receipts (V2)

The server records each owner-requested NO_TRADE or PROPOSAL_ONLY result in the
existing Supabase project's private schema. Only the owner key may list decisions.
The receipt stores exact ticket proposals, evidence IDs, issue time, rule/AI mode,
and abstention reasons, but NEVER full positions, NAV, cash, account IDs, or keys.
A database RPC independently checks publication and actual collection time,
rejects future-only source IDs or backdated decisions, and rejects any accidental
portfolio fields in receipts.

A full broker position snapshot remains ephemeral within the owner request.
Private execution is still disabled; a stored recommendation is not an order,
fill, backtest result or realized P&L.
