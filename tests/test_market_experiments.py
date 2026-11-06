"""Adversarial temporal research tests; fixtures are not actual performance."""
import asyncio
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.market_calendar import is_expected_market_session
from app.market_experiments import (
    _fit, _predict, _train_rows, candidate_configs, run_experiments, score_predictions,
)

NY = ZoneInfo("America/New_York")


def history(n=280):
    sessions = []
    day = date(2024, 1, 2)
    while len(sessions) < n + 1:
        if is_expected_market_session(day):
            sessions.append(day)
        day += timedelta(days=1)
    rows = []
    for i, (previous, target) in enumerate(zip(sessions, sessions[1:])):
        cutoff = datetime.combine(previous, time(19), NY).isoformat()
        outcome = "UP" if i % 2 else "DOWN"
        value = 1 if outcome == "UP" else -1
        feature = {"value": value, "published_at": cutoff, "first_collected_at": cutoff,
                   "usable_at": cutoff, "source_ids": [f"source-{i}"]}
        opposite = {**feature, "value": -value}
        rows.append({"session": target.isoformat(), "cutoff_at": cutoff,
                     "label_available_at": datetime.combine(target, time(16, 30), NY).isoformat(),
                     "outcome": outcome, "adjustment": "split", "data_basis": "POINT_IN_TIME_CAPTURED",
                     "features": {"return_1": opposite, "return_5": feature}})
    return rows


def run(rows, events=None, **kwargs):
    events = events if events is not None else []

    async def emit(event):
        events.append(deepcopy(event))

    return asyncio.run(run_experiments(rows, study_id="test-study", dataset_id="fixture-prices",
                       code_hash="a" * 64, instrument="SPY", development_end=rows[159]["session"],
                       holdout_start=rows[160]["session"], emit=emit, **kwargs))


def test_registers_every_hypothesis_parameter_rejection_and_freezes_before_holdout():
    rows, events = history(), []
    result = run(rows, events)
    assert result["status"] == "UNTOUCHED_HOLDOUT_OUTPERFORMANCE"
    assert result["selected_strategy"]["id"] == "B_momentum_5"
    assert result["predictive_edge_established"] is False
    assert result["prospective_validation_required"] is True
    assert result["probability_status"] == "NOT_CALIBRATED"
    assert events[0]["kind"] == "protocol_registered"
    assert events[0]["payload"]["actual_historical_forecasts"] is False
    configs = candidate_configs()
    assert {c["family"] for c in configs} == set("ABCDEF")
    registrations = [e for e in events if e["kind"] == "trial_registered"]
    results = [e for e in events if e["kind"] == "trial_result"]
    assert len(registrations) == len(results) == len(configs)
    for registered, tested in zip(registrations, results):
        assert events.index(registered) < events.index(tested)
        assert registered["payload"]["hypothesis"]
        assert registered["payload"]["parameters"] == tested["payload"]["parameters"]
    missing_macro = next(e["payload"] for e in results if e["payload"]["candidate_id"] == "D_ridge_0.1")
    assert missing_macro["status"] == "REJECTED"
    assert missing_macro["rejection_reason"]
    assert "credit_spread_change" in missing_macro["missing_or_unavailable_features"]
    freeze = next(e for e in events if e["kind"] == "selection_frozen")
    heldout = next(e for e in events if e["kind"] == "holdout_result")
    assert events.index(freeze) < events.index(heldout)
    assert freeze["payload"]["holdout_inspected_for_selection"] is False
    assert len(freeze["payload"]["candidate_decisions"]) == len(configs)
    assert all(c["matched_sessions"] == 120 for c in heldout["payload"]["benchmarks"].values())
    assert heldout["payload"]["metrics"]["brier_score"] is None


def test_failed_holdout_rejects_frozen_winner_without_tuning_or_runner_up():
    rows, events = history(), []
    for row in rows[160:]:
        row["outcome"] = "DOWN" if row["outcome"] == "UP" else "UP"
    result = run(rows, events)
    assert result["status"] == "REJECTED"
    assert result["frozen_candidate"] == "B_momentum_5"
    assert result["selected_strategy"] is None
    assert "no positive" in result["reason"]
    holdout = next(e["payload"] for e in events if e["kind"] == "holdout_result")
    assert holdout["no_tuning_or_runner_up_after_holdout"] is True
    assert holdout["metrics"]["directional_accuracy"] == 0


def test_missing_and_flat_results_remain_visible_and_prevent_success():
    rows, events = history(), []
    rows[170]["outcome"] = None
    rows[170]["label_available_at"] = None
    rows[171]["outcome"] = "FLAT"
    result = run(rows, events)
    heldout = next(e["payload"] for e in events if e["kind"] == "holdout_result")
    assert result["selected_strategy"] is None
    assert heldout["metrics"]["unresolved_outcomes"] == 1
    assert heldout["metrics"]["flat_outcomes"] == 1
    assert heldout["metrics"]["total_sessions"] == 120
    assert heldout["metrics"]["scored_predictions"] == 118


def test_abstentions_cannot_manufacture_high_accuracy_on_a_few_selected_sessions():
    rows, events = history(), []
    for row in rows[160:250]:
        row["features"].pop("return_5")
    result = run(rows, events)
    heldout = next(e["payload"] for e in events if e["kind"] == "holdout_result")
    assert result["status"] == "REJECTED"
    assert heldout["metrics"]["directional_accuracy"] == 1
    assert heldout["metrics"]["coverage_on_directional_outcomes"] == 0.25
    assert heldout["metrics"]["full_directional_session_correct_rate"] == 0.25


def test_matched_subset_success_cannot_hide_a_full_cohort_baseline_loss():
    rows, events = history(310), []
    holdout = rows[160:]
    for i, row in enumerate(holdout):
        # 120UP/30DOWN; the model skips30 easyUP sessions and misses10 others.
        row["outcome"] = "DOWN" if i % 5 == 4 else "UP"
        row["features"]["return_1"]["value"] = 1
        row["features"]["return_5"]["value"] = -1 if row["outcome"] == "DOWN" or i % 15 == 1 else 1
        if i % 5 == 0:
            row["features"].pop("return_5")
    result = run(rows, events)
    heldout = next(e["payload"] for e in events if e["kind"] == "holdout_result")
    assert heldout["metrics"]["coverage_on_directional_outcomes"] == 0.8
    assert heldout["benchmarks"]["always_up"]["accuracy_improvement"] > 0
    assert heldout["benchmarks"]["always_up"]["paired_improvement_lower_bound"] > 0
    assert heldout["metrics"]["full_directional_session_correct_rate"] == pytest.approx(110/150)
    assert heldout["all_directional_session_baselines"]["always_up"]["full_directional_session_correct_rate"] == 0.8
    assert result["selected_strategy"] is None
    assert "full-cohort correctness" in result["reason"]


def test_forecast_feature_availability_is_collection_and_publication_not_old_article_date():
    rows, events = history(), []
    for row in rows:
        feature = row["features"]["return_5"]
        feature["first_collected_at"] = "2026-11-01T12:00:00+00:00"
        feature["usable_at"] = feature["first_collected_at"]
        row["data_basis"] = "RETROSPECTIVE_RECONSTRUCTED"
    result = run(rows, events)
    b5 = next(e["payload"] for e in events if e["kind"] == "trial_result" and e["payload"]["candidate_id"] == "B_momentum_5")
    assert b5["metrics"]["scored_predictions"] == 0
    assert b5["status"] == "REJECTED"
    assert result["selected_strategy"] is None
    assert "RETROSPECTIVE_RECONSTRUCTED" in events[0]["payload"]["data_basis"]


def test_dishonest_availability_duplicate_sessions_and_missing_session_rows_are_rejected():
    rows = history()
    rows[20]["features"]["return_5"]["first_collected_at"] = "2026-11-01T12:00:00+00:00"
    with pytest.raises(ValueError, match="cannot precede"):
        run(rows)
    rows = history()
    rows[20] = rows[19]
    with pytest.raises(ValueError, match="chronological"):
        run(rows)
    rows = history()
    rows.pop(20)
    with pytest.raises(ValueError, match="Missing exchange sessions"):
        run(rows)


def test_no_overlap_no_random_split_no_weakened_sample_requirement_or_mixed_adjustments():
    rows = history()
    with pytest.raises(ValueError, match="cannot be weakened"):
        run(rows, min_holdout=4)
    rows[10]["adjustment"] = "all"
    with pytest.raises(ValueError, match="mix historical adjustment"):
        run(rows)
    rows = history()

    async def emit(event):
        pass

    with pytest.raises(ValueError, match="strictly after"):
        asyncio.run(run_experiments(rows, study_id="test", dataset_id="fixture", code_hash="a"*64, instrument="SPY",
                    development_end=rows[170]["session"], holdout_start=rows[160]["session"], emit=emit))


def test_registration_failure_stops_before_any_experiment_or_holdout_runs():
    events = []

    async def reserved(event):
        events.append(event)
        raise ValueError("Holdout already reserved or consumed")

    rows = history()
    with pytest.raises(ValueError, match="already reserved"):
        asyncio.run(run_experiments(rows, study_id="second-study", dataset_id="alias", code_hash="a"*64, instrument="SPY",
                    development_end=rows[159]["session"], holdout_start=rows[160]["session"], emit=reserved))
    assert len(events) == 1
    assert events[0]["kind"] == "protocol_registered"


def test_label_delay_excludes_overlapping_training_labels_and_no_full_sample_preprocessing():
    rows = history()
    delayed = deepcopy(rows[60])
    delayed["label_available_at"] = rows[70]["label_available_at"]
    train = _train_rows(rows[:60] + [delayed], rows[61]["cutoff_at"], ["return_5"])
    assert len(train) == 60
    config = {"method": "logistic", "features": ["return_5"], "ridge": 0.1,
              "epochs": 80, "learning_rate": 0.15}
    fit = _fit(config, rows[:60], rows[60]["cutoff_at"], 60)
    predicted = _predict(config, rows[60], fit)
    rows[-1]["features"]["return_5"]["value"] = 1e10
    assert _fit(config, rows[:60], rows[60]["cutoff_at"], 60) == fit
    assert _predict(config, rows[60], fit) == predicted


def test_metrics_preserve_abstentions_flat_unresolved_and_confusion_denominators():
    rows = [{"outcome": value} for value in ("UP", "DOWN", "UP", "DOWN", "FLAT", None)]
    metrics = score_predictions(rows, ["UP", "UP", "ABSTAIN", "DOWN", "UP", "DOWN"])
    assert metrics["directional_accuracy"] == pytest.approx(2/3)
    assert metrics["balanced_accuracy"] == 0.75
    assert metrics["false_positive_rate"] == 0.5
    assert metrics["false_negative_rate"] == 0
    assert metrics["flat_outcomes"] == metrics["unresolved_outcomes"] == 1
    assert metrics["abstentions"] == 1
    assert metrics["coverage_on_directional_outcomes"] == 0.75
    assert metrics["brier_score"] is metrics["calibration_error"] is None
    assert score_predictions([], [])["directional_accuracy"] is None
