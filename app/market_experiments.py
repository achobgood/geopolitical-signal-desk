"""Auditable chronological research; a failed untouched holdout stays failed.

No probability is presented as calibrated. Prices collected after a historical
cutoff are never accepted as point-in-time features. An async append callback
must durably reserve the protocol/holdout before this runner evaluates anything.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import inspect
import json
import math
import random
from typing import Callable
from zoneinfo import ZoneInfo

from app.market_calendar import is_expected_market_session

VERSION = "chronological-strategy-experiments-v1"
NY = ZoneInfo("America/New_York")
TECHNICAL = ("return_1", "return_5", "sma20_gap", "rsi14", "atr14_pct", "vix_change_1", "vxn_change_1")
MACRO = ("treasury_10y_change", "treasury_2y_change", "credit_spread_change")
INTELLIGENCE = ("geopolitical_score", "ai_financing_score")
BASELINES = ("always_up", "momentum_1", "training_majority")
MIN_TRAIN = 60
MIN_HOLDOUT = 100
FOLD_SESSIONS = 20
MAX_ROWS = 1500


def fingerprint(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _time(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Feature and label times must be ISO timestamps")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("Timezone-aware availability timestamps required")
    return result.astimezone(timezone.utc)


def candidate_configs() -> list[dict]:
    """The fixed search space is registered before development results are read."""
    result = [{"id": "A_always_up", "family": "A", "method": "always_up", "features": [],
               "hypothesis": "The unconditional upward market tendency is a useful direction baseline."}]
    for window in (1, 5):
        result.append({"id": f"B_momentum_{window}", "family": "B", "method": "momentum",
                       "features": [f"return_{window}"], "window_sessions": window, "threshold": 0.0,
                       "hypothesis": f"The sign of the previous {window}-session return persists for one session."})
    for family, features, hypothesis in (
        ("C", TECHNICAL, "Price technicals and volatility add next-session information."),
        ("D", MACRO, "Prior Treasury and credit changes add next-session information."),
        ("E", INTELLIGENCE, "Pre-existing geopolitical and financing signals add next-session information."),
        ("F", TECHNICAL + MACRO + INTELLIGENCE, "Combining factors improves on the inexpensive baselines."),
    ):
        for ridge in (0.1, 1.0):
            result.append({"id": f"{family}_ridge_{ridge}", "family": family, "method": "logistic",
                           "features": list(features), "ridge": ridge, "epochs": 80,
                           "learning_rate": 0.15, "decision_threshold": 0.5,
                           "hypothesis": hypothesis})
    return result


def _validate_rows(rows: list[dict]) -> None:
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise ValueError(f"Research requires 1–{MAX_ROWS} explicit session rows")
    last_session = last_cutoff = None
    adjustments = set()
    for row in rows:
        session = date.fromisoformat(row["session"])
        cutoff = _time(row["cutoff_at"])
        local = cutoff.astimezone(NY)
        if not is_expected_market_session(session) or not is_expected_market_session(local.date()) or local.date() >= session or (local.hour, local.minute, local.second, local.microsecond) != (19, 0, 0, 0):
            raise ValueError("Rows require a previous-evening 19:00 Eastern cutoff and later weekday target")
        next_day = local.date()
        for _ in range(12):
            next_day += timedelta(days=1)
            if is_expected_market_session(next_day):
                break
        if session != next_day:
            raise ValueError("Target must be the very next expected exchange session")
        if last_session is not None and (session <= last_session or cutoff <= last_cutoff):
            raise ValueError("Sessions and cutoffs must be strictly chronological and unique")
        if last_session is not None and local.date() != last_session:
            raise ValueError("Missing exchange sessions must be explicit unresolved rows, never silently removed")
        last_session, last_cutoff = session, cutoff
        if row.get("data_basis") not in ("POINT_IN_TIME_CAPTURED", "RETROSPECTIVE_RECONSTRUCTED"):
            raise ValueError("Explicit point-in-time or reconstructed data basis required")
        if row.get("adjustment") not in ("split", "all"):
            raise ValueError("Explicit historical adjustment required")
        adjustments.add(row["adjustment"])
        outcome = row.get("outcome")
        if outcome not in ("UP", "DOWN", "FLAT", None):
            raise ValueError("Outcome must be UP, DOWN, FLAT, or unresolved null")
        if outcome is not None:
            available = _time(row.get("label_available_at"))
            if available < datetime.combine(session, time(16), NY):
                raise ValueError("Outcome label cannot predate the target session close")
            if available > datetime.now(timezone.utc):
                raise ValueError("Future outcome labels are forbidden")
        elif row.get("label_available_at") is not None:
            _time(row["label_available_at"])
        if not isinstance(row.get("features"), dict):
            raise ValueError("Explicit feature records required")
        for feature in row["features"].values():
            if not isinstance(feature, dict):
                raise ValueError("Features require value and availability provenance")
            value = float(feature["value"])
            if not math.isfinite(value) or abs(value) > 1e12:
                raise ValueError("Nonfinite research feature")
            published = _time(feature["published_at"])
            collected = _time(feature["first_collected_at"])
            usable = _time(feature["usable_at"])
            if usable < max(published, collected):
                raise ValueError("Earliest usable time cannot precede publication or first collection")
            ids = feature.get("source_ids")
            if not isinstance(ids, list) or not ids or len(ids) != len(set(map(str, ids))):
                raise ValueError("Features require unique original source IDs")
    if len(adjustments) != 1:
        raise ValueError("Never mix historical adjustment methods within a study")


def _vector(row: dict, names: list[str]) -> list[float] | None:
    cutoff = _time(row["cutoff_at"])
    values = []
    for name in names:
        feature = row["features"].get(name)
        if feature is None or max(_time(feature[x]) for x in ("published_at", "first_collected_at", "usable_at")) > cutoff:
            return None
        values.append(float(feature["value"]))
    return values


def _direction(value: float) -> str:
    return "UP" if value > 0 else "DOWN" if value < 0 else "ABSTAIN"


def _train_rows(rows: list[dict], cutoff: str, names: list[str]) -> list[tuple[list[float], int]]:
    timestamp = _time(cutoff)
    return [(vector, int(row["outcome"] == "UP")) for row in rows
            if row.get("outcome") in ("UP", "DOWN") and _time(row["label_available_at"]) < timestamp
            and (vector := _vector(row, names)) is not None]


def _fit(config: dict, training: list[dict], cutoff: str, min_train: int):
    if config["method"] != "logistic":
        return None
    samples = _train_rows(training, cutoff, config["features"])
    if len(samples) < min_train or len({y for _, y in samples}) < 2:
        return None
    width = len(config["features"])
    means = [sum(x[j] for x, _ in samples) / len(samples) for j in range(width)]
    scales = [math.sqrt(sum((x[j] - means[j]) ** 2 for x, _ in samples) / len(samples)) or 1.0 for j in range(width)]
    x = [[1.0] + [(v - means[j]) / scales[j] for j, v in enumerate(vector)] for vector, _ in samples]
    y = [value for _, value in samples]
    weights = [0.0] * (width + 1)
    for _ in range(config["epochs"]):
        gradient = [0.0] * len(weights)
        for vector, outcome in zip(x, y):
            z = max(-35, min(35, sum(a * b for a, b in zip(weights, vector))))
            error = (1 / (1 + math.exp(-z))) - outcome
            for j, value in enumerate(vector):
                gradient[j] += error * value
        weights = [w - config["learning_rate"] * (gradient[j] / len(samples) + (config["ridge"] * w if j else 0))
                   for j, w in enumerate(weights)]
    return means, scales, weights


def _predict(config: dict, row: dict, fitted) -> str:
    if config["method"] == "always_up":
        return "UP"
    vector = _vector(row, config["features"])
    if vector is None:
        return "ABSTAIN"
    if config["method"] == "momentum":
        return _direction(vector[0])
    if fitted is None:
        return "ABSTAIN"
    means, scales, weights = fitted
    score = weights[0] + sum(w * (v - mean) / scale for w, v, mean, scale in zip(weights[1:], vector, means, scales))
    return _direction(score)


def _majority(training: list[dict], cutoff: str) -> str:
    labels = [y for _, y in _train_rows(training, cutoff, [])]
    if not labels:
        return "ABSTAIN"
    return "UP" if sum(labels) * 2 >= len(labels) else "DOWN"


def score_predictions(rows: list[dict], predictions: list[str]) -> dict:
    if len(rows) != len(predictions) or any(p not in ("UP", "DOWN", "ABSTAIN") for p in predictions):
        raise ValueError("One valid direction or abstention per session is required")
    resolved = [(r["outcome"], p) for r, p in zip(rows, predictions) if r.get("outcome") in ("UP", "DOWN")]
    scored = [(y, p) for y, p in resolved if p != "ABSTAIN"]
    tp = sum(y == p == "UP" for y, p in scored)
    tn = sum(y == p == "DOWN" for y, p in scored)
    fp = sum(y == "DOWN" and p == "UP" for y, p in scored)
    fn = sum(y == "UP" and p == "DOWN" for y, p in scored)
    n, correct = len(scored), tp + tn
    accuracy = correct / n if n else None
    if n:
        z = 1.959963984540054
        mid = (accuracy + z*z / (2*n)) / (1 + z*z / n)
        half = z * math.sqrt(accuracy * (1 - accuracy) / n + z*z / (4*n*n)) / (1 + z*z / n)
        interval = [max(0.0, mid-half), min(1.0, mid+half)]
    else:
        interval = None
    return {
        "total_sessions": len(rows), "directional_outcomes": len(resolved), "scored_predictions": n,
        "flat_outcomes": sum(r.get("outcome") == "FLAT" for r in rows),
        "unresolved_outcomes": sum(r.get("outcome") is None for r in rows),
        "abstentions": predictions.count("ABSTAIN"),
        "abstention_rate": predictions.count("ABSTAIN") / len(rows) if rows else None,
        "coverage_on_directional_outcomes": n / len(resolved) if resolved else None,
        "directional_accuracy": accuracy, "accuracy_wilson_95": interval,
        "full_directional_session_correct_rate": correct / len(resolved) if resolved else None,
        "balanced_accuracy": ((tp / (tp+fn) + tn / (tn+fp)) / 2) if tp+fn and tn+fp else None,
        "false_positive_rate": fp / (fp+tn) if fp+tn else None,
        "false_negative_rate": fn / (fn+tp) if fn+tp else None,
        "confusion": {"true_up": tp, "true_down": tn, "false_up": fp, "false_down": fn},
        "brier_score": None, "calibration_error": None, "probability_status": "NOT_CALIBRATED",
        "uncertainty_note": "Wilson interval describes directional observations; temporal dependence can reduce effective sample size.",
    }


def _walk_forward(config: dict, rows: list[dict], min_train: int) -> tuple[list[dict], list[str], dict]:
    evaluated, predictions, baseline = [], [], {key: [] for key in BASELINES}
    for start in range(min_train, len(rows), FOLD_SESSIONS):
        training, fold = rows[:start], rows[start:start+FOLD_SESSIONS]
        fitted = _fit(config, training, fold[0]["cutoff_at"], min_train)
        for row in fold:
            evaluated.append(row)
            predictions.append(_predict(config, row, fitted))
            baseline["always_up"].append("UP")
            baseline["momentum_1"].append(_predict({"method": "momentum", "features": ["return_1"]}, row, None))
            # The majority fit is frozen at the fold boundary, like fitted factors.
            baseline["training_majority"].append(_majority(training, fold[0]["cutoff_at"]))
    return evaluated, predictions, baseline


def _comparison(rows: list[dict], predictions: list[str], baseline: list[str], *, uncertainty=False) -> dict:
    indexes = [i for i, row in enumerate(rows) if row.get("outcome") in ("UP", "DOWN")
               and predictions[i] != "ABSTAIN" and baseline[i] != "ABSTAIN"]
    matched_rows = [rows[i] for i in indexes]
    model = score_predictions(matched_rows, [predictions[i] for i in indexes])
    benchmark = score_predictions(matched_rows, [baseline[i] for i in indexes])
    improvement = None if not indexes else model["directional_accuracy"] - benchmark["directional_accuracy"]
    lower = None
    if uncertainty and indexes:
        # Moving blocks preserve short-range chronology; no shuffled training or test split.
        differences = {i: int(predictions[i] == rows[i]["outcome"]) - int(baseline[i] == rows[i]["outcome"]) for i in indexes}
        rng = random.Random(7319)
        block = min(5, len(rows))
        simulated = []
        for _ in range(1200):
            draw = []
            timeline = []
            while len(timeline) < len(rows):
                start = rng.randrange(len(rows) - block + 1)
                timeline.extend(range(start, start+block))
            draw = [differences[i] for i in timeline[:len(rows)] if i in differences]
            simulated.append(sum(draw) / len(draw) if draw else -1.0)
        # 98.33% one-sided bound: Bonferroni across the three fixed baselines.
        lower = sorted(simulated)[int(len(simulated) * (0.05 / len(BASELINES)))]
    return {"matched_sessions": len(indexes), "candidate": model, "baseline": benchmark,
            "accuracy_improvement": improvement, "paired_improvement_lower_bound": lower,
            "uncertainty_method": "five-session moving-block bootstrap; 1200 draws; one-sided Bonferroni 5%/3" if uncertainty else None,
            "uncertainty_limitations": "Five-session blocks assume longer serial dependence is negligible; this is not proof of a prospective edge." if uncertainty else None}


def _session_results(rows: list[dict], predictions: list[str], baselines: dict) -> list[dict]:
    return [{"session": row["session"], "cutoff_at": row["cutoff_at"],
             "direction": predictions[i], "observed_outcome": row.get("outcome"),
             "label_available_at": row.get("label_available_at"),
             "data_basis": row["data_basis"],
             "benchmarks": {name: values[i] for name, values in baselines.items()}}
            for i, row in enumerate(rows)]


async def run_experiments(rows: list[dict], *, study_id: str, dataset_id: str, code_hash: str, instrument: str,
                          development_end: str, holdout_start: str, emit: Callable,
                          min_train: int = MIN_TRAIN, min_holdout: int = MIN_HOLDOUT) -> dict:
    """Persist registrations before execution, freeze one winner, then consume holdout.

    `emit(event)` MUST await durable append and enforce atomic single-use holdout
    reservation including overlapping sessions across dataset aliases. Exceptions
    propagate and stop execution. No runner can prove durable state by itself.
    """
    _validate_rows(rows)
    if instrument not in ("SPY", "QQQ"):
        raise ValueError("Only SPY or QQQ experiments supported")
    if not isinstance(study_id, str) or not study_id or not isinstance(dataset_id, str) or not dataset_id:
        raise ValueError("Stable study and dataset identities required")
    if not isinstance(code_hash, str) or len(code_hash) != 64 or any(c not in "0123456789abcdef" for c in code_hash):
        raise ValueError("Exact SHA-256 code/configuration hash required")
    if type(min_train) is not int or type(min_holdout) is not int or min_train < MIN_TRAIN or min_holdout < MIN_HOLDOUT:
        raise ValueError("The protocol minimum samples cannot be weakened")
    dev_end, hold_start = date.fromisoformat(development_end), date.fromisoformat(holdout_start)
    if dev_end >= hold_start:
        raise ValueError("Final holdout must begin strictly after development")
    development = [r for r in rows if date.fromisoformat(r["session"]) <= dev_end]
    holdout = [r for r in rows if date.fromisoformat(r["session"]) >= hold_start]
    gap = [r["session"] for r in rows if dev_end < date.fromisoformat(r["session"]) < hold_start]
    if len(development) <= min_train or not holdout:
        raise ValueError("Need development evaluation and a distinct untouched chronological holdout")
    configs = candidate_configs()
    protocol = {
        "version": VERSION, "instrument": instrument, "dataset_id": dataset_id, "dataset_sha256": fingerprint(rows), "code_hash": code_hash,
        "development_end": development_end, "holdout_start": holdout_start,
        "holdout_sessions": [r["session"] for r in holdout], "purged_gap_sessions": gap,
        "min_train": min_train, "min_holdout": min_holdout, "fold_sessions": FOLD_SESSIONS,
        "candidate_configs": configs, "baselines": list(BASELINES), "minimum_directional_coverage": 0.8,
        "minimum_each_outcome_class": 20,
        "selection_metric": "development matched-baseline minimum accuracy improvement, then balanced accuracy; fixed candidate-id tie break",
        "retrospective": True, "actual_historical_forecasts": False,
        "data_basis": sorted({r["data_basis"] for r in rows}),
        "holdout_policy": "Single winner frozen before holdout; failed holdout rejected with no runner-up substitution or retuning.",
    }

    async def append(key: str, kind: str, payload: dict):
        event = {"study_id": study_id, "event_key": key, "kind": kind, "payload": payload}
        result = emit(event)
        if not inspect.isawaitable(result):
            raise TypeError("Experiment emit must be async and durably persist each event")
        await result

    await append("protocol_registered", "protocol_registered", protocol)
    results = []
    for config in configs:
        await append("trial_registered:" + config["id"], "trial_registered", {
            "candidate_id": config["id"], "hypothesis": config["hypothesis"], "parameters": config,
            "protocol_sha256": fingerprint(protocol), "status": "REGISTERED_BEFORE_TEST"})
        evaluated, predictions, baseline = _walk_forward(config, development, min_train)
        metrics = score_predictions(evaluated, predictions)
        comparisons = {name: _comparison(evaluated, predictions, values) for name, values in baseline.items()}
        covered = metrics["coverage_on_directional_outcomes"] or 0
        comparable = all(c["matched_sessions"] >= min_train and c["matched_sessions"] >= 0.8 * metrics["directional_outcomes"]
                         and c["accuracy_improvement"] is not None for c in comparisons.values())
        eligible = covered >= 0.8 and comparable and metrics["balanced_accuracy"] is not None
        missing = sorted({name for row in evaluated for name in config["features"] if _vector(row, [name]) is None})
        result = {"candidate_id": config["id"], "parameters": config, "metrics": metrics, "benchmarks": comparisons,
                  "missing_or_unavailable_features": missing,
                  "status": "DEVELOPMENT_EVALUATED" if eligible else "REJECTED",
                  "rejection_reason": None if eligible else "Insufficient comparable chronological development samples, missing factors, class diversity, or 80% coverage.",
                  "regime_performance": _regimes(evaluated, predictions),
                  "session_results": _session_results(evaluated, predictions, baseline)}
        await append("trial_result:" + config["id"], "trial_result", result)
        if eligible:
            rank = min(c["accuracy_improvement"] for c in comparisons.values())
            results.append((rank, metrics["balanced_accuracy"], config["id"], config))
    # Ranking uses only chronological development, never holdout observations.
    ranked = sorted(results, key=lambda result: (-result[0], -result[1], result[2]))
    winner = ranked[0][3] if ranked else None
    decisions = {config["id"]: ("FROZEN_FOR_ONE_HOLDOUT" if winner and config["id"] == winner["id"] else
                                "Rejected: did not win the preregistered development comparison or lacked eligible data.") for config in configs}
    freeze = {"candidate_id": winner["id"] if winner else None, "parameters": winner,
              "candidate_decisions": decisions, "holdout_inspected_for_selection": False}
    await append("selection_frozen", "selection_frozen", freeze)
    if winner is None:
        final = {"status": "NO_ELIGIBLE_STRATEGY", "selected_strategy": None,
                 "reason": "No candidate met preregistered chronological development requirements.",
                 "holdout_evaluated": False, "predictive_edge_established": False}
        await append("final_decision", "final_decision", final)
        return final
    # Fit final preprocessing and coefficients ONLY on development labels known
    # before the FIRST holdout cutoff, then freeze them for the entire holdout.
    fitted = _fit(winner, development, holdout[0]["cutoff_at"], min_train)
    predictions = [_predict(winner, row, fitted) for row in holdout]
    baseline = {
        "always_up": ["UP"] * len(holdout),
        "momentum_1": [_predict({"method": "momentum", "features": ["return_1"]}, row, None) for row in holdout],
        "training_majority": [_majority(development, holdout[0]["cutoff_at"])] * len(holdout),
    }
    metrics = score_predictions(holdout, predictions)
    comparisons = {name: _comparison(holdout, predictions, values, uncertainty=True) for name, values in baseline.items()}
    full_baselines = {name: score_predictions(holdout, values) for name, values in baseline.items()}
    failures = []
    if metrics["unresolved_outcomes"] or metrics["flat_outcomes"]:
        failures.append("Holdout contains unresolved/flat outcomes; they remain visible and prevent a success claim.")
    if (metrics["coverage_on_directional_outcomes"] or 0) < 0.8:
        failures.append("Directional coverage below preregistered 80%.")
    for name, comparison in comparisons.items():
        candidate, benchmark = comparison["candidate"], comparison["baseline"]
        confusion = candidate["confusion"]
        if comparison["matched_sessions"] < min_holdout or min(confusion["true_up"] + confusion["false_down"], confusion["true_down"] + confusion["false_up"]) < 20:
            failures.append(name + ": fewer than required matched sessions or each outcome class.")
        if comparison["matched_sessions"] < 0.8 * metrics["directional_outcomes"]:
            failures.append(name + ": matched baseline coverage below preregistered 80%.")
        if (comparison["accuracy_improvement"] or 0) <= 0 or (comparison["paired_improvement_lower_bound"] or 0) <= 0:
            failures.append(name + ": no positive uncertainty-supported matched accuracy improvement.")
        if candidate["balanced_accuracy"] is None or benchmark["balanced_accuracy"] is None or candidate["balanced_accuracy"] <= benchmark["balanced_accuracy"]:
            failures.append(name + ": balanced accuracy does not exceed benchmark.")
        if (metrics["full_directional_session_correct_rate"] or 0) <= (full_baselines[name]["full_directional_session_correct_rate"] or 0):
            failures.append(name + ": candidate does not improve full-cohort correctness with abstentions counted as missed opportunities.")
    result = {"candidate_id": winner["id"], "metrics": metrics, "benchmarks": comparisons,
              "regime_performance": _regimes(holdout, predictions), "failed_requirements": failures,
              "all_directional_session_baselines": full_baselines,
              "status": "REJECTED" if failures else "UNTOUCHED_HOLDOUT_OUTPERFORMANCE",
              "no_tuning_or_runner_up_after_holdout": True,
              "session_results": _session_results(holdout, predictions, baseline)}
    await append("holdout_result", "holdout_result", result)
    final = {"status": result["status"], "selected_strategy": None if failures else winner,
             "frozen_candidate": winner["id"], "holdout_evaluated": True,
             "reason": "; ".join(failures) if failures else "Frozen candidate exceeded every fixed baseline on the untouched holdout under the preregistered criteria.",
             "predictive_edge_established": False, "prospective_validation_required": True,
             "probability_status": "NOT_CALIBRATED"}
    await append("final_decision", "final_decision", final)
    return final


def _regimes(rows: list[dict], predictions: list[str]) -> dict:
    # Regimes are fixed before data access. Missing volatility is its own bucket.
    buckets = {"vix_below_20": [], "vix_at_least_20": [], "volatility_unavailable": []}
    for i, row in enumerate(rows):
        vector = _vector(row, ["vix_level"])
        bucket = "volatility_unavailable" if vector is None else "vix_at_least_20" if vector[0] >= 20 else "vix_below_20"
        buckets[bucket].append(i)
    return {name: score_predictions([rows[i] for i in indexes], [predictions[i] for i in indexes])
            for name, indexes in buckets.items()}
