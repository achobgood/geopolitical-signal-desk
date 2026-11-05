"""Separate public-source market intelligence domain; no personalized holdings or orders.

Source publication and first collection are distinct timestamps. Retrospective replay
may use subsequently discovered old articles but is NEVER a previously-issued alert.
"""
from __future__ import annotations

from collections import defaultdict, deque
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal
from typing import Literal

from fastapi import Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.responses import FileResponse
from pydantic import Field, field_validator, model_validator

from app.models import StrictModel, public_url, PipelineIn
from app.trade_synthesis import TradeSynthesisRequest
from app.trade_research import ResearchRequest
from app.trade_orchestrator import AutoTradeRequest
from app.store import get_store

PUBLIC_REQUESTS = deque()

FAMILIES = {
    "ai_credit": "AI credit and financing",
    "ai_capex_returns": "AI capex and monetization",
    "policy_siting": "Regulation and data-center siting",
    "rates_liquidity": "Rates and liquidity",
    "earnings_demand": "Earnings and demand",
    "market_breadth": "Market breadth and price confirmation",
    "valuation": "Valuation and positioning",
    "macro": "Macroeconomy",
    "geopolitical": "Geopolitical spillover",
    "other": "Other market signals",
}
Family = Literal["ai_credit", "ai_capex_returns", "policy_siting", "rates_liquidity",
                 "earnings_demand", "market_breadth", "valuation", "macro", "geopolitical", "other"]
Phase = Literal["structural", "leading", "catalyst", "confirmation", "counterevidence"]
Pressure = Literal["risk_up", "risk_down", "ambiguous"]
ClaimStatus = Literal["primary_document", "reported", "unverified", "disputed"]


def aware(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("Use a timezone-aware timestamp")
    return value


class MarketObservation(StrictModel):
    source_url: str = Field(min_length=10, max_length=2000)
    source_title: str = Field(min_length=4, max_length=300)
    publisher: str = Field(min_length=2, max_length=120)
    published_at: datetime
    event_at: datetime | None = None
    family: Family
    phase: Phase
    pressure: Pressure
    claim_status: ClaimStatus
    origin_group: str = Field(min_length=3, max_length=160)
    summary: str = Field(min_length=20, max_length=2500)
    tickers: list[str] = Field(default_factory=list, max_length=20)
    metric_code: str | None = Field(default=None, max_length=60, pattern=r"^[a-z][a-z0-9_]*$")
    metric_value: Decimal | None = None
    metric_unit: str | None = Field(default=None, max_length=40)
    caveats: str = Field(min_length=1, max_length=2000)

    _url = field_validator("source_url")(public_url)
    _time = field_validator("published_at", "event_at")(lambda v: aware(v) if v is not None else v)

    @field_validator("tickers")
    @classmethod
    def symbols(cls, values):
        if len(set(values)) != len(values) or any(
            not 1 <= len(v) <= 12 or not v.replace(".", "").replace("-", "").isalnum()
            or v != v.upper() for v in values
        ):
            raise ValueError("Tickers must be unique uppercase symbols")
        return values

    @model_validator(mode="after")
    def metric_and_future(self):
        if self.published_at > datetime.now(timezone.utc) + timedelta(minutes=10):
            raise ValueError("A source cannot be published in the future")
        if self.metric_value is not None and not self.metric_code:
            raise ValueError("Numeric observations require a metric code")
        if self.metric_value is not None and (not self.metric_value.is_finite() or self.metric_unit is None):
            raise ValueError("Metrics require a finite value and unit")
        return self


class MarketAmendment(StrictModel):
    observation_id: int = Field(gt=0)
    amendment_kind: Literal["canonical_link", "unavailable", "attribution", "retraction"]
    corrected_source_url: str | None = None
    rationale: str = Field(min_length=25, max_length=1500)

    @field_validator("corrected_source_url")
    @classmethod
    def validated_link(cls, value):
        return public_url(value) if value is not None else None

    @model_validator(mode="after")
    def validate_change(self):
        if self.amendment_kind == "canonical_link" and not self.corrected_source_url:
            raise ValueError("A canonical link amendment requires an inspected URL")
        if self.amendment_kind in ("unavailable", "retraction") and self.corrected_source_url:
            raise ValueError("Retraction/unavailable records do not pretend to have a new source link")
        return self


class MarketAssessment(StrictModel):
    as_of: datetime
    mode: Literal["live", "replay"]
    horizon_sessions: int = Field(ge=1, le=60)
    scenario: Literal["further_downside", "stabilization", "rebound"]
    confidence: Literal["low", "medium", "high"]
    thesis: str = Field(min_length=20, max_length=3000)
    countercase: str = Field(min_length=20, max_length=3000)
    falsification: str = Field(min_length=20, max_length=2000)
    next_check: str = Field(min_length=20, max_length=2000)
    coverage_gaps: str = Field(min_length=20, max_length=2000)
    evidence_ids: list[int] = Field(min_length=1, max_length=20)

    _as_of = field_validator("as_of")(aware)

    @model_validator(mode="after")
    def validate_refs_and_date(self):
        if len(set(self.evidence_ids)) != len(self.evidence_ids) or any(i <= 0 for i in self.evidence_ids):
            raise ValueError("Evidence IDs must be positive and unique")
        if self.as_of > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("Assessment is dated in the future")
        return self


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def expected_completed_market_session(as_of: datetime):
    """Conservative weekday-only US close check; holidays may be falsely stale."""
    ny = as_of.astimezone(ZoneInfo("America/New_York"))
    day = ny.date()
    if ny.weekday() < 5 and (ny.hour, ny.minute) < (16, 15):
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def eligible(row: dict, cutoff: datetime, mode: str) -> bool:
    """A source was knowable when published, but actually monitored only when collected."""
    try:
        if parse_time(row["published_at"]) > cutoff:
            return False
        return mode == "replay" or parse_time(row["collected_at"]) <= cutoff
    except (ValueError, KeyError, TypeError):
        return False


def radar(observations: list[dict], *, as_of: datetime, mode: str) -> dict:
    """Descriptive risk pressure ONLY. These are not empirical probabilities."""
    selected = [o for o in observations if eligible(o, as_of, mode)]
    grouped = defaultdict(set)
    counter = defaultdict(set)
    for observation in selected:
        family = observation.get("family")
        if family not in FAMILIES:
            continue
        # Multiple syndications of the same original source contribute only once
        # per family and direction; callers must document real lineage.
        group = observation.get("origin_group") or observation.get("source_url")
        if observation.get("pressure") == "risk_up":
            grouped[family].add(group)
        elif observation.get("pressure") == "risk_down":
            counter[family].add(group)
    affected = sorted(grouped)
    supporting = sorted(counter)
    if len(affected) >= 3 and "ai_credit" in grouped:
        posture = "MULTIPLE DOWNSIDE PRESSURES"
    elif len(affected) >= 2:
        posture = "RISING CAUTION"
    elif len(affected) == 1:
        posture = "CONCENTRATED WATCH"
    else:
        posture = "INSUFFICIENT OBSERVATIONS"
    # A few articles cannot establish that a real-time market data monitor is healthy.
    return {
        "posture": posture,
        "method": "Uncalibrated descriptive rule: family diversity, lineage-deduplicated; no price forecast",
        "risk_families": [{"code": k, "label": FAMILIES[k], "distinct_origin_groups": len(grouped[k])}
                          for k in affected],
        "counterevidence_families": [{"code": k, "label": FAMILIES[k], "distinct_origin_groups": len(counter[k])}
                                     for k in supporting],
        "eligible_observations": len(selected),
        "observations_from_future_excluded": len(observations) - len(selected),
        "actual_earlier_alert": False if mode == "replay" else None,
        "limitations": (
            "Replay shows historical public availability, NOT actual earlier collection or an alert."
            if mode == "replay" else
            "Coverage is incomplete. Without live price, credit and breadth feeds, bottom timing is unknown."
        ),
    }


async def snapshot(store, *, as_of: datetime, mode: str) -> dict:
    """Bounded query, and no per-user portfolio fields or private geop records."""
    observations = await store.rows("market_observations", limit=250)
    assessments = await store.rows("market_assessments", limit=100)
    feed_runs = await store.rows("market_feed_runs", limit=12)
    amendments = await store.rows("market_source_amendments", limit=100)
    watch_records = await store.rows("market_watch_signals", limit=80)
    technical_records = await store.rows("market_technical_snapshots", limit=20)
    forward_records = await store.rows("market_technical_outcomes", limit=40)
    # Apply only corrections/retractions actually recorded by the historical cutoff.
    # Original records remain immutable in the database for forensic inspection.
    corrections = {}
    for amendment in amendments:
        if parse_time(amendment["reviewed_at"]) <= as_of:
            corrections.setdefault(amendment["observation_id"], amendment)
    effective = []
    for source in observations:
        entry = dict(source)
        amendment = corrections.get(source["id"])
        if amendment:
            entry["source_review"] = amendment["amendment_kind"]
            entry["source_reviewed_at"] = amendment["reviewed_at"]
            entry["source_review_note"] = amendment["rationale"]
            if amendment["amendment_kind"] == "canonical_link":
                entry["original_source_url"] = source["source_url"]
                entry["source_url"] = amendment["corrected_source_url"]
            if amendment["amendment_kind"] == "retraction":
                continue
        effective.append(entry)
    valid = [o for o in effective if eligible(o, as_of, mode)]
    latest_by_metric = {}
    for o in valid:
        code = o.get("metric_code")
        if code and o.get("metric_value") is not None and code not in latest_by_metric:
            latest_by_metric[code] = {
                "code": code, "value": o["metric_value"], "unit": o.get("metric_unit"),
                "published_at": o["published_at"], "source_url": o["source_url"],
                "publisher": o["publisher"], "event_at": o.get("event_at")
            }
    recent_assessments = [
        a for a in assessments
        if a.get("mode") == mode and parse_time(a["as_of"]) <= as_of
        # Backfilled "live" assessments must not appear in earlier live histories.
        and (mode == "replay" or (
            a.get("created_at") is not None
            and parse_time(a["created_at"]) <= as_of
        ))
    ]
    latest_scenarios = {}
    for a in recent_assessments:
        key = a["scenario"]
        if key not in latest_scenarios:
            latest_scenarios[key] = a
    # Feed health is factual: a completed run and dated session, not an assertion of live quote coverage.
    feed_health = {}
    for run in feed_runs:
        # Replay is a historical research view, not a picture of later collection health.
        if mode == "replay" or not run.get("finished_at") or parse_time(run["finished_at"]) > as_of:
            continue
        for p in run.get("provider_results") or []:
            name = p.get("provider")
            if name in ("us_treasury", "cboe_vix", "cboe_vxn", "sec_edgar") and name not in feed_health:
                feed_health[name] = {
                    "status": p.get("status", "unknown"),
                    "latest_session": p.get("latest_session"),
                    "checked_at": run.get("finished_at"),
                    "run_status": run.get("status"),
                    "freshness": ("not_applicable" if name == "sec_edgar" else "stale_or_unknown" if not p.get("latest_session")
                        or datetime.fromisoformat(p["latest_session"]).date() <
                            expected_completed_market_session(as_of)
                        else "current_completed_session"),
                    "feed_type": "filing_metadata_index" if name == "sec_edgar" else "historical_daily_reference_not_live_quote",
                }
    # A source's older publication does not make a later-recorded watch retroactive.
    visible_source_ids = {int(o["id"]): o for o in valid}
    visible_watches = []
    for signal in watch_records:
        try:
            if parse_time(signal["first_detected_at"]) > as_of:
                continue
            if parse_time(signal["source_first_seen_at"]) > as_of:
                continue
            source = visible_source_ids.get(int(signal["observation_id"]))
            if not source:
                continue
            visible_watches.append({
                "id": signal["id"], "rule_id": signal["rule_id"],
                "direction": signal["direction"],
                "signal_type": "market_confirmation_only",
                "observed_session": signal["observed_session"],
                "observed_value": signal["observed_value"],
                "threshold": signal["threshold"], "unit": signal["unit"],
                "source_url": source["source_url"],
                "source_title": source["source_title"],
                "source_observation_id": signal["observation_id"],
                "source_first_seen_at": signal["source_first_seen_at"],
                "first_detected_at": signal["first_detected_at"],
                "limitations": signal["limitations"],
                "not_an_advance_alert": True,
            })
        except (ValueError, TypeError, KeyError):
            continue
    # A calculation's creation time is its actual first availability; no lookahead
    # in historical replay. The persisted raw bars never leave the server.
    technical_readouts = []
    for record in technical_records:
        try:
            if (parse_time(record["calculated_at"]) > as_of
                    or parse_time(record["captured_at"]) > as_of):
                continue
            source = visible_source_ids.get(record["source_observation_id"])
            if not source:
                continue
            overview = dict(record["analysis"])
            overview.pop("input_bars", None)
            technical_readouts.append({
                "id": record["id"], "symbol": record["symbol"],
                "instrument_kind": "volatility_index",
                "method_version": record["method_version"],
                "source_url": source["source_url"],
                "source_observation_id": source["id"],
                "source_first_collected_at": source["collected_at"],
                "calculated_at": record["calculated_at"],
                "last_completed_session": record["observed_session"],
                "analysis": overview,
                "not_a_trade_recommendation": True,
            })
        except (KeyError, ValueError, TypeError):
            continue
    unique_technical = {}
    for item in technical_readouts:
        unique_technical.setdefault(item["symbol"],item)
    forward_outcomes = []
    for result in forward_records:
        try:
            if parse_time(result["first_evaluated_at"]) > as_of:
                continue
            forward_outcomes.append({
                "id": result["id"],
                "symbol": result["symbol"],
                "baseline_session": result["baseline_session"],
                "target_session": result["target_session"],
                "horizon_sessions": result["horizon_sessions"],
                "baseline_close": result["baseline_close"],
                "target_close": result["target_close"],
                "observed_index_change_pct": result["observed_index_change_pct"],
                "first_evaluated_at": result["first_evaluated_at"],
                "label": "observed_index_movement_not_trading_return",
                "not_a_trade_pnl": True,
            })
        except (KeyError,TypeError,ValueError):
            continue
    return {
        "desk": "market", "as_of": as_of.isoformat(), "mode": mode,
        "radar": radar(effective, as_of=as_of, mode=mode),
        "observations": valid[:150],
        "latest_metrics": list(latest_by_metric.values())[:40],
        "feed_health": feed_health,
        "scenarios": latest_scenarios,
        "watch_signals": visible_watches[:30],
        "technical_readouts": list(unique_technical.values()),
        "forward_observed_outcomes": forward_outcomes[:20],
        "coverage": {
            "price_feed": "not_connected",
            "credit_spread_feed": "not_connected",
            "earnings_filings_feed": "not_connected",
            "research_intake": "manual_reviewed",
            "automatic_alert_delivery": "not_enabled",
            "calibrated_forecast": False,
            "sample_limit": 250,
        },
        "readout": "Scenario claims require signed-off evidence; absence of observations is not evidence of safety.",
    }


def register_market_routes(app, authorize, scheduler_bearer):
    """Mount independent market API and its read-only, public-source dashboard."""
    from app.main import fingerprint
    from pathlib import Path

    @app.get("/market", include_in_schema=False)
    async def market_home():
        return FileResponse(Path(__file__).with_name("market_dashboard.html"), media_type="text/html")

    @app.get("/v1/market/public-snapshot", include_in_schema=False)
    async def market_public_snapshot(
        mode: Literal["live", "replay"] = "live",
        as_of: datetime | None = None,
        store=Depends(get_store),
    ):
        cutoff = as_of or datetime.now(timezone.utc)
        if cutoff.utcoffset() is None or cutoff > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise HTTPException(422, "Supply a nonfuture timezone-aware as_of")
        # Global single-process throttle protects free service from accidental unbounded reads.
        now = time.monotonic()
        while PUBLIC_REQUESTS and PUBLIC_REQUESTS[0] < now - 60:
            PUBLIC_REQUESTS.popleft()
        if len(PUBLIC_REQUESTS) >= 60:
            raise HTTPException(429, "Public market view request limit reached")
        PUBLIC_REQUESTS.append(now)
        # Only public market research observations; never private portfolio or geopolitical tables.
        return await snapshot(store, as_of=cutoff, mode=mode)

    @app.get("/v1/market/snapshot", dependencies=[Depends(authorize)])
    async def market_snapshot(
        mode: Literal["live", "replay"] = "live",
        as_of: datetime | None = None,
        store=Depends(get_store),
    ):
        cutoff = as_of or datetime.now(timezone.utc)
        if cutoff.utcoffset() is None or cutoff > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise HTTPException(422, "Supply a nonfuture timezone-aware as_of")
        return await snapshot(store, as_of=cutoff, mode=mode)

    @app.get("/v1/market/amendments", dependencies=[Depends(authorize)])
    async def market_amendments(
        limit: int = Query(50, ge=1, le=100),
        before: int | None = Query(None, gt=0),
        store=Depends(get_store),
    ):
        return await store.rows("market_source_amendments", limit=limit, before=before)

    @app.post("/v1/market/amendments")
    async def record_market_amendment(
        entry: MarketAmendment,
        actor=Depends(authorize),
        store=Depends(get_store),
    ):
        if actor != "api:shared-key":
            raise HTTPException(403, "Only owner can correct source references")
        source = await store.rows("market_observations",limit=1,filters={"id":f"eq.{entry.observation_id}"})
        if not source:
            raise HTTPException(404,"Unknown market observation")
        body = {**entry.model_dump(mode="json"), "reviewed_by": "owner_research"}
        return await store.insert("market_source_amendments", body)

    @app.get("/v1/market/observations", dependencies=[Depends(authorize)])
    async def market_observations(
        limit: int = Query(50, ge=1, le=100),
        before: int | None = Query(None, gt=0),
        store=Depends(get_store),
    ):
        return await store.rows("market_observations", limit=limit, before=before)

    @app.get("/v1/market/technical-outcomes", dependencies=[Depends(authorize)])
    async def technical_outcome_history(
        limit: int = Query(30, ge=1, le=50),
        store=Depends(get_store),
    ):
        return await store.request("GET", "market_technical_outcomes", params={
            "select": "id,symbol,baseline_session,target_session,horizon_sessions,baseline_close,target_close,observed_index_change_pct,first_evaluated_at,outcome_label",
            "order": "id.desc", "limit": str(limit),
        })

    @app.get("/v1/market/technical-snapshots", dependencies=[Depends(authorize)])
    async def technical_history(
        limit: int = Query(25, ge=1, le=50),
        store=Depends(get_store),
    ):
        # Do not return the large raw input_bars or any personal account information.
        return await store.request("GET", "market_technical_snapshots", params={
            "select": "id,symbol,instrument_kind,observed_session,source_url,source_observation_id,calculated_at,method_version,bar_count,bars_sha256,analysis",
            "order": "id.desc", "limit": str(limit),
        })

    @app.get("/v1/market/watch-signals", dependencies=[Depends(authorize)])
    async def market_watch_signals(
        limit: int = Query(40, ge=1, le=100),
        before: int | None = Query(None, gt=0),
        store=Depends(get_store),
    ):
        return await store.rows("market_watch_signals", limit=limit, before=before)

    @app.get("/v1/market/assessments", dependencies=[Depends(authorize)])
    async def market_assessments(
        limit: int = Query(50, ge=1, le=100),
        before: int | None = Query(None, gt=0),
        store=Depends(get_store),
    ):
        return await store.rows("market_assessments", limit=limit, before=before)

    @app.post("/v1/market/observations")
    async def record_market_observation(
        entry: MarketObservation,
        actor=Depends(authorize),
        store=Depends(get_store),
    ):
        payload = entry.model_dump(mode="json")
        key = "market-url-v1:" + fingerprint({
            "source_url": entry.source_url, "family": entry.family,
            "phase": entry.phase, "metric_code": entry.metric_code,
            "event_at": payload.get("event_at"), "published_at": payload["published_at"],
        })
        return await store.rpc("market_record_observation_v1", {
            "payload": payload, "p_key": key, "p_actor": actor
        })


    @app.get("/v1/market/feeds/runs", dependencies=[Depends(authorize)])
    async def market_feed_runs(
        limit: int = Query(30, ge=1, le=100),
        before: int | None = Query(None, gt=0),
        store=Depends(get_store),
    ):
        return await store.rows("market_feed_runs", limit=limit, before=before)

    @app.post("/v1/market/feeds/run")
    async def market_feed_manual(
        value: PipelineIn,
        actor=Depends(authorize),
        store=Depends(get_store),
    ):
        from app.market_feeds import run_market_feed
        return await run_market_feed(store, value.run_key, "manual")

    @app.post("/v1/market/feeds/scheduled")
    async def market_feed_scheduled(
        value: PipelineIn,
        credentials: HTTPAuthorizationCredentials | None = Depends(scheduler_bearer),
        store=Depends(get_store),
    ):
        # Reuse the existing, dedicated scheduler-only Vault secret.
        # It does NOT authorize general API or research writes.
        import hashlib
        import os
        import secrets
        from app.market_feeds import run_market_feed
        expected = os.getenv("SIGNAL_DESK_SCHEDULER_KEY_SHA256", "")
        supplied = credentials.credentials if credentials and credentials.scheme.lower() == "bearer" else ""
        if len(expected) != 64 or not secrets.compare_digest(
            hashlib.sha256(supplied.encode()).hexdigest(), expected
        ):
            raise HTTPException(401, "Invalid scheduler credential")
        return await run_market_feed(store, value.run_key, "supabase_cron")


    @app.get("/v1/market/data-sources/status", dependencies=[Depends(authorize)])
    async def market_data_source_status():
        from app.market_quotes import configuration
        from app.market_sec import configured_user_agent
        return {
            "stock_data": configuration(),
            "sec_edgar": {
                "configured": configured_user_agent() is not None,
                "scope": "official_filing_metadata_only",
                "contact_value_exposed": False,
            },
            "ai_issuer_cds": "not_connected",
            "broad_market_breadth": "not_connected",
            "earnings_calendar": "not_connected",
            "automatic_broker_orders": "disabled",
        }

    @app.get("/v1/market/vendor-quote/{ticker}", dependencies=[Depends(authorize)])
    async def market_vendor_quote(ticker: str, actor=Depends(authorize)):
        if actor != "api:shared-key":
            raise HTTPException(403, "Only owner can query licensed vendor data")
        from app.market_quotes import vendor_snapshot
        try:
            return await vendor_snapshot(ticker)
        except ValueError:
            raise HTTPException(422, "Invalid ticker")

    @app.get("/v1/market/vendor-technical/{ticker}")
    async def vendor_technical_route(ticker: str, actor=Depends(authorize)):
        if actor != "api:shared-key":
            raise HTTPException(403, "Owner authorization required for licensed stock market data")
        from app.market_quotes import vendor_technical_research
        try:
            return await vendor_technical_research(ticker)
        except ValueError:
            raise HTTPException(422, "Invalid ticker")

    @app.get("/v1/market/trade-synthesis/status", dependencies=[Depends(authorize)])
    async def trade_synthesis_status():
        """Market-wide readiness; this endpoint never returns positions or account data."""
        return {
            "mode": "research_proposals_only",
            "live_stock_quote_provider": __import__("app.market_quotes", fromlist=["configuration"]).configuration(),
            "ai_issuer_credit_spread_provider": "not_connected",
            "breadth_provider": "not_connected",
            "private_portfolio_sync": "not_connected",
            "broker_order_execution": "disabled",
            "requirements": [
                "An owner-provided, fresh account-level NAV, cash and positions snapshot",
                "Current regular-session stock or ETF quotes, SMA20/SMA50/ATR14 and liquidity",
                "Dated AI credit, market breadth and Nasdaq volatility indicators from distinct origins",
                "At least one real stored market observation to cite",
                "Manual owner review of any generated ticket before any brokerage action",
            ],
            "no_trade_default": True,
            "note": "No execution, no storage of positions, no implied vendor verification; recommendations are conditional.",
        }

    @app.get("/v1/market/trade-synthesis/history")
    async def private_trade_history(
        limit: int = Query(20, ge=1, le=100),
        actor=Depends(authorize),
        store=Depends(get_store),
    ):
        if actor != "api:shared-key":
            raise HTTPException(403, "Private trade decisions require the owner credential")
        return await store.rpc("market_decision_history_v1", {"p_limit": limit})

    @app.post("/v1/market/trade-synthesis/research")
    async def trade_research(
        entry: ResearchRequest,
        actor=Depends(authorize),
        store=Depends(get_store),
    ):
        if actor != "api:shared-key":
            raise HTTPException(403, "Only the owner may request model research")
        from app.trade_research import propose_research
        import httpx
        try:
            return await propose_research(store, entry)
        except (ValueError, httpx.HTTPError):
            raise HTTPException(502, "Market research model did not produce a valid sourced shortlist")

    @app.post("/v1/market/trade-synthesis/auto")
    async def trade_synthesis_auto(
        entry: AutoTradeRequest,
        actor=Depends(authorize),
        store=Depends(get_store),
    ):
        if actor != "api:shared-key":
            raise HTTPException(403, "Only the owner may submit private account data for synthesis")
        import httpx
        from app.trade_orchestrator import run_auto_synthesis
        ids = ",".join(str(i) for i in entry.trade.evidence_ids)
        evidence = await store.request(
            "GET", "market_observations",
            params={"select": "id,published_at", "id": f"in.({ids})", "limit": "20"}
        )
        if len(evidence) != len(entry.trade.evidence_ids):
            raise HTTPException(409, "Auto-synthesis requires existing research references")
        try:
            result = await run_auto_synthesis(store, entry)
            from app.trade_audit import persist_trade_result
            method = "ai_researched" if result.get("research", {}).get("status") == "RESEARCH_ONLY" else "rule_based"
            return await persist_trade_result(store, result, method=method)
        except (ValueError, httpx.HTTPError):
            raise HTTPException(502, "Sourced trade research did not complete")

    @app.post("/v1/market/trade-synthesis")
    async def trade_synthesis(
        entry: TradeSynthesisRequest,
        actor=Depends(authorize),
        store=Depends(get_store),
    ):
        if actor != "api:shared-key":
            raise HTTPException(403, "Only the owner may submit account data for synthesis")
        from app.trade_synthesis import synthesize
        ids = ",".join(str(i) for i in entry.evidence_ids)
        evidence = await store.request(
            "GET", "market_observations",
            params={"select": "id,published_at", "id": f"in.({ids})", "limit": "20"},
        )
        if len(evidence) != len(entry.evidence_ids):
            raise HTTPException(409, "Trade synthesis requires existing cited market observations")
        from app.trade_audit import persist_trade_result
        result = synthesize(entry)
        return await persist_trade_result(store, result, method="rule_based")

    @app.post("/v1/market/assessments")
    async def record_market_assessment(
        entry: MarketAssessment,
        actor=Depends(authorize),
        store=Depends(get_store),
    ):
        payload = entry.model_dump(mode="json")
        # The RPC checks this invariant atomically again; this precheck returns
        # a meaningful conflict instead of revealing database error details.
        ids = ",".join(str(i) for i in entry.evidence_ids)
        rows = await store.request(
            "GET", "market_observations",
            params={"select": "id,published_at,collected_at", "id": f"in.({ids})", "limit": "20"}
        )
        if len(rows) != len(entry.evidence_ids) or any(
            not eligible(o, entry.as_of, entry.mode) for o in rows
        ):
            raise HTTPException(409, "Evidence was not available at the requested as-of time")
        return await store.rpc("market_record_assessment_v1", {
            "payload": payload, "p_key": fingerprint(payload), "p_actor": actor
        })
