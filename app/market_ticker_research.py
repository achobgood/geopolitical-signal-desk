"""Compose actual independent ticker research sections; owner-only route required.

No broker calls, private holdings, made-up price history or probability. A failed
provider does not erase successful issuer facts or retained source observations.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from app.market_alpaca_history import fetch_historical_equity
from app.market_companyfacts import fetch_company_fundamentals
from app.market_etf_holdings import ETF_TICKERS, fetch_etf_facts, fetch_etf_holdings
from app.market_sec import SEC_TICKERS
from app.market_technicals import analyze_bars

SUPPORTED_TICKERS = frozenset(SEC_TICKERS) | ETF_TICKERS
AVAILABLE = frozenset(("AVAILABLE", "ANALYZED", "ANALYZED_WITH_GAPS", "STORED_SNAPSHOT", "RESEARCH_ONLY", "STALE"))


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("Source observation needs timezone")
    return parsed


async def retained_ticker_news(store, ticker: str, as_of: datetime) -> dict:
    observations, amendments = await asyncio.gather(
        store.rows("market_observations", limit=250), store.rows("market_source_amendments", limit=100))
    latest = {}
    for amendment in sorted(amendments, key=lambda row: row["id"], reverse=True):
        if _time(amendment["reviewed_at"]) <= as_of:
            latest.setdefault(amendment["observation_id"], amendment)
    rows = []
    for observation in observations:
        if ticker not in (observation.get("tickers") or []):
            continue
        if _time(observation["published_at"]) > as_of or _time(observation["collected_at"]) > as_of:
            continue
        amendment = latest.get(observation["id"])
        if amendment and amendment["amendment_kind"] == "retraction":
            continue
        row = {key: observation.get(key) for key in ("id", "publisher", "source_title", "source_url", "published_at", "collected_at", "event_at", "origin_group", "summary", "family", "phase", "pressure", "claim_status", "caveats", "tickers")}
        if amendment:
            row.update({"source_review": amendment["amendment_kind"], "source_reviewed_at": amendment["reviewed_at"], "source_review_note": amendment["rationale"]})
            if amendment["amendment_kind"] == "canonical_link":
                row["original_source_url"] = row["source_url"]
                row["source_url"] = amendment["corrected_source_url"]
        rows.append(row)
    rows.sort(key=lambda row: row["published_at"], reverse=True)
    return {"status": "AVAILABLE" if rows else "NO_RETAINED_TICKER_NEWS", "observations": rows[:30],
            "scope": "Most recent 250 retained public market observations; exact recorded ticker links, not comprehensive global news",
            "ticker_links_are_recorded_interpretations": True,
            "independent_origin_count_is_not_inferred": True,
            "missing_sources": ["No licensed global financial news, analyst consensus or earnings-transcript stream connected"]}


async def _fundamentals(store, ticker, force):
    try:
        stored = await store.rows("market_fundamental_snapshots", limit=1, filters={"ticker": "eq."+ticker})
    except Exception:
        stored = []
    if stored and not force:
        return {**stored[0], "status": "STORED_SNAPSHOT", "refresh_requested": False, "execution_enabled": False}
    fresh = await fetch_company_fundamentals(ticker)
    if fresh.get("status") not in AVAILABLE and stored:
        return {**stored[0], "status": "STORED_SNAPSHOT", "refresh_warning": fresh.get("reason", fresh.get("status")), "execution_enabled": False}
    return fresh


async def research_ticker(store, ticker: str, *, force=False) -> dict:
    if ticker not in SUPPORTED_TICKERS:
        raise ValueError("Ticker is outside the supported stock/ETF research universe")
    if type(force) is not bool:
        raise ValueError("Refresh control must be boolean")
    checked = datetime.now(timezone.utc)
    tasks = {"price_history": fetch_historical_equity(ticker), "news": retained_ticker_news(store, ticker, checked)}
    if ticker in ETF_TICKERS:
        tasks.update({"holdings": fetch_etf_holdings(ticker), "etf_facts": fetch_etf_facts(ticker)})
    else:
        tasks["fundamentals"] = _fundamentals(store, ticker, force)
    entries = list(tasks.items())
    outcomes = await asyncio.gather(*(task for _, task in entries), return_exceptions=True)
    sections, errors = {}, {}
    for (name, _), outcome in zip(entries, outcomes):
        if isinstance(outcome, BaseException):
            # No provider response body, secret or private exception is echoed.
            errors[name] = {"status": "SOURCE_UNAVAILABLE", "reason": "Source retrieval or validation failed; other successful sections remain available"}
        else:
            sections[name] = outcome
            if outcome.get("status") not in AVAILABLE:
                errors[name] = {"status": outcome.get("status", "UNAVAILABLE"), "reason": outcome.get("reason", "No supported data returned for this section")}
    history = sections.get("price_history", {})
    bars = history.pop("bars", None)
    if bars:
        local_capture = _time(history["first_checked_at"]).astimezone(ZoneInfo("America/New_York"))
        # Match the existing forecast pipeline's conservative availability rule:
        # at 16:15 a Basic SIP request still ends at 15:59, so its daily candle
        # must not masquerade as the completed official closing observation.
        if local_capture.time() < time(16, 30) and bars[-1].session >= local_capture.date():
            bars = [bar for bar in bars if bar.session < local_capture.date()]
            history["status"] = "STALE"
            history["completion_warning"] = "Excluded current-session daily candle before conservative 16:30 Eastern closing-data availability"
        if not bars:
            errors["price_history"] = {"status": "NO_COMPLETED_BARS", "reason": "No completed daily equity observations remain"}
            bars = None
    if bars:
        # Genuine historical OHLC, with observed session and actual capture time;
        # issuer NAV or a reported headline change never manufactures a candle.
        history["bars"] = [bar.as_dict() for bar in bars[-320:]]
        history["data_kind"] = "HISTORICAL_DAILY_OHLC"
        history["not_a_live_quote"] = True
        history["last_available_price"] = {"value": str(bars[-1].close), "session": str(bars[-1].session),
                                            "price_kind": "HISTORICAL_DAILY_CLOSE", "captured_at": history["first_checked_at"]}
        history["last_session_change_pct"] = str((bars[-1].close/bars[-2].close-1)*100) if len(bars)>1 else None
        try:
            analysis = analyze_bars(bars[-320:], captured_at=_time(history["first_checked_at"]), symbol=ticker, instrument_kind="stock")
            analysis.pop("input_bars", None)
            sections["technicals"] = {"status": "AVAILABLE", "analysis": analysis, "source_url": history["source_url"],
                                      "source_captured_at": history["first_checked_at"], "price_adjustment": history["adjustment"],
                                      "stale": history["status"] == "STALE", "execution_enabled": False}
        except (ValueError, KeyError, TypeError):
            errors["technicals"] = {"status": "INSUFFICIENT_VALID_BARS", "reason": "Actual retrieved daily bars cannot support the required deterministic calculation"}
    else:
        errors["technicals"] = {"status": "BLOCKED_PRICE_HISTORY", "reason": "No usable actual equity OHLC history; no invented indicators or price zones"}
    technical = sections.get("technicals", {})
    analysis = technical.get("analysis", {})
    quantitative = [name for name in ("fundamentals", "etf_facts", "holdings", "technicals") if sections.get(name, {}).get("status") in AVAILABLE]
    if technical.get("status") == "AVAILABLE" and not technical.get("stale") and history.get("historical_consolidated"):
        if analysis.get("trend") == "down":
            label, rationale = "AVOID", "Avoid a new technical entry under the provisional trend rule: the observed daily/weekly trend is down. This is not a sell instruction."
        elif analysis.get("trend") == "up" and analysis.get("confirmed_zones") and analysis.get("setup", {}).get("status") == "REFERENCE_LEVELS_ONLY":
            label, rationale = "BUY_SETUP", "The deterministic historical-price rule produced provisional reference levels; live quotes, earnings, portfolio and independent risk checks remain required."
        else:
            label, rationale = "WATCH", "Actual technical observations do not meet all provisional entry-setup requirements."
    elif quantitative:
        label, rationale = "WATCH", "Dated issuer facts or holdings are available, but a current source-backed technical entry cannot be assessed."
    else:
        label, rationale = "INSUFFICIENT_DATA", "No supported current quantitative ticker section is available. Retained news alone cannot create price levels or establish valuation."
    if history.get('feed')=='iex':
        errors['consolidated_coverage']={'status':'PARTIAL_EXCHANGE_ONLY','reason':'IEX covers one exchange; historical levels are not consolidated-market entry confirmation.'}
    missing = [name+": "+item["status"] for name, item in errors.items()]
    missing.extend(["Licensed analyst consensus/price-target aggregation not connected", "Earnings calendar, estimates, surprises and transcripts not connected", "No validated predictive edge or calibrated directional probability"])
    return {"ticker": ticker, "instrument_kind": "ETF" if ticker in ETF_TICKERS else "STOCK", "requested_at": checked.isoformat(), "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": "INSUFFICIENT_DATA" if not quantitative else "PARTIAL" if errors else "RESEARCH_ONLY",
            "sections": sections, "errors": errors, "classification": {"label": label, "basis": rationale,
            "scope": "PROVISIONAL_RESEARCH_SETUP_ONLY", "not_probability": True, "not_personalized_allocation_advice": True},
            "missing_inputs": missing, "rights_scope": "OWNER_PRIVATE_RESEARCH_ONLY", "execution_enabled": False,
            "not_a_forecast_or_trade_authorization": True, "portfolio_data_used": False}
