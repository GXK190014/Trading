"""Risk management: the rules that decide whether a trade may be taken and how big.

Hard rules (none can be switched off by the strategy):
  * risk per trade <= max_risk_per_trade_pct of equity, including expected exit costs
  * reward:risk >= min_reward_risk after costs
  * stop-loss defined before entry; no margin; no averaging down / adding to a position
  * halt after `max_consecutive_losses` losses in a row
  * no new entries for the day after a `max_daily_drawdown_pct` drawdown
  * pattern-day-trader guard (rolling 5 business days)
If the minimum position (1 whole share) would breach the risk cap, the trade is skipped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from .costs import CostModel


@dataclass
class OpenRisk:
    symbol: str
    shares: float
    entry: float
    stop: float
    risk_dollars: float


@dataclass
class RiskState:
    equity: float
    cash: float
    day_start_equity: float
    current_day: str | None = None
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: str | None = None
    halted_until: str | None = None  # ISO date; None + halted => manual reset required
    day_halted: bool = False
    day_trades: list[str] = field(default_factory=list)  # ISO dates of round trips opened+closed same day
    open_positions: dict[str, OpenRisk] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["open_positions"] = {k: v.__dict__ for k, v in self.open_positions.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RiskState":
        d = dict(d)
        d["open_positions"] = {k: OpenRisk(**v) for k, v in d.get("open_positions", {}).items()}
        return cls(**d)


@dataclass
class SizeResult:
    shares: float
    risk_per_share: float
    max_loss: float
    position_value: float
    reason: str | None = None  # set when shares == 0


class RiskManager:
    def __init__(self, cfg: dict, costs: CostModel, backtest: bool = False):
        self.cfg = cfg
        self.costs = costs
        self.backtest = backtest

    # ---- day / halt bookkeeping -------------------------------------------------
    def start_day(self, state: RiskState, day: str) -> None:
        if state.current_day != day:
            state.current_day = day
            state.day_start_equity = state.equity
            state.day_halted = False
        if state.halted and state.halted_until and day >= state.halted_until:
            state.halted = False
            state.halt_reason = None
            state.halted_until = None
            state.consecutive_losses = 0

    def update_equity(self, state: RiskState, equity: float, cash: float | None = None) -> None:
        state.equity = equity
        if cash is not None:
            state.cash = cash
        if state.day_start_equity > 0:
            dd = (state.day_start_equity - equity) / state.day_start_equity
            if dd >= self.cfg["max_daily_drawdown_pct"] - 1e-12:
                state.day_halted = True

    def record_exit(self, state: RiskState, symbol: str, pnl: float, day: str, entry_day: str,
                    cooldown_until: str | None = None) -> None:
        state.open_positions.pop(symbol, None)
        if entry_day == day:
            state.day_trades.append(day)
        if pnl < 0:
            state.consecutive_losses += 1
        else:
            state.consecutive_losses = 0
        if state.consecutive_losses >= self.cfg["max_consecutive_losses"] and not state.halted:
            state.halted = True
            state.halt_reason = f"{state.consecutive_losses} consecutive losses"
            # Live/paper: stays halted until a human reviews and resets. Backtest: cooldown.
            state.halted_until = cooldown_until if self.backtest else None

    def manual_reset(self, state: RiskState) -> None:
        state.halted = False
        state.halt_reason = None
        state.halted_until = None
        state.consecutive_losses = 0

    # ---- gates -------------------------------------------------------------------
    def day_trades_in_window(self, state: RiskState, day: str) -> int:
        d = np.datetime64(day)
        start = np.busday_offset(d, -4, roll="backward")
        return sum(1 for t in state.day_trades if start <= np.datetime64(t) <= d)

    def can_open_new(self, state: RiskState, day: str) -> tuple[bool, str | None]:
        floor = self.cfg.get("equity_floor", 0.0)
        if floor and state.equity < floor:
            return False, f"equity ${state.equity:.2f} below the ${floor:.2f} floor -- review before trading"
        if state.halted:
            return False, f"halted: {state.halt_reason}"
        if state.day_halted:
            return False, f"daily drawdown >= {self.cfg['max_daily_drawdown_pct']:.0%}"
        if len(state.open_positions) >= self.cfg["max_open_positions"]:
            return False, "max open positions"
        if self.cfg.get("pdt_guard") and self.day_trades_in_window(state, day) >= self.cfg["max_day_trades_per_5d"]:
            return False, "pattern-day-trader guard"
        return True, None

    def size(self, state: RiskState, entry: float, stop: float) -> SizeResult:
        if stop >= entry:
            return SizeResult(0, 0, 0, 0, "stop must be below entry")
        risk_per_share = self.costs.risk_per_share(entry, stop)
        open_risk = sum(p.risk_dollars for p in state.open_positions.values())
        budget = min(
            state.equity * self.cfg["max_risk_per_trade_pct"],
            state.equity * self.cfg["max_total_open_risk_pct"] - open_risk,
        )
        if budget <= 0:
            return SizeResult(0, risk_per_share, 0, 0, "no risk budget left")
        # No margin: only settled cash; never more than max_position_pct of equity in one name.
        cash_cap = state.cash if not self.cfg.get("allow_margin") else float("inf")
        value_cap = min(cash_cap, state.equity * self.cfg["max_position_pct"])
        raw = min(budget / risk_per_share, value_cap / entry)
        if self.cfg.get("whole_shares_only", True):
            shares = math.floor(raw + 1e-9)
            if shares < 1:
                why = ("1 share risks more than the per-trade cap"
                       if budget / risk_per_share < 1 else "not enough cash for 1 share")
                return SizeResult(0, risk_per_share, 0, 0, why)
        else:
            shares = math.floor(raw * 1e6) / 1e6
            if shares * entry < 1.0:
                return SizeResult(0, risk_per_share, 0, 0, "position below $1 minimum")
        return SizeResult(shares, risk_per_share, shares * risk_per_share, shares * entry)

    def check_plan(self, state: RiskState, symbol: str, entry: float, stop: float, target: float,
                   shares: float, net_rr: float) -> list[str]:
        """Return a list of violated rules (empty = allowed)."""
        problems = []
        if symbol in state.open_positions:
            problems.append("already holding this symbol (no averaging down / adding)")
        if stop >= entry:
            problems.append("no valid stop below entry")
        if target <= entry:
            problems.append("target not above entry")
        if net_rr + 1e-9 < self.cfg["min_reward_risk"]:
            problems.append(f"reward:risk {net_rr:.2f} < {self.cfg['min_reward_risk']}")
        risk = shares * self.costs.risk_per_share(entry, stop)
        if risk > state.equity * self.cfg["max_risk_per_trade_pct"] + 1e-9:
            problems.append("risk exceeds per-trade cap")
        if not self.cfg.get("allow_margin") and shares * entry > state.cash + 1e-9:
            problems.append("cost exceeds cash (margin not allowed)")
        return problems

    def register_entry(self, state: RiskState, symbol: str, shares: float, entry: float, stop: float) -> None:
        risk = shares * self.costs.risk_per_share(entry, stop)
        state.open_positions[symbol] = OpenRisk(symbol, shares, entry, stop, risk)


def iso(d) -> str:
    return d.isoformat()[:10] if isinstance(d, date) else str(d)[:10]
