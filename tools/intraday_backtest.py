"""Backtest the same-day strategy in trader/intraday.py on real Robinhood 30-minute bars.

The base rules were fixed before the first run; the variants only show how fragile the
result is (they are not used to pick parameters).

    python tools/intraday_backtest.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.config import ROOT, load_settings  # noqa: E402
from trader.costs import CostModel  # noqa: E402
from trader.intraday import INTRADAY_30, IntradayRules, run_intraday, summarize  # noqa: E402


def main():
    settings = load_settings()
    costs = CostModel.from_settings(settings["costs"])
    symbols = sorted(p.stem for p in INTRADAY_30.glob("*.csv") if p.stem != "SPY"
                     and (ROOT / "data" / "daily" / p.name).exists())
    base = IntradayRules()
    double = CostModel.from_settings({**settings["costs"], "half_spread_pct": 0.001, "slippage_pct": 0.001,
                                      "stop_slippage_pct": 0.003, "min_half_spread": 0.02})
    runs = [
        ("BASE (rules fixed in advance)", base, costs, None, None),
        ("  first half (to 2026-06-15)", base, costs, None, "2026-06-15"),
        ("  second half (from 2026-06-16)", base, costs, "2026-06-16", None),
        ("no SPY-VWAP filter", replace(base, require_spy_vwap=False), costs, None, None),
        ("plus SPY above 50-day EMA", replace(base, require_spy_50ema=True), costs, None, None),
        ("signals until 1:00 PM", replace(base, signal_bars=(1, 2, 3, 4, 5, 6)), costs, None, None),
        ("RVOL >= 2.0", replace(base, min_rvol=2.0), costs, None, None),
        ("double costs", base, double, None, None),
        ("2% risk per trade", replace(base, risk_pct=0.02), costs, None, None),
    ]
    rows, detail = [], None
    for name, rules, c, a, b in runs:
        res = run_intraday(symbols, c, rules, 25.0, a, b)
        s = summarize(res)
        rows.append({"variant": name, **s})
        if detail is None:
            detail = [asdict(t) for t in res["trades"]]
    out = ROOT / "reports" / "intraday_backtest.json"
    out.write_text(json.dumps({"symbols": symbols, "rules": asdict(base), "rows": rows, "base_trades": detail},
                              indent=2, default=str))
    print(f"{len(symbols)} symbols, sessions {rows[0]['days']}")
    print(f"{'variant':34s} {'final':>7s} {'ret%':>7s} {'trades':>6s} {'win':>5s} {'avgR':>6s} {'t':>5s} {'maxDD':>6s}")
    for r in rows:
        win = f"{r['win_rate']:.0%}" if "win_rate" in r else "-"
        avg = f"{r['avg_r']:+.2f}" if "avg_r" in r else "-"
        t = f"{r['t_stat']:.1f}" if r.get("t_stat") is not None else "-"
        print(f"{r['variant']:34s} {r['final']:7.2f} {r['return_pct']:+7.1f} {r['trades']:6d} {win:>5s} {avg:>6s} "
              f"{t:>5s} {r['max_dd']:6.1%}")
    print("base exits:", rows[0].get("exits"), "| skipped days:", rows[0]["skipped"])


if __name__ == "__main__":
    main()
