"""The trading engine: one session at a time, identical for backtest and paper trading.

Session order (no look-ahead):
  1. pending buy-limits from yesterday's signals try to fill on today's bar
  2. open positions are checked against their OCO stop / target / time exit
  3. equity is marked at today's close; drawdown and loss-streak halts update
  4. signals are computed on today's close and, if the risk manager allows,
     become plans (paper/backtest: submitted to the paper broker for tomorrow;
     live: written out as order proposals for human approval)
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .costs import CostModel
from .indicators import add_indicators
from .journal import Journal, TradeRecord, record_from_fill
from .paper import PaperBroker
from .risk import RiskManager, RiskState
from .strategy import MarketContext, TradePlan, build_plan, evaluate, market_context


@dataclass
class SessionResult:
    day: str
    fills: list = field(default_factory=list)
    closed: list[TradeRecord] = field(default_factory=list)
    plans: list[TradePlan] = field(default_factory=list)
    blocked: list[tuple[str, str]] = field(default_factory=list)  # (symbol, reason) for signals not taken
    market: MarketContext | None = None
    equity: float = 0.0


class Engine:
    def __init__(self, settings: dict, data: dict[str, pd.DataFrame], mode: str = "backtest",
                 broker: PaperBroker | None = None, state: RiskState | None = None,
                 journal: Journal | None = None, earnings: dict[str, list[str]] | None = None):
        self.settings = settings
        self.mode = mode
        self.scfg, self.rcfg, self.ucfg = settings["strategy"], settings["risk"], settings["universe"]
        self.costs = CostModel.from_settings(settings["costs"])
        self.risk = RiskManager(self.rcfg, self.costs, backtest=(mode == "backtest"))
        start = settings["account"]["starting_balance"]
        self.broker = broker or PaperBroker(start, self.costs, self.scfg["max_hold_days"])
        self.state = state or RiskState(equity=start, cash=start, day_start_equity=start)
        self.journal = journal
        self.earnings = earnings or {}
        mkt_sym = self.scfg["market_symbol"]
        prepared = {s: add_indicators(df, self.scfg) for s, df in data.items()}
        self.spy = prepared.pop(mkt_sym, None)
        self.data = prepared
        self.pos_index = {s: {d: i for i, d in enumerate(df.index)} for s, df in prepared.items()}
        self.entry_fees: dict[str, float] = {}
        self.last_close: dict[str, float] = {}
        self.rejections: Counter = Counter()
        self.halts = 0

    def trading_days(self) -> list[pd.Timestamp]:
        days = set()
        for df in self.data.values():
            days.update(df.index)
        if self.spy is not None:
            days &= set(self.spy.index)
        return sorted(days)

    def _breadth(self, day) -> float | None:
        vals = [bool(df.loc[day, "close"] > df.loc[day, "ema_slow"]) for df in self.data.values()
                if day in df.index and not pd.isna(df.loc[day, "ema_slow"])]
        return round(sum(vals) / len(vals), 3) if vals else None

    def _earnings_blocked(self, symbol: str, day: str) -> bool:
        dates = self.earnings.get(symbol, [])
        if not dates:
            return False
        d0 = np.datetime64(day)
        d1 = np.busday_offset(d0, self.scfg["earnings_blackout_days"], roll="forward")
        return any(d0 <= np.datetime64(d) <= d1 for d in dates)

    def step(self, day: pd.Timestamp, generate: bool = True) -> SessionResult:
        dstr = day.strftime("%Y-%m-%d")
        res = SessionResult(dstr)
        self.risk.start_day(self.state, dstr)
        bars = {s: df.loc[day, ["open", "high", "low", "close"]].to_dict()
                for s, df in self.data.items() if day in self.pos_index[s]}
        for s, b in bars.items():
            self.last_close[s] = b["close"]

        equity_before = self.state.equity
        for f in self.broker.process_day(dstr, bars):
            res.fills.append(f)
            if f.kind == "entry":
                self.entry_fees[f.symbol] = f.fees
                self.risk.register_entry(self.state, f.symbol, f.shares, f.price, f.position.stop)
            else:
                rec = record_from_fill(self.mode, f, self.entry_fees.pop(f.symbol, 0.0), equity_before)
                res.closed.append(rec)
                if self.journal:
                    self.journal.append(rec)
                was_halted = self.state.halted
                cooldown = str(np.busday_offset(np.datetime64(dstr), self.rcfg["backtest_halt_cooldown_days"],
                                                roll="forward"))
                self.risk.record_exit(self.state, f.symbol, rec.pnl, dstr, f.position.entry_date, cooldown)
                if self.state.halted and not was_halted:
                    self.halts += 1

        res.equity = self.broker.equity(self.last_close)
        self.risk.update_equity(self.state, res.equity, self.broker.cash)
        if generate:
            self._signals(day, dstr, res)
        return res

    def _signals(self, day: pd.Timestamp, dstr: str, res: SessionResult) -> None:
        mkt = market_context(self.spy, day, self.scfg, self._breadth(day))
        res.market = mkt
        cands = []
        for s, df in self.data.items():
            i = self.pos_index[s].get(day)
            if i is None:
                continue
            cand, reason = evaluate(s, df, i, mkt, self.scfg, self.ucfg, self.costs)
            if cand is None:
                self.rejections[reason] += 1
            else:
                cands.append((s, cand))
        cands.sort(key=lambda x: -x[1]["confidence"])

        # Work on a scratch copy so that same-day plans share one cash/risk budget.
        scratch = RiskState.from_dict(self.state.to_dict())
        for s, cand in cands:
            ok, why = self.risk.can_open_new(scratch, dstr)
            if not ok:
                res.blocked.append((s, why))
                self.rejections["risk gate: " + why] += 1
                continue
            if s in scratch.open_positions:
                res.blocked.append((s, "already holding (no averaging down)"))
                self.rejections["already holding"] += 1
                continue
            if self._earnings_blocked(s, dstr):
                res.blocked.append((s, "earnings blackout"))
                self.rejections["earnings blackout"] += 1
                continue
            size = self.risk.size(scratch, cand["entry"], cand["stop"])
            if size.shares <= 0:
                res.blocked.append((s, size.reason))
                self.rejections["sizing: " + size.reason] += 1
                continue
            problems = self.risk.check_plan(scratch, s, cand["entry"], cand["stop"], cand["target"],
                                            size.shares, cand["net_rr"])
            if problems:
                res.blocked.append((s, "; ".join(problems)))
                self.rejections["rule: " + problems[0]] += 1
                continue
            plan = build_plan(s, dstr, cand, size.shares, mkt, self.costs)
            if not self.earnings.get(s):
                plan.warnings.append("earnings dates not loaded for this symbol -- check before entry")
            res.plans.append(plan)
            self.risk.register_entry(scratch, s, size.shares, cand["entry"], cand["stop"])
            scratch.cash -= size.shares * cand["entry"]
            if self.mode in ("backtest", "paper"):
                self.broker.submit(plan.as_dict(), dstr)
