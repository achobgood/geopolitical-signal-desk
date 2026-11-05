"""Deterministic technical math tests; all synthetic values are TEST FIXTURES ONLY."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.market_technicals import (
    Bar, METHOD, _confirmed_zones, _pivots, analyze_bars, parse_cboe_history,
    wilder_atr, wilder_rsi
)
from app.market_feeds import run_market_feed


def candles(n=145):
    rows = []
    day = date(2026,1,5)
    i = 0
    while len(rows) < n:
        if day.weekday() < 5:
            # Deterministic rolling data with cyclical pivots and a gradual trend.
            phase = (len(rows) % 17)
            wave = abs(phase-8) * Decimal("0.72")
            close = Decimal("21") + wave + Decimal(len(rows)) * Decimal(".034")
            rows.append(Bar(
                session=day, close=close, open=close,
                high=close+Decimal("1.6"),
                low=close-Decimal("1.4"),
            ))
        day += timedelta(days=1)
    return rows


def cboe_fixture(bars, *, include_ohlc=True):
    if include_ohlc:
        return ("Cboe official historical CSV\nDATE,OPEN,HIGH,LOW,CLOSE\n" +
                "\n".join(f"{b.session:%m/%d/%Y},{b.open},{b.high},{b.low},{b.close}" for b in bars)
               ).encode()
    return ("Cboe historical\nDATE,CLOSE\n" +
            "\n".join(f"{b.session:%m/%d/%Y},{b.close}" for b in bars)
           ).encode()


CAPTURE = datetime(2026,11,5,23,30,tzinfo=timezone.utc)


def test_deterministic_indicators_have_known_values():
    constant = [Decimal(100)] * 60
    assert wilder_rsi(constant) == Decimal(50)
    assert wilder_rsi([Decimal(100+i) for i in range(60)]) == Decimal(100)
    assert wilder_rsi([Decimal(200-i) for i in range(60)]) == Decimal(0)
    steady = [Bar(session=date(2026,1,1)+timedelta(days=i),
                  open=Decimal(100),high=Decimal(102),
                  low=Decimal(98),close=Decimal(100)) for i in range(60)]
    assert wilder_atr(steady) == Decimal(4)


def test_parse_true_candles_and_no_future_session_or_lookahead():
    first = candles()
    added = Bar(session=date(2099,1,1),open=Decimal(1),
                high=Decimal(10000),low=Decimal(1),close=Decimal(9999))
    source = cboe_fixture(first + [added])
    got = parse_cboe_history(source,cutoff_session=date(2026,11,5))
    assert len(got) == 145
    assert got[-1].session != date(2099,1,1)
    r = analyze_bars(got,captured_at=CAPTURE,symbol="VXN",instrument_kind="volatility_index")
    assert r["daily_sessions"] == 145
    assert r["atr14"] is not None
    assert r["trade_ticket_approved"] is False
    assert r["source_is_tradeable_instrument"] is False
    assert r["method_version"] == METHOD
    assert "win_rate" not in r and "confidence_score" not in r
    assert r["input_bars"][-1]["session"] == got[-1].session.isoformat()
    assert len(r["bars_sha256"])==64


def test_close_only_no_volatility_levels_or_fabricated_high_low():
    got = parse_cboe_history(cboe_fixture(candles(),include_ohlc=False),
                             cutoff_session=date(2026,11,5))
    assert all(row.high is None and row.low is None for row in got)
    r = analyze_bars(got,captured_at=CAPTURE,symbol="VIX",instrument_kind="volatility_index")
    assert r["price_coverage"] == "close_only"
    assert r["atr14"] is None
    assert r["confirmed_zones"] == []
    assert r["setup"]["status"] == "NO_LEVELS"
    assert r["sma20"] is not None and r["rsi14"] is not None


def test_conflicting_duplicate_daily_bars_fail_closed():
    bs=candles()
    raw=cboe_fixture(bs).decode()
    same_date=bs[0].session.strftime("%m/%d/%Y")
    bad=raw + f"\n{same_date},20,22,19,20\n"
    with pytest.raises(ValueError,match="Conflicting"):
        parse_cboe_history(bad.encode(),cutoff_session=date(2026,11,5))


def test_incomplete_and_invalid_ohlc_rejected_instead_of_fabricated():
    b = candles()
    with pytest.raises(ValueError,match="Inconsistent"):
        Bar(b[0].session,close=Decimal(150),open=Decimal(100),
            high=Decimal(110),low=Decimal(90))
    wrong=cboe_fixture(b).decode().replace("DATE,OPEN,HIGH,LOW,CLOSE",
                                           "DATE,OPEN,HIGH,CLOSE")
    with pytest.raises(ValueError,match="partial"):
        parse_cboe_history(wrong.encode(),cutoff_session=date(2026,11,5))


def test_unconfirmed_reversal_near_end_is_not_used():
    b = [Bar(session=date(2026,1,1)+timedelta(days=i),
             open=Decimal(100),close=Decimal(100),
             high=Decimal(102),low=Decimal(98)) for i in range(12)]
    b[4] = Bar(session=b[4].session,open=Decimal(100),
               high=Decimal(102),low=Decimal(90),close=Decimal(100))
    b[-2] = Bar(session=b[-2].session,open=Decimal(100),
                high=Decimal(102),low=Decimal(88),close=Decimal(100))
    pts = _pivots(b,2,2,"daily")
    assert any(x["price"] == 90 for x in pts)
    assert not any(x["price"] == 88 for x in pts)


def test_zones_need_both_completed_timeframes_and_scale_with_atr():
    ps=[{"side":"support","price":Decimal("99"),"view":"daily"},
        {"side":"support","price":Decimal("99.5"),"view":"weekly"},
        {"side":"support","price":Decimal("100"),"view":"daily"}]
    z1=_confirmed_zones(ps,Decimal("110"),Decimal("2"))
    z2=_confirmed_zones(ps,Decimal("110"),Decimal("6"))
    assert z1 and z2
    w1=Decimal(z1[0]["upper"])-Decimal(z1[0]["lower"])
    w2=Decimal(z2[0]["upper"])-Decimal(z2[0]["lower"])
    assert w2 > w1
    assert z2[0]["timeframes"] == ["daily","weekly"]
    assert not _confirmed_zones(ps[:1],Decimal("110"),Decimal(6))


def test_out_of_order_sessions_rejected():
    c=candles()
    c[-2],c[-1]=c[-1],c[-2]
    with pytest.raises(ValueError,match="ascending"):
        analyze_bars(c,captured_at=CAPTURE,symbol="VXN",instrument_kind="volatility_index")


class FakeSourceStore:
    def __init__(self): self.records=[];self.technical=[];self.patches=[]
    async def rpc(self,name,body):
        if name=="market_feed_claim_v1":
            return {"claimed":True,"run":{"id":47,"run_key":"test_quant_run"}}
        if name=="market_record_observation_v1":
            self.records.append(body["payload"])
            return {"created":True,"observation":{"id":len(self.records)}}
        if name=="market_record_technical_v1":
            data=body["payload"]
            assert data["analysis"]["bars_sha256"]==data["bars_sha256"]
            assert data["analysis"]["trade_ticket_approved"] is False
            assert data["source_observation_id"] <= len(self.records)
            assert data["bar_count"]==len(data["input_bars"])
            self.technical.append(data)
            return {"created":True,"snapshot_id":len(self.technical),"trade_approved":False}
        raise AssertionError("Unexpected SQL RPC "+name)
    async def patch(self,table,id,payload):
        self.patches.append(payload);return payload


def test_actual_collector_path_runs_quant_and_cites_persisted_close_row(monkeypatch):
    import app.market_feeds as m
    from tests.test_market_feeds import treasury_fixture
    # The fixtures are synthetic and can never appear in deployed market data.
    raw=cboe_fixture(candles())
    async def fetch(url,**kw):
        return treasury_fixture() if "treasury" in url else raw
    monkeypatch.setattr(m,"fixed_public_data",fetch)
    monkeypatch.delenv("SEC_USER_AGENT",raising=False)
    db=FakeSourceStore()
    result=asyncio.run(run_market_feed(db,"test_quant_run","manual"))
    assert len(db.technical)==2
    assert {x["symbol"] for x in db.technical}=={"VIX","VXN"}
    assert all(x["instrument_kind"]=="volatility_index" for x in db.technical)
    assert all(x["analysis"]["source_is_tradeable_instrument"] is False for x in db.technical)
    cboe_results = [x for x in result["run"]["provider_results"] if x["provider"].startswith("cboe")]
    assert len(cboe_results)==2
    assert all(x["quantitative_analysis"]["status"]=="completed" for x in cboe_results)
    assert all(x["quantitative_analysis"]["trade_approved"] is False for x in cboe_results)
