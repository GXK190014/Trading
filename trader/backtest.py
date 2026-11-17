"""Historical backtest on real daily bars, using the same engine as paper trading."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .config import ROOT
from .engine import Engine
from .journal import analyze

REPORTS = ROOT / "reports"


def run_backtest(settings: dict, data: dict[str, pd.DataFrame], start: str | None = None,
                 end: str | None = None) -> dict:
    eng = Engine(settings, data, mode="backtest")
    days = eng.trading_days()
    start_ts = pd.Timestamp(start) if start else None
    end_ts = pd.Timestamp(end) if end else None
    trades, curve = [], []
    target = settings["account"]["target_balance"]
    target_hit = None
    signals = plans = 0
    for day in days:
        if end_ts is not None and day > end_ts:
            break
        trading = start_ts is None or day >= start_ts
        if not trading:
            continue
        r = eng.step(day, generate=True)
        trades.extend(r.closed)
        plans += len(r.plans)
        signals += len(r.plans) + len(r.blocked)
        curve.append((r.day, round(r.equity, 4)))
        if target_hit is None and r.equity >= target:
            target_hit = r.day
    equity = [e for _, e in curve]
    stats = analyze(trades, equity)
    filled = sum(1 for t in trades)
    start_bal = settings["account"]["starting_balance"]
    final = equity[-1] if equity else start_bal
    summary = {
        "period": [curve[0][0], curve[-1][0]] if curve else None,
        "sessions": len(curve),
        "symbols": sorted(eng.data),
        "starting_balance": start_bal,
        "final_equity": round(final, 2),
        "return_pct": round((final / start_bal - 1) * 100, 2),
        "target": target,
        "target_hit_on": target_hit,
        "signals_found": signals,
        "orders_submitted": plans,
        "entries_filled": filled + len(eng.broker.positions),
        "halts_triggered": eng.halts,
        "open_at_end": list(eng.broker.positions),
        "stats": stats,
        "top_rejections": eng.rejections.most_common(12),
    }
    return {"summary": summary, "trades": [t.__dict__ for t in trades], "equity_curve": curve}


def save_report(result: dict, name: str = "backtest") -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"{name}.json"
    path.write_text(json.dumps(result, indent=2, default=str))
    return path


def summary_markdown(result: dict, title: str = "Backtest") -> str:
    s, st = result["summary"], result["summary"]["stats"]
    lines = [
        f"## {title}",
        f"- Period: {s['period'][0]} to {s['period'][1]} ({s['sessions']} sessions), {len(s['symbols'])} symbols",
        f"- Equity: ${s['starting_balance']:.2f} -> ${s['final_equity']:.2f} ({s['return_pct']:+.2f}%)",
        f"- Target ${s['target']:.0f} reached: {s['target_hit_on'] or 'no'}",
        f"- Orders submitted: {s['orders_submitted']}, closed trades: {st.get('trades', 0)}, "
        f"halts: {s['halts_triggered']}",
    ]
    if st.get("trades"):
        lines += [
            f"- Win rate {st['win_rate']:.0%}, avg {st['avg_r']:+.2f}R/trade, profit factor {st['profit_factor']}, "
            f"max drawdown {st['max_drawdown']:.1%}, fees ${st['total_fees']:.2f}",
            f"- By setup: {json.dumps(st['by_setup'])}",
            f"- By exit: {json.dumps(st['by_exit'])}",
        ]
    lines.append("- Weaknesses: " + " | ".join(st["weaknesses"]))
    lines.append("- Most common reasons a bar was not traded: " +
                 ", ".join(f"{k} ({v})" for k, v in s["top_rejections"][:8]))
    return "\n".join(lines)
