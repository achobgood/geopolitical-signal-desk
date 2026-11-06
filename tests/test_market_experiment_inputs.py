import asyncio
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from app.market_experiment_inputs import run_stored_experiments, stored_session_rows
from tests.test_market_experiments import history


class Store:
    def __init__(self, tables=None):
        self.tables = tables or {}
        self.events = []

    async def rows(self, table, *, limit, before=None, filters=None):
        rows = self.tables.get(table, [])
        for name, expression in (filters or {}).items():
            if name == "select":
                continue
            if expression.startswith("eq."):
                rows = [row for row in rows if str(row.get(name)) == expression[3:]]
            elif expression.startswith("lte."):
                rows = [row for row in rows if str(row.get(name)) <= expression[4:]]
            elif expression.startswith("in.("):
                ids = expression[4:-1].split(",")
                rows = [row for row in rows if str(row.get(name)) in ids]
        rows = sorted(rows, key=lambda row: row["id"], reverse=True)
        if before is not None:
            rows = [row for row in rows if row["id"] < before]
        return deepcopy(rows[:limit])

    async def rpc(self, name, body):
        assert name == "market_experiment_event_v1"
        self.events.append(deepcopy(body["p"]))
        return {"id": len(self.events)}


def test_no_source_data_records_planned_hypotheses_without_fabricated_tests_or_reserving_holdout():
    store = Store()
    result = asyncio.run(run_stored_experiments(store, study_id="blocked-study", instrument="SPY"))
    assert result["status"] == "BLOCKED_INSUFFICIENT_POINT_IN_TIME_DATA"
    assert result["tested_hypotheses"] == 0
    assert result["result"] is None
    assert result["holdout_reserved"] is False
    assert {config["family"] for config in result["planned_hypotheses"]} == set("ABCDEF")
    assert [event["kind"] for event in store.events] == ["study_blocked"]


def test_real_historical_capture_does_not_manufacture_prior_point_in_time_training():
    capture = {"id": 1, "symbol": "QQQ", "captured_at": "2026-11-05T20:00:00+00:00",
               "observed_session": "2026-11-05", "bars_sha256": "c"*64}
    store = Store({"market_forecast_prices": [capture]})
    result = asyncio.run(run_stored_experiments(store, study_id="historical-block", instrument="QQQ"))
    assert result["retrospective_history_basis"] == "RETROSPECTIVE_RECONSTRUCTED"
    assert result["available_point_in_time_feature_rows"] == 0
    assert result["latest_actual_price_capture"] == capture
    assert result["result"] is None


def stored_fixture(n=4):
    rows = history(n)
    forecasts, evaluations, captures = [], [], []
    for i, row in enumerate(rows, 1):
        cutoff = datetime.fromisoformat(row["cutoff_at"])
        first = (cutoff - timedelta(hours=1)).isoformat()
        reference = cutoff.date().isoformat()
        forecasts.append({"id": i, "symbol": "SPY", "model_version": "momentum-1-research-v1",
                          "target_session": row["session"], "reference_session": reference,
                          "cutoff_at": row["cutoff_at"], "issued_at": first,
                          "reference_capture_id": i,
                          "features": {"return_1": -1, "return_5": 1, "source_price_capture_id": i, "adjustment": "split"}})
        captures.append({"id": i, "symbol": "SPY", "provider": "alpaca", "feed": "sip", "adjustment": "split",
                         "observed_session": reference, "captured_at": first,
                         "source_url": "https://data.alpaca.markets/v2/stocks/SPY/bars", "bars_sha256": "a"*64})
        evaluations.append({"id": i, "forecast_id": i, "status": "OBSERVED", "actual_direction": row["outcome"],
                            "evaluated_at": row["label_available_at"], "capture_id": 1000+i})
    return forecasts, evaluations, captures


def test_stored_constructor_preserves_missing_expected_sessions_and_latest_revision():
    forecasts, evaluations, captures = stored_fixture()
    forecasts.pop(1)
    evaluations.append({"id": 99, "forecast_id": 3, "status": "SOURCE_REVISED"})
    rows = stored_session_rows(forecasts, evaluations, captures, instrument="SPY")
    assert len(rows) == 4
    assert rows[1]["features"] == {}
    assert rows[1]["outcome"] is None
    assert "No immutable forecast" in rows[1]["missing_reason"]
    assert rows[2]["outcome"] is None
    assert rows[2]["missing_reason"] == "Latest outcome state: SOURCE_REVISED"
    assert rows[0]["outcome"] in ("UP", "DOWN")
    assert rows[0]["features"]["return_1"]["source_ids"] == ["market_forecast_prices:1"]
    assert datetime.fromisoformat(rows[0]["features"]["return_1"]["first_collected_at"]) == datetime.fromisoformat(captures[0]["captured_at"])


def test_stored_constructor_does_not_trust_declared_snapshot_collection_time():
    forecasts, evaluations, captures = stored_fixture()
    captures[0]["captured_at"] = "2026-11-05T23:00:00+00:00"
    forecasts[0]["features"]["first_collected_at"] = "2020-01-01T00:00:00+00:00"
    rows = stored_session_rows(forecasts, evaluations, captures, instrument="SPY")
    assert rows[0]["features"]["return_1"]["first_collected_at"] == captures[0]["captured_at"]
    assert datetime.fromisoformat(rows[0]["features"]["return_1"]["usable_at"]) > datetime.fromisoformat(rows[0]["cutoff_at"])


def test_unchanged_observation_rechecks_preserve_first_label_availability():
    forecasts, evaluations, captures = stored_fixture()
    evaluations[0]["target_close"] = "501.10"
    original = deepcopy(evaluations[0])
    repeated = {**original, "id": 99, "capture_id": 999,
                "evaluated_at": (datetime.fromisoformat(original["evaluated_at"]) + timedelta(days=5)).isoformat(),
                "target_close": "501.100"}
    evaluations.append(repeated)
    rows = stored_session_rows(forecasts, evaluations, captures, instrument="SPY")
    assert rows[0]["label_available_at"] == original["evaluated_at"]
    assert rows[0]["label_source_capture_id"] == original["capture_id"]
    assert rows[0]["label_evaluation_id"] == original["id"]
    assert rows[0]["latest_label_evaluation_id"] == repeated["id"]
    # A later different target close is a different label vintage even when its
    # direction stays the same; never backdate that changed numerical outcome.
    repeated["target_close"] = "501.20"
    rows = stored_session_rows(forecasts, evaluations, captures, instrument="SPY")
    assert rows[0]["label_available_at"] == repeated["evaluated_at"]
    # The latest revision always invalidates earlier observed results.
    evaluations.append({"id": 100, "forecast_id": 1, "status": "SOURCE_REVISED"})
    rows = stored_session_rows(forecasts, evaluations, captures, instrument="SPY")
    assert rows[0]["outcome"] is None
    assert rows[0]["label_available_at"] is None


def test_input_boundary_supports_only_known_instruments():
    with pytest.raises(ValueError, match="SPY and QQQ"):
        asyncio.run(run_stored_experiments(Store(), study_id="bad-instrument", instrument="VIX"))
