"""Event studies use actual stored daily close records, never past hypothetical alerts."""
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal
import pytest
from app.market_volatility_study import study_cboe_history, VERSION


def recorded_history(n=160):
    start=date(2026,1,1)
    values=[]
    # Historical index fixture with recurring short-duration volatility spikes.
    for i in range(n):
        day=start+timedelta(days=i)
        close=Decimal("20") + Decimal(i%27)*Decimal(".12")
        if i%45 in (13,14,15,16,17,18):
            close += Decimal("9")
        values.append({"session":day.isoformat(),"close":str(close)})
    return values


def test_full_horizon_denominators_and_right_censor():
    r=study_cboe_history(recorded_history(),symbol="VXN",
                         collected_at=datetime(2026,11,5,23,tzinfo=timezone.utc))
    assert r["status"]=="RETROSPECTIVE_DESCRIPTIVE_ONLY"
    assert r["method_version"]==VERSION
    assert r["signal_was_recorded_at_past_dates"] is False
    assert r["no_forecast_accuracy_established"] is True
    assert r["no_stock_or_portfolio_return"] is True
    h5=r["horizons"]["5"]
    h20=r["horizons"]["20"]
    assert h5["all_eligible_windows"]==150
    assert h20["all_eligible_windows"]==135
    assert h5["historical_threshold_hits"] >= h5["completed_triggered_windows"]>=3
    assert h5["right_censored_triggered_windows"] == (
        h5["historical_threshold_hits"] - h5["completed_triggered_windows"]
    )
    assert h5["overlapping_events_not_independent"] is True
    assert h5["not_a_winning_trade_rate"] is True
    assert "unconditional_median_index_change_pct" in h5
    assert all(x["future_end_session"] > x["session"] for x in h20["example_events"])


def test_volatility_index_only_and_no_future_or_reordered_history():
    rows=recorded_history()
    capture=datetime(2026,11,5,23,tzinfo=timezone.utc)
    with pytest.raises(ValueError,match="volatility indices"):
        study_cboe_history(rows,symbol="NVDA",collected_at=capture)
    later=rows.copy()
    later[-1]={"session":"2030-01-01","close":"30"}
    with pytest.raises(ValueError,match="Future market session"):
        study_cboe_history(later,symbol="VIX",collected_at=capture)
    duplicate=rows.copy()
    duplicate[-1]=duplicate[-2]
    with pytest.raises(ValueError,match="strictly increasing"):
        study_cboe_history(duplicate,symbol="VXN",collected_at=capture)


def test_reject_nonpositive_or_nonfinite_historical_prices():
    rows=recorded_history()
    rows[100]={"session":rows[100]["session"],"close":"NaN"}
    with pytest.raises(ValueError,match="Nonpositive or nonfinite"):
        study_cboe_history(rows,symbol="VIX",
                           collected_at=datetime(2026,11,5,23,tzinfo=timezone.utc))
    rows[100]["close"]="0"
    with pytest.raises(ValueError,match="Nonpositive or nonfinite"):
        study_cboe_history(rows,symbol="VIX",
                           collected_at=datetime(2026,11,5,23,tzinfo=timezone.utc))


def test_insufficient_historical_periods_are_not_misreported_as_evidence():
    rows=recorded_history(80)
    with pytest.raises(ValueError,match="100"):
        study_cboe_history(rows,symbol="VIX",
                           collected_at=datetime(2026,11,5,23,tzinfo=timezone.utc))
