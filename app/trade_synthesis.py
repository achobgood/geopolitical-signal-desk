"""Evidence-gated, owner-only trade synthesis.

Returns exact conditional trade tickets when sufficient current information is
supplied. No brokerage integration, order placement, portfolio persistence,
probabilities or claims that caller-supplied quotes were independently verified.
Default is NO_TRADE. Pure calculation is deterministic and unit-testable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.models import StrictModel, public_url


def aware(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("Timezone-aware market timestamp required")
    return value

D = Decimal
ZERO = D("0")
ONE = D("1")


def finite_nonnegative(v: Decimal) -> Decimal:
    if not v.is_finite() or v < ZERO:
        raise ValueError("Amount must be finite and nonnegative")
    return v


def cash(value: Decimal) -> Decimal:
    return value.quantize(D("0.01"))


def floor_shares(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


class Position(StrictModel):
    ticker: str = Field(min_length=1, max_length=12, pattern=r"^[A-Z][A-Z0-9.-]*$")
    shares: Decimal = Field(gt=0, le=100000000)
    category: Literal["ai", "diversified", "defensive", "other"]
    account_kind: Literal["taxable", "traditional_retirement", "roth_retirement"] = "taxable"
    hard_exit_below: Decimal | None = Field(default=None, gt=0, le=1000000)

    @field_validator("shares")
    @classmethod
    def valid_shares(cls, v):
        return finite_nonnegative(v)


class Portfolio(StrictModel):
    as_of: datetime
    net_liquidation_value: Decimal = Field(gt=0, le=10000000000)
    available_cash: Decimal = Field(ge=0, le=10000000000)
    positions: list[Position] = Field(default_factory=list, max_length=100)

    _asof = field_validator("as_of")(aware)

    @model_validator(mode="after")
    def reasonable_portfolio(self):
        if self.available_cash > self.net_liquidation_value:
            raise ValueError("Cash cannot exceed reported account NAV")
        if len({p.ticker for p in self.positions}) != len(self.positions):
            raise ValueError("A position ticker can appear only once per account")
        return self


class QuotedInstrument(StrictModel):
    ticker: str = Field(min_length=1, max_length=12, pattern=r"^[A-Z][A-Z0-9.-]*$")
    category: Literal["ai", "diversified", "defensive", "other"]
    instrument: Literal["stock", "unlevered_etf"]
    last: Decimal = Field(gt=0, le=1000000)
    bid: Decimal = Field(gt=0, le=1000000)
    ask: Decimal = Field(gt=0, le=1000000)
    atr14: Decimal = Field(gt=0, le=100000)
    sma20: Decimal = Field(gt=0, le=1000000)
    sma50: Decimal = Field(gt=0, le=1000000)
    average_daily_dollar_volume: Decimal = Field(ge=0, le=1000000000000)
    as_of: datetime
    market_session: Literal["regular", "extended", "closed"]
    provider: str = Field(min_length=3, max_length=80)
    source_url: str
    earnings_within_3_sessions: bool = False

    _url = field_validator("source_url")(public_url)
    _asof = field_validator("as_of")(aware)

    @model_validator(mode="after")
    def valid_quote(self):
        numbers = [self.last, self.bid, self.ask, self.atr14, self.sma20,
                   self.sma50, self.average_daily_dollar_volume]
        if any(not x.is_finite() for x in numbers):
            raise ValueError("Quote numbers must be finite")
        if self.bid > self.ask or self.atr14 >= self.last:
            raise ValueError("Crossed quote or invalid volatility")
        if not self.bid * D("0.97") <= self.last <= self.ask * D("1.03"):
            raise ValueError("Last sale too far from quoted market")
        return self


class Indicator(StrictModel):
    value: Decimal
    observed_at: datetime
    source_url: str
    origin_group: str = Field(min_length=3, max_length=120)
    _asof = field_validator("observed_at")(aware)
    _url = field_validator("source_url")(public_url)

    @field_validator("value")
    @classmethod
    def validate_value(cls, x):
        if not x.is_finite() or abs(x) > D("100000"):
            raise ValueError("Indicator invalid or out of range")
        return x


class RegimeIndicators(StrictModel):
    ai_credit_spread_change_5d_bp: Indicator | None = None
    market_pct_above_sma50: Indicator | None = None
    vxn_5d_change_pct: Indicator | None = None
    ai_relative_strength_20d_pct: Indicator | None = None


class Candidate(StrictModel):
    ticker: str = Field(min_length=1, max_length=12, pattern=r"^[A-Z][A-Z0-9.-]*$")
    reason: str = Field(min_length=20, max_length=700)
    thesis_invalidated_if: str = Field(min_length=20, max_length=700)


class RiskPolicy(StrictModel):
    max_symbol_pct_nav: Decimal = Field(default=D("7"), ge=1, le=20)
    max_ai_pct_nav: Decimal = Field(default=D("30"), ge=5, le=65)
    max_risk_per_ticket_pct_nav: Decimal = Field(default=D("0.5"), ge=D("0.1"), le=2)
    max_cash_deployment_pct: Decimal = Field(default=D("25"), ge=5, le=50)
    minimum_remaining_cash_pct_nav: Decimal = Field(default=D("10"), ge=0, le=50)
    trim_fraction_pct: Decimal = Field(default=D("25"), ge=10, le=50)
    min_average_daily_dollar_volume: Decimal = Field(default=D("10000000"), ge=100000, le=1000000000)
    max_bid_ask_spread_bp: Decimal = Field(default=D("50"), ge=5, le=300)
    max_quote_age_seconds: int = Field(default=90, ge=10, le=300)
    max_portfolio_snapshot_age_seconds: int = Field(default=300, ge=30, le=3600)
    max_indicator_age_hours: int = Field(default=30, ge=1, le=96)
    max_tickets: int = Field(default=2, ge=1, le=5)


class TradeSynthesisRequest(StrictModel):
    portfolio: Portfolio
    indicators: RegimeIndicators
    quotes: list[QuotedInstrument] = Field(default_factory=list, max_length=80)
    candidates: list[Candidate] = Field(default_factory=list, max_length=30)
    evidence_ids: list[int] = Field(min_length=1, max_length=20)
    policy: RiskPolicy = Field(default_factory=RiskPolicy)

    @model_validator(mode="after")
    def valid_lists(self):
        if len({q.ticker for q in self.quotes}) != len(self.quotes):
            raise ValueError("Quote tickers must be unique")
        if len({c.ticker for c in self.candidates}) != len(self.candidates):
            raise ValueError("Candidate tickers must be unique")
        if len(set(self.evidence_ids)) != len(self.evidence_ids) or any(x < 1 for x in self.evidence_ids):
            raise ValueError("Existing research IDs must be unique and positive")
        return self


def _market_regime(indicators: RegimeIndicators, now: datetime, policy: RiskPolicy):
    required = [
        ("AI credit-spread change", indicators.ai_credit_spread_change_5d_bp),
        ("market breadth", indicators.market_pct_above_sma50),
        ("Nasdaq implied-volatility change", indicators.vxn_5d_change_pct),
    ]
    gaps = [f"Missing {name} from a dated market data source"
            for name, ind in required if ind is None]
    present = [x for _, x in required if x is not None]
    if indicators.ai_relative_strength_20d_pct is not None:
        present.append(indicators.ai_relative_strength_20d_pct)
    for ind in present:
        age = (now - ind.observed_at).total_seconds()
        if age < 0 or age > policy.max_indicator_age_hours * 3600:
            gaps.append(f"Stale or future-dated market indicator: {ind.origin_group}")
    if len({x.origin_group for x in present}) < 2:
        gaps.append("At least two separately attributed market data origins required")
    breadth = indicators.market_pct_above_sma50
    if breadth and not ZERO <= breadth.value <= D("100"):
        gaps.append("Market breadth percentage outside 0 to 100")
    if gaps:
        return "unavailable", 0, 0, gaps

    a = indicators.ai_credit_spread_change_5d_bp.value
    b = breadth.value
    v = indicators.vxn_5d_change_pct.value
    r = indicators.ai_relative_strength_20d_pct
    bearish = int(a >= D("50")) + int(b <= D("35")) + int(v >= D("20"))
    bullish = int(a <= D("-20")) + int(b >= D("60")) + int(v <= D("-10"))
    if r is not None:
        bearish += int(r.value <= D("-5"))
        bullish += int(r.value >= D("3"))
    # Opposing signals are not "neutral = green light".
    if bearish >= 2:
        return "risk_off", bearish, bullish, []
    if bullish >= 3 and a <= D("-20") and b >= D("60"):
        return "risk_on", bearish, bullish, []
    return "mixed", bearish, bullish, []


def synthesize(req: TradeSynthesisRequest, *, now: datetime | None = None) -> dict:
    """Calculate conditional owner-reviewed tickets; never place orders."""
    now = now or datetime.now(timezone.utc)
    policy = req.policy
    problems = []
    portfolio_age = (now - req.portfolio.as_of).total_seconds()
    if portfolio_age < 0 or portfolio_age > policy.max_portfolio_snapshot_age_seconds:
        problems.append("Portfolio positions/cash are not sufficiently fresh")
    quotes = {q.ticker: q for q in req.quotes}
    for held in req.portfolio.positions:
        if held.ticker not in quotes:
            problems.append(f"Missing live quote for held position {held.ticker}")
    for q in req.quotes:
        age = (now - q.as_of).total_seconds()
        if age < 0 or age > policy.max_quote_age_seconds:
            problems.append(f"Quote for {q.ticker} is stale or future-dated")
        if q.market_session != "regular":
            problems.append(f"{q.ticker} has no verified regular-session quote")
        if (q.ask - q.bid) / q.ask * D("10000") > policy.max_bid_ask_spread_bp:
            problems.append(f"Quoted spread too wide for {q.ticker}")
        if q.average_daily_dollar_volume < policy.min_average_daily_dollar_volume:
            problems.append(f"Insufficient dollar liquidity in {q.ticker}")
    nav = req.portfolio.net_liquidation_value
    held_value = sum((p.shares * quotes[p.ticker].last for p in req.portfolio.positions
                      if p.ticker in quotes), ZERO)
    if held_value + req.portfolio.available_cash > nav * D("1.05"):
        problems.append("Portfolio NAV does not reconcile with holdings and cash")

    regime, bears, bulls, regime_gaps = _market_regime(req.indicators, now, policy)
    problems.extend(regime_gaps)
    # No limit price when market data is incomplete. Exact timing requires current
    # holdings and regular-session quote integrity, not a press headline.
    if problems:
        return {
            "status": "NO_TRADE", "regime": "unavailable", "as_of": now.isoformat(),
            "tickets": [], "blocked_by": sorted(set(problems)), "bearish_signals": bears,
            "bullish_signals": bulls, "evidence_ids": req.evidence_ids,
            "decision_basis": "Market, portfolio, and quote inputs must all pass before issuing a ticket",
            "execution": "manual_approval_only", "source_attestation": "caller_supplied_not_independently_verified",
        }

    holdings = {p.ticker: p for p in req.portfolio.positions}
    ai_value = sum((p.shares * quotes[p.ticker].last for p in req.portfolio.positions if p.category == "ai"), ZERO)
    tickets = []
    # A user-specified absolute exit level takes priority over macro regime labels.
    # Never invent an exit threshold from the stock's recent price drop.
    for p in sorted(req.portfolio.positions, key=lambda item: item.ticker):
        if p.hard_exit_below is None:
            continue
        q = quotes[p.ticker]
        if q.last > p.hard_exit_below:
            continue
        shares = floor_shares(p.shares)
        if shares < 1:
            continue
        limit = cash(q.bid)
        tickets.append({
            "symbol": p.ticker, "action": "SELL", "shares": shares,
            "limit_price": str(limit), "order_type": "SELL_LIMIT",
            "trigger": f"Owner-defined hard exit reached: last sale <= {p.hard_exit_below}",
            "expires_at": (now + timedelta(seconds=90)).isoformat(),
            "estimated_notional": str(cash(limit * shares)),
            "reason": "Owner-defined hard-exit price, not an inferred market-bottom call",
            "stop_price": None, "target_price": None,
            "tax_review": p.account_kind == "taxable",
            "evidence_ids": req.evidence_ids,
        })
        if p.category == "ai":
            ai_value -= q.last * p.shares
        if len(tickets) >= policy.max_tickets:
            break
    if regime == "risk_off" and len(tickets) < policy.max_tickets:
        for p in sorted((x for x in req.portfolio.positions if x.category == "ai"),
                        key=lambda x: x.shares * quotes[x.ticker].last, reverse=True):
            if any(t["symbol"] == p.ticker for t in tickets):
                continue
            q = quotes[p.ticker]
            symbol_weight = (q.last * p.shares / nav) * D("100")
            ai_weight = ai_value / nav * D("100")
            if q.last >= q.sma50 or (symbol_weight <= policy.max_symbol_pct_nav and
                                     ai_weight <= policy.max_ai_pct_nav):
                continue
            shares = floor_shares(p.shares * policy.trim_fraction_pct / D("100"))
            if shares < 1:
                continue
            limit = cash(q.bid)
            if limit <= ZERO:
                continue
            tickets.append({
                "symbol": p.ticker, "action": "TRIM", "shares": shares,
                "limit_price": str(limit), "trigger": "Regime risk_off AND last sale below 50-day moving average AND AI concentration breaches policy",
                "order_type": "SELL_LIMIT", "expires_at": (now + timedelta(seconds=90)).isoformat(),
                "estimated_notional": str(cash(limit * shares)),
                "reason": "Concentration reduction in a confirmed risk-off regime",
                "stop_price": None, "target_price": None,
                "tax_review": p.account_kind == "taxable",
                "evidence_ids": req.evidence_ids,
            })
            ai_value -= p.shares * q.last * (D(shares) / p.shares)
            if len(tickets) >= policy.max_tickets:
                break

    elif regime == "risk_on" and len(tickets) < policy.max_tickets:
        remaining_cash = req.portfolio.available_cash
        ranked = []
        for candidate in req.candidates:
            q = quotes.get(candidate.ticker)
            if q is None or q.earnings_within_3_sessions or any(t["symbol"] == candidate.ticker for t in tickets):
                continue
            if not (q.last > q.sma20 > q.sma50):
                continue
            if q.last > q.sma20 * D("1.08"):
                continue  # do not chase a >8%-extended move
            # Stronger trend has higher priority. Not a return forecast.
            rank = q.last / q.sma50 - ONE
            ranked.append((rank, candidate.ticker, q, candidate))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        for _, ticker, q, candidate in ranked:
            existing = holdings.get(ticker)
            if existing and existing.category != q.category:
                continue
            position_value = existing.shares * q.last if existing else ZERO
            if position_value >= nav * policy.max_symbol_pct_nav / D("100"):
                continue
            if q.category == "ai" and ai_value >= nav * policy.max_ai_pct_nav / D("100"):
                continue
            limit = cash(q.ask)
            stop = cash(limit - D("2") * q.atr14)
            if stop <= ZERO or limit - stop < D("0.01"):
                continue
            risk_per_share = limit - stop
            capacity = min(
                remaining_cash * policy.max_cash_deployment_pct / D("100"),
                max(ZERO, nav * policy.max_symbol_pct_nav / D("100") - position_value),
                max(ZERO, remaining_cash - nav * policy.minimum_remaining_cash_pct_nav / D("100")),
                (nav * policy.max_risk_per_ticket_pct_nav / D("100") / risk_per_share) * limit,
            )
            if q.category == "ai":
                capacity = min(capacity, max(ZERO, nav * policy.max_ai_pct_nav / D("100") - ai_value))
            shares = floor_shares(capacity / limit)
            if shares < 1:
                continue
            risk = cash(D(shares) * risk_per_share)
            if risk > nav * policy.max_risk_per_ticket_pct_nav / D("100"):
                continue
            target = cash(limit + D("2") * risk_per_share)
            tickets.append({
                "symbol": ticker, "action": "BUY", "shares": shares,
                "limit_price": str(limit),
                "trigger": "Regime risk_on AND price > SMA20 > SMA50 AND no near-term earnings AND all risk limits satisfied",
                "order_type": "BUY_LIMIT", "expires_at": (now + timedelta(seconds=90)).isoformat(),
                "estimated_notional": str(cash(D(shares) * limit)),
                "risk_at_proposed_stop": str(risk),
                "stop_price": str(stop), "target_price": str(target),
                "reason": candidate.reason,
                "thesis_invalidated_if": candidate.thesis_invalidated_if,
                "tax_review": False, "evidence_ids": req.evidence_ids,
            })
            remaining_cash -= D(shares) * limit
            if q.category == "ai":
                ai_value += D(shares) * limit
            if len(tickets) >= policy.max_tickets:
                break
    return {
        "status": "PROPOSAL_ONLY" if tickets else "NO_TRADE",
        "regime": regime, "as_of": now.isoformat(), "tickets": tickets,
        "blocked_by": [] if tickets else [
            "No candidate met both regime-specific trade trigger and portfolio risk requirements"
        ],
        "bearish_signals": bears, "bullish_signals": bulls,
        "evidence_ids": req.evidence_ids,
        "decision_basis": "Deterministic rule-based synthesis with explicit price, portfolio and market checks",
        "execution": "manual_approval_only",
        "source_attestation": "caller_supplied_not_independently_verified",
        "limitations": "No tax-lot optimization, real brokerage fill data or verified exchange quote vendor connected; protective stops and targets are proposed levels, not orders.",
    }
