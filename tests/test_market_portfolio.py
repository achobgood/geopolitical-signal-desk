"""Financial invariants using local fixtures, never production portfolio records."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import math
import statistics

import pytest

from app.market_calendar import is_expected_market_session
from app.market_portfolio import ManualPortfolio, analyze_portfolio, exposures, historical_risk, stress

D = Decimal


def portfolio(positions=None, *, cash="20000", complete=True, age_days=0):
    return ManualPortfolio.model_validate({
        "as_of": datetime.now(timezone.utc)-timedelta(days=age_days),
        "source": "Local test fixture only", "complete": complete, "cash": cash,
        "positions": [{"asset_type":"EQUITY",**p}for p in (positions if positions is not None else [{"ticker":"IXN","market_value":"10000"}])],
    })


def security(ticker, weight, *, sector="Information Technology", country="United States", asset="Equity", venue="NASDAQ", sid=None):
    return {"security_id": sid or "fixture:"+ticker, "ticker": ticker, "name": ticker,
            "weight_pct": weight, "sector": sector, "country": country,
            "asset_class": asset, "exchange": venue, "currency": "USD"}


def source(rows, *, as_of=None):
    return {"status": "AVAILABLE", "holdings": rows,
            "source_as_of": as_of or (datetime.now(timezone.utc)-timedelta(days=1)).date().isoformat(),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "source_url": "https://www.ishares.com/local-fixture-not-a-real-download",
            "document_sha256": "a"*64, "weights_renormalized": False}


def test_partial_weights_preserve_unknown_residual_and_never_rescale_to_full_fund():
    p = portfolio()
    sources = {"IXN": source([security("NVDA", "60"), security("JNJ", "20", sector="Health Care")])}
    e = exposures(p, sources)
    assert e["total_entered_value_usd"] == "30000.00"
    assert e["known_technology_value_usd"] == "6000.00"
    assert e["lookthrough_unknown_usd"] == "2000.00"
    assert e["technology_upper_bound_with_unknown_usd"] == "8000.00"
    assert e["weights_renormalized"] is False
    assert sum(D(x["known_value_usd"]) for x in e["underlying_securities"]) == D("8000")


def test_permission_blocked_vanguard_remains_unknown_without_style_estimated_technology():
    p = portfolio([{"ticker": "MGV", "market_value": "10000"}])
    e = exposures(p, {"MGV": {"status": "SOURCE_PERMISSION_REQUIRED", "holdings": [], "style": "Large Value", "estimated_technology_pct": "12"}})
    assert e["lookthrough_unknown_usd"] == "10000.00"
    assert e["known_technology_value_usd"] == "0.00"
    assert e["technology_upper_bound_with_unknown_usd"] == "10000.00"


def test_direct_and_exact_sourced_us_indirect_security_are_combined_once():
    p = portfolio([{"ticker": "NVDA", "market_value": "5000"}, {"ticker": "IXN", "market_value": "10000"}])
    e = exposures(p, {"IXN": source([security("NVDA", "40"), security("MSFT", "60")])})
    nvda = next(row for row in e["underlying_securities"] if row["security_id"] == "US_TICKER:NVDA")
    assert nvda["known_value_usd"] == "9000.00"
    assert nvda["funds"] == ["DIRECT", "IXN"]
    assert nvda in e["cross_fund_overlap"]
    assert e["total_entered_value_usd"] == "35000.00"
    assert e["known_technology_value_usd"] == "15000.00"


def test_same_ticker_at_unresolved_foreign_venue_is_not_guessed_same_security():
    p = portfolio([{"ticker": "NVDA", "market_value": "1000"}, {"ticker": "IXN", "market_value": "10000"}])
    rows = [security("NVDA", "50"), security("NVDA", "50", country="Japan", venue="Tokyo", sid="fixture:foreign-nvda")]
    e = exposures(p, {"IXN": source(rows)})
    direct = next(row for row in e["underlying_securities"] if row["security_id"] == "US_TICKER:NVDA")
    foreign = next(row for row in e["underlying_securities"] if row["security_id"] == "fixture:foreign-nvda")
    assert direct["known_value_usd"] == "6000.00"
    assert foreign["known_value_usd"] == "5000.00"


def test_brokerage_cash_and_internal_fund_cash_are_not_double_counted():
    p = portfolio()
    e = exposures(p, {"IXN": source([security("NVDA", "80"), security("USD", "20", sector="Cash and/or Derivatives", asset="Cash", venue="-")])})
    assert e["total_entered_value_usd"] == "30000.00"
    assert e["spendable_cash_usd"] == "20000.00"
    assert e["sector_value_estimates_usd"]["Fund internal cash (not spendable brokerage cash)"] == "2000.00"
    assert e["known_technology_value_usd"] == "8000.00"
    assert e["lookthrough_unknown_usd"] == "0.00"


def test_equity_bear_scenario_keeps_sourced_internal_cash_unchanged():
    p = portfolio()
    e = exposures(p, {"IXN": source([security("NVDA", "80"), security("USD", "20", sector="Cash and/or Derivatives", asset="Cash", venue="-")])})
    scenario = next(row for row in stress(p, e) if row["id"] == "broad_bear")
    assert scenario["change_range_usd"] == ["-2400.00", "-2400.00"]
    assert scenario["implied_portfolio_value_range_usd"] == ["27600.00", "27600.00"]


@pytest.mark.parametrize("scenario_id,lower,upper", [("tech_30", "-2400.00", "-1800.00"), ("tech_50", "-4000.00", "-3000.00"), ("tech_70", "-5600.00", "-4200.00")])
def test_hypothetical_technology_shocks_use_unknown_exposure_ranges(scenario_id, lower, upper):
    p = portfolio()
    e = exposures(p, {"IXN": source([security("NVDA", "60"), security("JNJ", "20", sector="Health Care")])})
    scenario = next(row for row in stress(p, e) if row["id"] == scenario_id)
    assert scenario["change_range_usd"] == [lower, upper]
    assert scenario["hypothetical"] is scenario["not_a_forecast"] is True


def test_unknown_sector_is_not_silently_assumed_to_have_zero_technology_exposure():
    p = portfolio([{"ticker": "SPMO", "market_value": "10000"}])
    e = exposures(p, {"SPMO": source([security("NVDA", "100", sector=None, country=None, venue="-", sid="cusip:67066G104")])})
    assert e["known_technology_value_usd"] == "0.00"
    assert e["technology_upper_bound_with_unknown_usd"] == "10000.00"
    scenario = next(row for row in stress(p, e) if row["id"] == "tech_30")
    assert scenario["change_range_usd"] == ["-3000.00", "0.00"]


def test_fx_legs_do_not_invent_equity_exposure_and_unquantified_legs_warn():
    p = portfolio()
    rows = [security("NVDA", "100"), security("KRW", "-0.2", sector="Cash and/or Derivatives", asset="FX", venue="-", sid="fixture:fx-leg")]
    result = analyze_portfolio(p, {"IXN": source(rows)})
    assert result["exposure"]["known_technology_value_usd"] == "10000.00"
    assert any("unquantified" in warning or "derivative" in warning for warning in result["warnings"])
    assert result["status"] == "PARTIAL_SOURCE_COVERAGE"


def test_all_in_staged_and_wait_preserve_nav_and_spend_only_entered_cash():
    p = portfolio([{"ticker": "VOO", "market_value": "10000"}])
    original = p.model_dump()
    result = analyze_portfolio(p, {"IXN": source([security("NVDA", "100")])}, deploy_amount=D("20000"), choices=["IXN"], initial_fraction=D(".25"))
    comparison = result["cash_comparison"]
    assert comparison["waiting"]["remaining_cash_usd"] == "20000.00"
    cases = {row["strategy"]: row for row in comparison["alternatives"]}
    assert cases["DEPLOY_ALL_NOW"]["deployed_now_usd"] == "20000.00"
    assert cases["DEPLOY_ALL_NOW"]["remaining_cash_usd"] == "0.00"
    assert cases["INITIAL_STAGED_TRANCHE"]["deployed_now_usd"] == "5000.00"
    assert cases["INITIAL_STAGED_TRANCHE"]["remaining_cash_usd"] == "15000.00"
    assert all(row["hypothetical_lookthrough"]["total_entered_value_usd"] == "30000.00" for row in cases.values())
    assert cases["DEPLOY_ALL_NOW"]["hypothetical_lookthrough"]["known_technology_value_usd"] == "20000.00"
    assert p.model_dump() == original
    assert result["orders_placed"] is result["execution_enabled"] is False
    assert result["probability"] is None
    with pytest.raises(ValueError, match="cash"):
        analyze_portfolio(p, {}, deploy_amount=D("20000.01"))


def test_incomplete_and_old_position_values_warn_before_current_weights_are_used():
    result = analyze_portfolio(portfolio(complete=False, age_days=8), {})
    assert result["status"] == "PARTIAL_SOURCE_COVERAGE"
    assert any("incomplete" in warning for warning in result["warnings"])
    assert any("seven days" in warning for warning in result["warnings"])


def test_stale_issuer_holdings_are_visible_even_with_fresh_position_values():
    result = analyze_portfolio(portfolio(), {"IXN": source([security("NVDA", "100")], as_of="2020-01-01")})
    assert any("stale" in warning.lower() or "old" in warning.lower() or "dated holdings" in warning.lower() for warning in result["warnings"])
    assert result["status"] == "PARTIAL_SOURCE_COVERAGE"


def daily_histories():
    sessions, day = [], date(2026, 1, 2)
    while len(sessions) < 61:
        if is_expected_market_session(day):
            sessions.append(day.isoformat())
        day += timedelta(days=1)
    benchmark, stock = [100.0], [100.0]
    changes = ([.01, -.02, .03] * 20)
    for change in changes:
        benchmark.append(benchmark[-1]*(1+change))
        stock.append(stock[-1]*(1+2*change))
    return {"SPY": [{"session": day, "close": close} for day, close in zip(sessions, benchmark)],
            "NVDA": [{"session": day, "close": close} for day, close in zip(sessions, stock)]}, changes


def test_no_history_and_missing_weighted_security_cannot_generate_beta():
    assert historical_risk({}, {"NVDA": D(".8")})["status"] == "BLOCKED_PRICE_HISTORY"
    histories, _ = daily_histories()
    histories.pop("NVDA")
    result = historical_risk(histories, {"NVDA": D(".8")})
    assert result["status"] == "BLOCKED_PRICE_HISTORY"
    assert "beta_vs_SPY" not in result


def test_missing_common_session_never_turns_a_multi_day_return_into_daily_volatility():
    histories, _ = daily_histories()
    # Add a 62nd complete exchange session, leaving sufficient sample size after
    # removing one middle observation; the test must reach the gap check.
    last = date.fromisoformat(histories["SPY"][-1]["session"])+timedelta(days=1)
    while not is_expected_market_session(last):
        last += timedelta(days=1)
    for rows in histories.values():
        rows.append({"session": last.isoformat(), "close": rows[-1]["close"]*1.01})
    missing = histories["NVDA"].pop(30)["session"]
    result = historical_risk(histories, {"NVDA": D(".8")})
    assert result["status"] == "MISSING_ALIGNED_SESSIONS"
    assert missing in result["missing_expected_sessions"]
    assert "annualized_price_volatility" not in result
    assert "beta_vs_SPY" not in result


def test_duplicate_or_nonfinite_prices_cannot_generate_risk_statistics():
    histories, _ = daily_histories()
    histories["NVDA"].append(deepcopy(histories["NVDA"][10]))
    with pytest.raises(ValueError, match="Duplicate"):
        historical_risk(histories, {"NVDA": D(".8")})
    histories, _ = daily_histories()
    histories["NVDA"][10]["close"] = float("nan")
    with pytest.raises(ValueError, match="invalid"):
        historical_risk(histories, {"NVDA": D(".8")})


def test_non_session_prices_cannot_inflate_daily_return_observation_count():
    histories, _ = daily_histories()
    for rows in histories.values():
        rows.append({"session": "2026-01-04", "close": "99"})  # Sunday.
    try:
        result = historical_risk(histories, {"NVDA": D(".8")})
    except ValueError:
        return
    assert "annualized_price_volatility" not in result
    assert "beta_vs_SPY" not in result


def test_aligned_daily_risk_uses_cash_weight_once_and_labels_hypothetical_price_returns():
    histories, changes = daily_histories()
    result = historical_risk(histories, {"NVDA": D(".8")})
    assert result["aligned_return_sessions"] == 60
    assert result["beta_vs_SPY"] == pytest.approx(1.6)
    assert result["annualized_price_volatility"] == pytest.approx(1.6*statistics.stdev(changes)*math.sqrt(252))
    assert result["maximum_price_drawdown"] < 0
    assert result["dividends_included"] is False
    assert "not actual historical personal performance" in result["scope"]


def test_asset_class_is_explicit_and_bond_is_not_an_equity_bear_loss():
    with pytest.raises(ValueError):
        ManualPortfolio.model_validate({'as_of':datetime.now(timezone.utc),'source':'local source','currency':'USD','complete':True,'cash':'0','positions':[{'ticker':'BND','market_value':'1000'}]})
    p=portfolio([{'ticker':'BND','market_value':'1000','asset_type':'BOND'}],cash='0')
    result=analyze_portfolio(p,{})
    case=next(x for x in result['stress_scenarios']if x['id']=='broad_bear')
    assert case['change_range_usd']==['0.00','0.00']
    assert case['range_is_complete']is False
