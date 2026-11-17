import numpy as np
import pandas as pd
import pytest

from trader.indicators import atr, ema, relative_volume, rsi, vwap
from trader.paper import PaperBroker


def plan(symbol="ABC", shares=2, entry=10.0, stop=9.7, target=10.7):
    return {"symbol": symbol, "shares": shares, "entry": entry, "stop": stop, "target": target,
            "setup": "momentum_breakout", "reasons": [], "max_loss": 0.6, "confidence": 70}


def bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def broker_with_position(costs, **kw):
    b = PaperBroker(25.0, costs, max_hold_days=10)
    b.submit(plan(**kw), "d0")
    b.process_day("d1", {"ABC": bar(9.95, 10.1, 9.9, 10.05)})
    return b


def test_limit_not_filled_when_price_gaps_above(costs):
    b = PaperBroker(25.0, costs)
    b.submit(plan(), "d0")
    fills = b.process_day("d1", {"ABC": bar(10.3, 10.6, 10.2, 10.5)})
    assert fills == [] and not b.positions and not b.pending  # day order expired, nothing chased


def test_limit_filled_at_limit_when_traded_through(costs):
    b = PaperBroker(25.0, costs)
    b.submit(plan(), "d0")
    fills = b.process_day("d1", {"ABC": bar(10.2, 10.3, 9.95, 10.1)})
    assert fills[0].price == 10.0
    assert b.cash == pytest.approx(25.0 - 20.0)


def test_never_buys_beyond_cash(costs):
    b = PaperBroker(15.0, costs)
    b.submit(plan(shares=2), "d0")
    assert b.process_day("d1", {"ABC": bar(9.9, 10.0, 9.8, 9.9)}) == []


def test_same_day_stop(costs):
    b = PaperBroker(25.0, costs)
    b.submit(plan(), "d0")
    fills = b.process_day("d1", {"ABC": bar(9.95, 10.0, 9.6, 9.7)})
    assert [f.kind for f in fills] == ["entry", "exit"]
    assert "same day" in fills[1].reason and fills[1].price < 9.7


def test_gap_below_stop_fills_at_open(costs):
    b = broker_with_position(costs)
    f = b.process_day("d2", {"ABC": bar(9.2, 9.4, 9.1, 9.3)})[0]
    assert "gap" in f.reason and f.price < 9.2


def test_stop_and_target_same_bar_assumes_stop(costs):
    b = broker_with_position(costs)
    f = b.process_day("d2", {"ABC": bar(10.0, 10.8, 9.6, 10.5)})[0]
    assert f.reason == "stop-loss"


def test_target_fill(costs):
    b = broker_with_position(costs)
    f = b.process_day("d2", {"ABC": bar(10.1, 10.75, 10.0, 10.6)})[0]
    assert f.reason == "take-profit" and f.price == 10.7


def test_time_exit(costs):
    b = broker_with_position(costs)
    for i in range(9):
        assert b.process_day(f"d{i + 2}", {"ABC": bar(10.0, 10.1, 9.9, 10.0)}) == []
    f = b.process_day("d11", {"ABC": bar(10.0, 10.1, 9.9, 10.0)})[0]
    assert "time exit" in f.reason


def test_rsi_bounds_and_extremes():
    up = pd.Series(np.arange(1, 60, dtype=float))
    down = pd.Series(np.arange(60, 1, -1, dtype=float))
    assert rsi(up).iloc[-1] == pytest.approx(100.0)
    assert rsi(down).iloc[-1] == pytest.approx(0.0)
    noisy = pd.Series(np.random.default_rng(0).normal(0, 1, 300).cumsum() + 100)
    r = rsi(noisy).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_ema_matches_definition():
    s = pd.Series([1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    e = ema(s, 3)
    alpha = 2 / 4
    manual = s.iloc[0]
    for x in s.iloc[1:]:
        manual = alpha * x + (1 - alpha) * manual
    assert e.iloc[-1] == pytest.approx(manual)


def test_atr_constant_range():
    df = pd.DataFrame({"open": 10.0, "high": [10.5] * 40, "low": [9.5] * 40, "close": 10.0})
    assert atr(df, 14).iloc[-1] == pytest.approx(1.0)


def test_relative_volume_uses_prior_bars_only():
    v = pd.Series([100.0] * 20 + [300.0])
    assert relative_volume(v, 20).iloc[-1] == pytest.approx(3.0)


def test_vwap_resets_each_session():
    idx = pd.to_datetime(["2026-11-16 14:30", "2026-11-16 14:35", "2026-11-17 14:30"])
    df = pd.DataFrame({"high": [10, 12, 20], "low": [10, 12, 20], "close": [10, 12, 20],
                       "volume": [100, 100, 50]}, index=idx)
    v = vwap(df)
    assert v.iloc[1] == pytest.approx(11.0)
    assert v.iloc[2] == pytest.approx(20.0)
