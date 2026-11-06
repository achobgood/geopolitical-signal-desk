"""Conservative US stock-market session calendar for daily research, not trading hours.

Includes regular NYSE federal/exchange holidays, observed holidays and Good Friday.
Unexpected exchange closures and exchange-specific half days are NOT inferred.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, time
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def nth_weekday(year: int, month: int, weekday: int, number: int) -> date:
    first = date(year,month,1)
    delta = (weekday - first.weekday()) % 7
    return first + timedelta(days=delta+7*(number-1))


def last_weekday(year:int,month:int,weekday:int) -> date:
    last = date(year+1,1,1)-timedelta(days=1) if month==12 else date(year,month+1,1)-timedelta(days=1)
    return last - timedelta(days=(last.weekday()-weekday)%7)


def observed(month: int, day:int, year:int) -> date:
    holiday = date(year,month,day)
    if holiday.weekday()==5:return holiday-timedelta(days=1)
    if holiday.weekday()==6:return holiday+timedelta(days=1)
    return holiday


def easter_sunday(year:int) -> date:
    # Meeus/Jones/Butcher Gregorian computus, 1900–2099.
    a=year%19;b=year//100;c=year%100;d=b//4;e=b%4
    f=(b+8)//25;g=(b-f+1)//3
    h=(19*a+b-d-g+15)%30
    i=c//4;k=c%4;l=(32+2*e+2*i-h-k)%7
    m=(a+11*h+22*l)//451
    month=(h+l-7*m+114)//31
    day=((h+l-7*m+114)%31)+1
    return date(year,month,day)


@lru_cache(maxsize=20)
def market_holidays(year:int) -> frozenset[date]:
    if not 2000 <= year <= 2100:
        raise ValueError("Unsupported exchange-calendar year")
    d={
        date(year,1,1) if date(year,1,1).weekday()!=6 else date(year,1,2),
        nth_weekday(year,1,0,3), # Martin Luther King
        nth_weekday(year,2,0,3), # Washington's birthday
        easter_sunday(year)-timedelta(days=2), # Good Friday
        last_weekday(year,5,0), # Memorial Day
        observed(7,4,year), # Independence Day
        nth_weekday(year,9,0,1), # Labor Day
        nth_weekday(year,11,3,4), # Thanksgiving
        observed(12,25,year), # Christmas
    }
    if year >= 2022:
        d.add(observed(6,19,year)) # Juneteenth starting in 2022
    # Exceptional full closures since 2000 (NYSE announcements).
    d.update(date.fromisoformat(x) for x in (
        '2001-09-11','2001-09-12','2001-09-13','2001-09-14',
        '2004-06-11','2007-01-02','2012-10-29','2012-10-30',
        '2018-12-05','2025-01-09'))
    return frozenset(x for x in d if x.year==year)


def is_expected_market_session(day:date) -> bool:
    return day.weekday()<5 and day not in market_holidays(day.year)


def expected_completed_session(as_of:datetime) -> date:
    if as_of.utcoffset() is None:
        raise ValueError("Calendar cutoff must be timezone-aware")
    local=as_of.astimezone(NY)
    candidate=local.date()
    # Wait until ordinary close plus a conservative daily feed publication buffer.
    # Early-close days may appear stale longer; never imply a shortened session is tradable.
    if (local.hour,local.minute)<(16,15):
        candidate-=timedelta(days=1)
    for _ in range(12):
        if is_expected_market_session(candidate):
            return candidate
        candidate-=timedelta(days=1)
    raise ValueError("Unable to resolve recent market session")
