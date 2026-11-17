"""Risk controls: each hard rule from the brief has a test."""

import pytest

from trader.risk import RiskManager, RiskState


def make(settings, costs, equity=25.0, cash=None, backtest=False):
    rm = RiskManager(settings["risk"], costs, backtest=backtest)
    st = RiskState(equity=equity, cash=equity if cash is None else cash, day_start_equity=equity)
    rm.start_day(st, "2026-11-16")
    return rm, st


def test_position_risk_never_exceeds_two_percent(settings, costs):
    rm, st = make(settings, costs)
    size = rm.size(st, entry=5.00, stop=4.85)
    assert size.shares >= 1
    assert size.max_loss <= 0.02 * 25 + 1e-9
    assert float(size.shares).is_integer()


def test_one_share_too_risky_is_skipped_not_forced(settings, costs):
    rm, st = make(settings, costs)
    size = rm.size(st, entry=20.00, stop=19.00)  # $1 risk on one share > $0.50 cap
    assert size.shares == 0
    assert "per-trade cap" in size.reason


def test_no_margin_cash_limits_size(settings, costs):
    rm, st = make(settings, costs, equity=25.0, cash=4.0)
    size = rm.size(st, entry=10.00, stop=9.90)
    assert size.shares == 0
    assert "cash" in size.reason


def test_reward_risk_below_two_rejected(settings, costs):
    rm, st = make(settings, costs)
    problems = rm.check_plan(st, "ABC", 10.0, 9.8, 10.3, 1, net_rr=1.5)
    assert any("reward:risk" in p for p in problems)


def test_no_averaging_down(settings, costs):
    rm, st = make(settings, costs)
    rm.register_entry(st, "ABC", 1, 10.0, 9.8)
    problems = rm.check_plan(st, "ABC", 9.5, 9.3, 9.95, 1, net_rr=2.2)
    assert any("averaging down" in p for p in problems)


def test_entry_cost_above_cash_rejected(settings, costs):
    rm, st = make(settings, costs, equity=25.0, cash=8.0)
    problems = rm.check_plan(st, "ABC", 10.0, 9.8, 10.5, 1, net_rr=2.2)
    assert any("margin" in p for p in problems)


def test_two_consecutive_losses_halt_until_manual_reset(settings, costs):
    rm, st = make(settings, costs)
    rm.record_exit(st, "A", -0.4, "2026-11-16", "2026-11-12")
    assert rm.can_open_new(st, "2026-11-16")[0]
    rm.record_exit(st, "B", -0.3, "2026-11-17", "2026-11-13")
    ok, why = rm.can_open_new(st, "2026-11-17")
    assert not ok and "consecutive losses" in why
    rm.start_day(st, "2026-12-30")  # time alone never clears a live/paper halt
    assert not rm.can_open_new(st, "2026-12-30")[0]
    rm.manual_reset(st)
    assert rm.can_open_new(st, "2026-12-30")[0]


def test_win_resets_loss_streak(settings, costs):
    rm, st = make(settings, costs)
    rm.record_exit(st, "A", -0.4, "2026-11-16", "2026-11-12")
    rm.record_exit(st, "B", +0.8, "2026-11-17", "2026-11-13")
    rm.record_exit(st, "C", -0.4, "2026-11-18", "2026-11-14")
    assert not st.halted


def test_backtest_halt_has_cooldown(settings, costs):
    rm, st = make(settings, costs, backtest=True)
    rm.record_exit(st, "A", -0.4, "2026-11-16", "2026-11-12", cooldown_until="2026-11-23")
    rm.record_exit(st, "B", -0.4, "2026-11-16", "2026-11-12", cooldown_until="2026-11-23")
    assert st.halted
    rm.start_day(st, "2026-11-20")
    assert st.halted
    rm.start_day(st, "2026-11-23")
    assert not st.halted and st.consecutive_losses == 0


def test_daily_drawdown_stops_new_entries_for_the_day(settings, costs):
    rm, st = make(settings, costs, equity=25.0)
    rm.update_equity(st, 23.70)  # -5.2% on the day
    ok, why = rm.can_open_new(st, "2026-11-16")
    assert not ok and "drawdown" in why
    rm.start_day(st, "2026-11-17")
    assert rm.can_open_new(st, "2026-11-17")[0]


def test_drawdown_just_under_limit_allowed(settings, costs):
    rm, st = make(settings, costs, equity=25.0)
    rm.update_equity(st, 23.80)  # -4.8%
    assert rm.can_open_new(st, "2026-11-16")[0]


def test_pdt_guard_blocks_after_three_day_trades(settings, costs):
    rm, st = make(settings, costs)
    for d in ("2026-11-10", "2026-11-12", "2026-11-16"):
        rm.record_exit(st, "X", 0.1, d, d)
    ok, why = rm.can_open_new(st, "2026-11-16")
    assert not ok and "pattern-day-trader" in why
    assert rm.can_open_new(st, "2026-11-23")[0]  # oldest fell out of the 5-business-day window


def test_max_open_positions(settings, costs):
    rm, st = make(settings, costs, equity=1000.0)
    rm.register_entry(st, "A", 1, 10, 9.8)
    rm.register_entry(st, "B", 1, 10, 9.8)
    ok, why = rm.can_open_new(st, "2026-11-16")
    assert not ok and "max open positions" in why


def test_total_open_risk_budget(settings, costs):
    rm, st = make(settings, costs, equity=100.0)
    rm.register_entry(st, "A", 10, 10.0, 9.70)  # ~$3.25 at risk of a $4 total budget
    size = rm.size(st, entry=10.0, stop=9.5)
    remaining = 4.0 - st.open_positions["A"].risk_dollars
    assert 0 < size.max_loss <= remaining + 1e-9
    assert size.shares == 1  # the $2 per-trade cap alone would have allowed 3 shares
    rm.register_entry(st, "B", 1, 10.0, 9.5)
    assert rm.size(st, entry=10.0, stop=9.5).shares == 0  # budget exhausted


@pytest.mark.parametrize("entry,stop", [(10, 10), (10, 10.5)])
def test_stop_must_be_below_entry(settings, costs, entry, stop):
    rm, st = make(settings, costs)
    assert rm.size(st, entry, stop).shares == 0


def test_percentage_costs_count_toward_the_two_percent_cap(settings):
    from trader.costs import CostModel
    crypto = CostModel.from_settings({**settings["costs"], "fee_pct": 0.0095, "half_spread_pct": 0.0,
                                      "min_half_spread": 0.0})
    rm = RiskManager({**settings["risk"], "whole_shares_only": False}, crypto)
    st = RiskState(equity=25.0, cash=25.0, day_start_equity=25.0)
    size = rm.size(st, entry=100.0, stop=95.0)
    # stop distance 5% + ~0.95% to get in + ~1.1% to get out
    assert crypto.risk_per_share(100.0, 95.0) > 5.0 + 0.95 + 0.9
    assert size.max_loss <= 0.5 + 1e-9
    target = crypto.target_for(100.0, 95.0, 2.0)
    assert crypto.net_reward_per_share(100.0, target) >= 2.0 * crypto.risk_per_share(100.0, 95.0) - 1e-9


def test_live_profile_caps_risk_at_its_configured_percentage(live_settings, costs):
    cap = live_settings["risk"]["max_risk_per_trade_pct"]
    assert cap <= 0.10, "live risk per trade must never exceed 10% without an explicit decision"
    rm = RiskManager(live_settings["risk"], costs)
    st = RiskState(equity=25.0, cash=25.0, day_start_equity=25.0)
    size = rm.size(st, entry=10.0, stop=9.0)
    assert 0 < size.max_loss <= cap * 25.0 + 1e-9
    assert size.position_value <= 25.0 + 1e-9  # never more than the cash in the account


def test_equity_floor_stops_new_trades(live_settings, costs):
    rm = RiskManager(live_settings["risk"], costs)
    floor = live_settings["risk"]["equity_floor"]
    st = RiskState(equity=floor - 0.01, cash=floor - 0.01, day_start_equity=floor)
    ok, why = rm.can_open_new(st, "2026-11-17")
    assert not ok and "floor" in why
