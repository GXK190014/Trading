"""Robustness check: does the baseline result survive small changes?

This is NOT an optimizer. The configured parameters were fixed before testing;
these variations only show how fragile the result is. Picking the best row here
would be curve-fitting.

    python tools/robustness.py
"""

from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.backtest import REPORTS, run_backtest  # noqa: E402
from trader.config import load_settings  # noqa: E402
from trader.data import load_universe  # noqa: E402


def variant(base: dict, **changes) -> dict:
    s = copy.deepcopy(base)
    for path, value in changes.items():
        section, key = path.split("__")
        s[section][key] = value
    return s


def row(name: str, res: dict) -> dict:
    s, st = res["summary"], res["summary"]["stats"]
    r = np.array([t["r_multiple"] for t in res["trades"]])
    t_stat = float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))) if len(r) > 2 and r.std(ddof=1) > 0 else None
    return {
        "variant": name, "final": s["final_equity"], "return_pct": s["return_pct"],
        "trades": st.get("trades", 0), "win_rate": st.get("win_rate"), "avg_r": st.get("avg_r"),
        "max_dd": st.get("max_drawdown"), "t_stat": None if t_stat is None else round(t_stat, 2),
    }


def main():
    base = load_settings()
    data = load_universe()
    runs = [
        ("baseline", base, None, None),
        ("first half (to 2025-11-30)", base, None, "2025-11-30"),
        ("second half (from 2025-12-01)", base, "2025-12-01", None),
        ("stop 1.25 ATR", variant(base, strategy__stop_atr_mult=1.25), None, None),
        ("stop 2.0 ATR", variant(base, strategy__stop_atr_mult=2.0, strategy__max_stop_atr_mult=2.5), None, None),
        ("hold max 7 days", variant(base, strategy__max_hold_days=7), None, None),
        ("hold max 15 days", variant(base, strategy__max_hold_days=15), None, None),
        ("RVOL >= 1.3", variant(base, strategy__min_rvol_breakout=1.3), None, None),
        ("RVOL >= 2.0", variant(base, strategy__min_rvol_breakout=2.0), None, None),
        ("confidence >= 70", variant(base, strategy__min_confidence=70), None, None),
        ("double costs", variant(base, costs__half_spread_pct=0.001, costs__slippage_pct=0.001,
                                 costs__stop_slippage_pct=0.003, costs__min_half_spread=0.02), None, None),
        ("1% risk per trade", variant(base, risk__max_risk_per_trade_pct=0.01), None, None),
        ("fractional shares (research only)", variant(base, risk__whole_shares_only=False), None, None),
    ]
    rows = [row(name, run_backtest(s, data, start, end)) for name, s, start, end in runs]
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "robustness.json").write_text(json.dumps(rows, indent=2))
    hdr = f"{'variant':36s} {'final':>7s} {'ret%':>7s} {'trades':>6s} {'win':>5s} {'avgR':>6s} {'maxDD':>6s} {'t':>5s}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        win = f"{r['win_rate']:.0%}" if r["win_rate"] is not None else "-"
        avg = f"{r['avg_r']:+.2f}" if r["avg_r"] is not None else "-"
        dd = f"{r['max_dd']:.1%}" if r["max_dd"] is not None else "-"
        t = f"{r['t_stat']:.1f}" if r["t_stat"] is not None else "-"
        print(f"{r['variant']:36s} {r['final']:7.2f} {r['return_pct']:+7.1f} {r['trades']:6d} {win:>5s} {avg:>6s} {dd:>6s} {t:>5s}")


if __name__ == "__main__":
    main()
