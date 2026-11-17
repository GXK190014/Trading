"""Trading journal and performance analytics.

Every closed trade is recorded with: date/time, ticker, entry/exit prices, size,
strategy, reason for entry, reason for exit, P&L, and whether it followed the
strategy. `analyze` turns the journal into statistics plus plain-language
weaknesses -- it reports what happened, good or bad.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .config import ROOT

JOURNAL_DIR = ROOT / "journal"


@dataclass
class TradeRecord:
    mode: str
    symbol: str
    setup: str
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    shares: float
    stop: float
    target: float
    reason_entry: str
    reason_exit: str
    pnl: float
    fees: float
    r_multiple: float
    equity_before: float
    confidence: int
    followed_strategy: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def win(self) -> bool:
        return self.pnl > 0


def record_from_fill(mode: str, exit_fill, entry_fees: float, equity_before: float) -> TradeRecord:
    pos = exit_fill.position
    pnl = (exit_fill.price - pos.entry_price) * pos.shares - exit_fill.fees - entry_fees
    planned_risk = pos.plan.get("max_loss") or (pos.entry_price - pos.stop) * pos.shares
    return TradeRecord(
        mode=mode, symbol=pos.symbol, setup=pos.setup, entry_time=pos.entry_date,
        entry_price=round(pos.entry_price, 4), exit_time=exit_fill.date, exit_price=round(exit_fill.price, 4),
        shares=pos.shares, stop=pos.stop, target=pos.target,
        reason_entry="; ".join(pos.plan.get("reasons", [])), reason_exit=exit_fill.reason,
        pnl=round(pnl, 4), fees=round(exit_fill.fees + entry_fees, 4),
        r_multiple=round(pnl / planned_risk, 2) if planned_risk else 0.0,
        equity_before=round(equity_before, 4), confidence=int(pos.plan.get("confidence", 0)),
    )


class Journal:
    def __init__(self, mode: str, path: Path | None = None):
        self.mode = mode
        self.path = path or JOURNAL_DIR / f"{mode}_trades.jsonl"

    def append(self, rec: TradeRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(rec)) + "\n")

    def load(self) -> list[TradeRecord]:
        if not self.path.exists():
            return []
        return [TradeRecord(**json.loads(line)) for line in self.path.read_text().splitlines() if line.strip()]


def max_drawdown(equity: list[float]) -> float:
    if not equity:
        return 0.0
    arr = np.asarray(equity, dtype=float)
    peak = np.maximum.accumulate(arr)
    return float(((peak - arr) / peak).max())


def analyze(trades: list[TradeRecord], equity_curve: list[float] | None = None) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "weaknesses": ["no trades yet -- nothing to evaluate"]}
    pnl = np.array([t.pnl for t in trades])
    r = np.array([t.r_multiple for t in trades])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_win, gross_loss = wins.sum(), -losses.sum()
    stats = {
        "trades": n,
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "win_rate": round(len(wins) / n, 3),
        "total_pnl": round(float(pnl.sum()), 2),
        "avg_win": round(float(wins.mean()), 3) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 3) if len(losses) else 0.0,
        "avg_r": round(float(r.mean()), 3),
        "profit_factor": round(float(gross_win / gross_loss), 2) if gross_loss > 0 else None,
        "total_fees": round(sum(t.fees for t in trades), 3),
        "max_drawdown": round(max_drawdown(equity_curve or []), 4),
        "longest_losing_streak": _longest_streak([t.pnl <= 0 for t in trades]),
    }
    by_exit: dict[str, list[float]] = {}
    by_setup: dict[str, list[float]] = {}
    for t in trades:
        key = t.reason_exit.split(" (")[0]
        by_exit.setdefault(key, []).append(t.r_multiple)
        by_setup.setdefault(t.setup, []).append(t.r_multiple)
    stats["by_exit"] = {k: {"count": len(v), "avg_r": round(float(np.mean(v)), 2)} for k, v in by_exit.items()}
    stats["by_setup"] = {
        k: {"count": len(v), "win_rate": round(float(np.mean([x > 0 for x in v])), 3), "avg_r": round(float(np.mean(v)), 2)}
        for k, v in by_setup.items()
    }
    stats["weaknesses"] = _weaknesses(trades, stats)
    return stats


def _longest_streak(flags: list[bool]) -> int:
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def _weaknesses(trades: list[TradeRecord], s: dict) -> list[str]:
    out = []
    n = s["trades"]
    if n < 30:
        out.append(f"only {n} trades: too few to judge the edge statistically (aim for 30+)")
    if s["avg_r"] <= 0:
        out.append(f"negative expectancy: average {s['avg_r']:+.2f}R per trade -- the strategy is losing money")
    be = 1 / 3  # breakeven win rate at 2R winners / 1R losers
    if s["win_rate"] < be:
        out.append(f"win rate {s['win_rate']:.0%} is below the ~33% needed to break even at 2:1")
    same_day = sum(1 for t in trades if "same day" in t.reason_exit)
    if same_day / n > 0.2:
        out.append(f"{same_day} trades stopped out on the entry day -- entries may be chasing or stops too tight")
    gaps = sum(1 for t in trades if "gap below stop" in t.reason_exit)
    if gaps:
        out.append(f"{gaps} stop(s) gapped through: real losses can exceed the planned 2% risk")
    timed = s["by_exit"].get("time exit", {}).get("count", 0)
    if timed / n > 0.4:
        out.append(f"{timed} trades hit the time exit -- targets may be too far for the holding period")
    for setup, v in s["by_setup"].items():
        if v["count"] >= 5 and v["avg_r"] < 0:
            out.append(f"setup '{setup}' loses on average ({v['avg_r']:+.2f}R over {v['count']} trades)")
    if s["longest_losing_streak"] >= 4:
        out.append(f"longest losing streak {s['longest_losing_streak']} trades")
    broke = [t for t in trades if not t.followed_strategy]
    if broke:
        out.append(f"{len(broke)} trade(s) broke the rules")
    return out or ["no obvious weaknesses detected in this sample"]
