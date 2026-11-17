"""End-to-end invariants on real data plus the live-trading gate."""

import copy

import pytest

from trader.costs import CostModel
from trader.data import available_symbols, load_universe
from trader.engine import Engine
from trader.proposals import build_proposal, live_enabled
from trader.strategy import MarketContext, TradePlan

HAVE_DATA = len(available_symbols()) > 5


@pytest.mark.skipif(not HAVE_DATA, reason="no market data in data/daily")
def test_every_plan_obeys_risk_rules_on_real_data(settings):
    eng = Engine(settings, load_universe(), mode="backtest")
    plans = 0
    for day in eng.trading_days():
        equity_at_signal = eng.state.equity
        r = eng.step(day)
        assert eng.broker.cash >= -1e-9, "cash went negative (margin used)"
        held = list(eng.broker.positions)
        assert len(held) == len(set(held))
        assert len(held) <= settings["risk"]["max_open_positions"]
        for p in r.plans:
            plans += 1
            assert float(p.shares).is_integer() and p.shares >= 1
            assert p.stop < p.entry < p.target
            assert p.reward_risk >= settings["risk"]["min_reward_risk"]
            assert p.max_loss <= settings["risk"]["max_risk_per_trade_pct"] * r.equity + 0.01
            assert p.position_value <= r.equity + 1e-6
            assert p.symbol not in held or p.symbol in [f.symbol for f in r.fills if f.kind == "exit"]
    assert plans > 0


def _plan(**kw):
    base = dict(symbol="ABC", setup="momentum_breakout", signal_date="2026-11-13", entry=10.02, stop=9.71,
                target=10.68, shares=1, max_loss=0.33, potential_profit=0.66, reward_risk=2.0, indicators={},
                sentiment=MarketContext(True, "bullish", []).as_dict(), confidence=70, position_value=10.02)
    base.update(kw)
    return TradePlan(**base)


def test_live_trading_off_by_default(settings):
    assert not live_enabled(settings)
    p = build_proposal(_plan(), settings)
    assert p["status"].startswith("PAPER_ONLY")


def test_live_needs_both_mode_and_approval(settings):
    s = copy.deepcopy(settings)
    s["mode"] = "live"
    assert not live_enabled(s)
    s["live_trading_approved"] = True
    assert live_enabled(s)
    assert build_proposal(_plan(), s)["status"] == "AWAITING_USER_APPROVAL"


def test_proposal_orders_are_whole_share_limit_and_sell_oco(settings):
    p = build_proposal(_plan(), settings)
    entry, oco = p["steps"][1]["params"], p["steps"][3]["params"]
    assert entry["type"] == "limit" and entry["quantity"] == "1" and entry["time_in_force"] == "gfd"
    assert oco["side"] == "sell" and float(oco["take_profit_limit_price"]) > float(oco["stop_loss_stop_price"])
    assert all("approval" in s["gate"] or "FILLED" in s["gate"] or "checks" in s["gate"] for s in p["steps"])
    assert p["oco_problems"] == []


def test_oco_rules_flag_tight_legs(settings):
    p = build_proposal(_plan(stop=10.0, target=10.05), settings)
    assert len(p["oco_problems"]) >= 2


def test_cost_model_round_trip_is_a_cost(settings):
    c = CostModel.from_settings(settings["costs"])
    assert c.market_buy_fill(10) > 10 > c.market_sell_fill(10) > c.stop_sell_fill(10) - 0.02
    assert c.stop_exit_cost_per_share(10) > 0
