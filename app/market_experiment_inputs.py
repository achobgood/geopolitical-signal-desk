"""Construct experiments from server-owned immutable research records only.

The public/request boundary takes no labels, prices, feature values, split dates,
or performance assertions. Historical bars first fetched today cannot manufacture
point-in-time training data. Blocked studies record planned hypotheses, not tests.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from app.market_calendar import is_expected_market_session, expected_completed_session
from app.market_experiments import (
    MIN_HOLDOUT, MIN_TRAIN, MAX_ROWS, NY, VERSION, _time, candidate_configs, fingerprint,
    run_experiments,
)


async def _all_rows(store, table, *, filters, maximum):
    result, before = [], None
    while True:
        page = await store.rows(table, limit=200, before=before, filters=filters)
        if not isinstance(page, list):
            raise ValueError("Stored experiment source is not a row collection")
        if not page:
            return result
        result.extend(page)
        if len(result) > maximum:
            raise ValueError("Research source exceeds explicit bound; never silently truncate an evaluation cohort")
        cursor = page[-1]["id"]
        if before is not None and cursor >= before:
            raise ValueError("Research source pagination did not advance")
        before = cursor
        if len(page) < 200:
            return result


def _previous_session(day: date) -> date:
    for _ in range(12):
        day -= timedelta(days=1)
        if is_expected_market_session(day):
            return day
    raise ValueError("No previous expected exchange session")


def _captured_features(forecast: dict, capture: dict | None, cutoff: datetime) -> dict:
    if not capture or capture.get("symbol") != forecast["symbol"] or capture.get("provider") != "alpaca" or capture.get("feed") != "sip" or capture.get("adjustment") != "split":
        return {}
    if capture.get("observed_session") != forecast["reference_session"]:
        return {}
    raw = forecast.get("features", {})
    if raw.get("source_price_capture_id") != capture["id"] or raw.get("adjustment") != "split":
        return {}
    first = _time(capture["captured_at"])
    published = datetime.combine(date.fromisoformat(forecast["reference_session"]), time(16, 15), NY)
    # The feature snapshot was generated from the immutable capture by the trusted
    # issuance service. Use the database capture timestamp, never a caller date.
    usable = max(first, published)
    return {name: {"value": raw[name], "published_at": published.isoformat(),
                   "first_collected_at": first.isoformat(), "usable_at": usable.isoformat(),
                   "source_ids": ["market_forecast_prices:" + str(capture["id"])],
                   "source_url": capture["source_url"], "bars_sha256": capture["bars_sha256"]}
            for name in ("return_1", "return_5") if name in raw}


def stored_session_rows(forecasts: list[dict], evaluations: list[dict], captures: list[dict], *, instrument: str) -> list[dict]:
    """Preserve every expected session and unresolved or revised outcome."""
    by_target = {}
    for forecast in sorted(forecasts, key=lambda row: row["id"]):
        if forecast["symbol"] != instrument or forecast.get("model_version") != "momentum-1-research-v1":
            continue
        target = forecast["target_session"]
        if target in by_target:
            raise ValueError("Ambiguous multiple original forecast versions in experiment cohort")
        by_target[target] = forecast
    if not by_target:
        return []
    latest = {}
    for evaluation in sorted(evaluations, key=lambda row: row["id"], reverse=True):
        latest.setdefault(evaluation["forecast_id"], evaluation)
    first_observed = {}
    for evaluation in evaluations:
        if evaluation.get("status") != "OBSERVED" or evaluation.get("target_close") is None:
            continue
        key = (evaluation["forecast_id"], evaluation["actual_direction"], Decimal(str(evaluation["target_close"])))
        previous = first_observed.get(key)
        if previous is None or _time(evaluation["evaluated_at"]) < _time(previous["evaluated_at"]):
            first_observed[key] = evaluation
    by_capture = {row["id"]: row for row in captures}
    first, final = map(date.fromisoformat, (min(by_target), max(by_target)))
    day, rows = first, []
    while day <= final:
        if not is_expected_market_session(day):
            day += timedelta(days=1)
            continue
        forecast = by_target.get(day.isoformat())
        previous = _previous_session(day)
        cutoff = datetime.combine(previous, time(19), NY)
        row = {"session": day.isoformat(), "cutoff_at": cutoff.isoformat(),
               "outcome": None, "label_available_at": None, "features": {},
               "adjustment": "split", "data_basis": "POINT_IN_TIME_CAPTURED",
               "missing_reason": "No immutable forecast was recorded for this expected session"}
        if forecast:
            if forecast["reference_session"] != previous.isoformat() or _time(forecast["cutoff_at"]) != cutoff or _time(forecast["issued_at"]) > cutoff:
                raise ValueError("Stored forecast violated previous-evening next-session contract")
            capture = by_capture.get(forecast.get("reference_capture_id"))
            row["features"] = _captured_features(forecast, capture, cutoff)
            row["forecast_id"] = forecast["id"]
            row["missing_reason"] = None if row["features"] else "Original forecast has no trusted available equity feature capture"
            evaluation = latest.get(forecast["id"])
            if evaluation and evaluation["status"] == "OBSERVED":
                # OBSERVED directions were computed inside the database using
                # exact immutable reference and target captures, not supplied here.
                earliest = first_observed.get((forecast["id"], evaluation["actual_direction"],
                                              Decimal(str(evaluation["target_close"])))) if evaluation.get("target_close") is not None else None
                first_valid = earliest or evaluation
                row["outcome"] = evaluation["actual_direction"]
                row["label_available_at"] = first_valid["evaluated_at"]
                row["label_source_capture_id"] = first_valid["capture_id"]
                row["label_evaluation_id"] = first_valid["id"]
                row["latest_label_evaluation_id"] = evaluation["id"]
            elif evaluation:
                row["missing_reason"] = "Latest outcome state: " + evaluation["status"]
        rows.append(row)
        if len(rows) > MAX_ROWS:
            raise ValueError("Expected-session cohort exceeds bounded research window")
        day += timedelta(days=1)
    return rows


async def run_stored_experiments(store, *, study_id: str, instrument: str) -> dict:
    if instrument not in ("SPY", "QQQ"):
        raise ValueError("Only SPY and QQQ experiments supported")
    if not isinstance(study_id, str) or not 8 <= len(study_id) <= 120:
        raise ValueError("Study identity must contain 8–120 characters")

    async def emit(event):
        return await store.rpc("market_experiment_event_v1", {"p": event})

    completed = expected_completed_session(datetime.now(timezone.utc) - timedelta(minutes=15))
    forecasts = await _all_rows(store, "market_forecasts", filters={"symbol": "eq." + instrument,
                                "target_session": "lte." + completed.isoformat()}, maximum=MAX_ROWS)
    evaluations = []
    capture_ids = set()
    for start in range(0, len(forecasts), 100):
        batch = forecasts[start:start+100]
        ids = ",".join(str(row["id"]) for row in batch)
        evaluations.extend(await _all_rows(store, "market_forecast_evaluations", filters={"forecast_id": "in.(" + ids + ")"}, maximum=MAX_ROWS*5))
        capture_ids.update(row["reference_capture_id"] for row in batch if row.get("reference_capture_id"))
    captures = []
    sorted_ids = sorted(capture_ids)
    for start in range(0, len(sorted_ids), 100):
        ids = ",".join(map(str, sorted_ids[start:start+100]))
        captures.extend(await store.rows("market_forecast_prices", limit=100, filters={
            "id": "in.(" + ids + ")", "select": "id,symbol,provider,feed,adjustment,source_url,captured_at,observed_session,bars_sha256"}))
    rows = stored_session_rows(forecasts, evaluations, captures, instrument=instrument)
    available = sum(bool(row["features"]) and all(_time(feature["usable_at"]) <= _time(row["cutoff_at"]) for feature in row["features"].values()) for row in rows)
    holdout_n = max(MIN_HOLDOUT, (len(rows) + 4) // 5)
    development_n = len(rows) - holdout_n
    development_labeled = sum(row.get("outcome") in ("UP", "DOWN") and bool(row["features"])
                              for row in rows[:max(0, development_n)])
    holdout_labeled = sum(row.get("outcome") in ("UP", "DOWN") for row in rows[max(0, development_n):])
    if available < MIN_TRAIN or development_n < MIN_TRAIN*2 or development_labeled < MIN_TRAIN*2 or holdout_labeled < MIN_HOLDOUT:
        # One latest real capture can explain the block but cannot turn today's
        # discovery of old prices into previously available historical features.
        latest_capture = await store.rows("market_forecast_prices", limit=1, filters={
            "symbol": "eq." + instrument,
            "select": "id,symbol,captured_at,observed_session,bars_sha256"})
        result = {"status": "BLOCKED_INSUFFICIENT_POINT_IN_TIME_DATA", "instrument": instrument,
                  "version": VERSION, "planned_hypotheses": candidate_configs(),
                  "tested_hypotheses": 0, "forecast_session_rows": len(rows),
                  "available_point_in_time_feature_rows": available, "minimum_training_rows": MIN_TRAIN,
                  "minimum_holdout_rows": MIN_HOLDOUT, "minimum_development_rows": MIN_TRAIN*2,
                  "labeled_development_rows": development_labeled, "labeled_holdout_rows": holdout_labeled,
                  "eligible_completed_session_through": completed.isoformat(),
                  "latest_actual_price_capture": latest_capture[0] if latest_capture else None,
                  "retrospective_history_basis": "RETROSPECTIVE_RECONSTRUCTED" if latest_capture else "NO_EQUITY_CAPTURE",
                  "missing_reasons": ["Insufficient immutable pre-cutoff equity features and later observed next-session outcomes.",
                                      "Historical bars first captured now cannot be treated as historical point-in-time observations.",
                                      "Macro, credit, technical-volatility, and intelligence factors require independently timestamped numeric feature contracts."],
                  "result": None, "selected_strategy": None, "predictive_edge_established": False,
                  "holdout_reserved": False}
        await emit({"study_id": study_id, "event_key": "study_blocked", "kind": "study_blocked", "payload": result})
        return result
    code_hash = sha256(Path(__file__).read_bytes() + Path(__file__).with_name("market_experiments.py").read_bytes()).hexdigest()
    return await run_experiments(rows, study_id=study_id, instrument=instrument,
                                dataset_id="immutable-forecast-cohort:" + instrument + ":" + fingerprint([row.get("forecast_id") for row in rows]),
                                code_hash=code_hash, development_end=rows[development_n-1]["session"],
                                holdout_start=rows[development_n]["session"], emit=emit)
