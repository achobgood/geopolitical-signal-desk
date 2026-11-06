"""Conservative market-date rules; never infer a trade session from weekdays alone."""
from datetime import date,datetime,timezone

from app.market_calendar import (
    easter_sunday,expected_completed_session,is_expected_market_session,market_holidays
)


def test_known_regular_us_exchange_holidays():
    assert easter_sunday(2026)==date(2026,4,5)
    assert date(2026,4,3) in market_holidays(2026) # Good Friday
    assert date(2026,11,26) in market_holidays(2026) # Thanksgiving
    assert date(2026,6,19) in market_holidays(2026) # Juneteenth
    assert date(2026,7,3) in market_holidays(2026) # Observed July 4
    assert date(2021,12,31) in market_holidays(2021) # Observed next Jan 1
    assert not is_expected_market_session(date(2026,11,26))
    assert is_expected_market_session(date(2026,11,25))


def test_no_nonexistent_holiday_market_session():
    assert expected_completed_session(datetime(
        2026,11,26,22,30,tzinfo=timezone.utc))==date(2026,11,25)
    assert expected_completed_session(datetime(
        2026,11,27,15,0,tzinfo=timezone.utc))==date(2026,11,25)
    assert expected_completed_session(datetime(
        2026,11,27,22,0,tzinfo=timezone.utc))==date(2026,11,27)
    assert expected_completed_session(datetime(
        2026,11,7,23,0,tzinfo=timezone.utc))==date(2026,11,6)


def test_market_calendar_rejects_naive_and_does_not_guess_unknown_year():
    import pytest
    with pytest.raises(ValueError):
        expected_completed_session(datetime(2026,11,5,23,0))
    with pytest.raises(ValueError):
        market_holidays(1900)
