"""S&P 500 short-term dip buying (RSI(2) mean reversion) on real SPY daily bars, 2007-2026.

Rules fixed before the first run (classic published rules, adapted to our evening-scan /
next-morning execution):
  * Only when SPY's close is above its 200-day simple average (long-term uptrend).
  * Buy at the NEXT open when the 2-day RSI closes below 10 (a sharp short-term dip).
  * Sell at the NEXT open after SPY closes above its 5-day simple average (the bounce).
  * Whole account in SPY via fractional shares; 0.05% cost per side; no leverage.

    python tools/dip_buy_backtest.py [path to saved SPY get_equity_historicals JSON]
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.config import ROOT  # noqa: E402
from trader.data import import_robinhood_json, load_daily  # noqa: E402
from trader.indicators import rsi  # noqa: E402

LONG = ROOT / "data" / "long"


def backtest(df, start=None, end=None, rsi_entry=10.0, use_200=True, stop=None, at_close=False, cost=0.0005):
    d = df.copy()
    d["sma200"], d["sma5"], d["rsi2"] = d.close.rolling(200).mean(), d.close.rolling(5).mean(), rsi(d.close, 2)
    if start:
        d = d[d.index >= start]
    if end:
        d = d[d.index <= end]
    eq, units, entry_px, trades, curve = 1.0, 0.0, None, [], []
    pending = None
    rows = list(d.itertuples())
    for i, r in enumerate(rows):
        px_open = r.open
        if pending == "buy" and units == 0:
            entry_px = px_open * (1 + cost)
            units, eq_in, entry_day = eq / entry_px, eq, r.Index
        elif pending == "sell" and units > 0:
            eq = units * px_open * (1 - cost)
            trades.append((str(entry_day.date()), str(r.Index.date()), eq / eq_in - 1))
            units = 0.0
        pending = None
        mark = units * r.close if units else eq
        curve.append(mark)
        if pd.isna(r.sma200) or pd.isna(r.rsi2):
            continue
        if at_close:  # optimistic variant: act at today's close instead of tomorrow's open
            if units == 0 and r.rsi2 < rsi_entry and (r.close > r.sma200 or not use_200):
                entry_px, units, eq_in, entry_day = r.close * (1 + cost), eq / (r.close * (1 + cost)), eq, r.Index
            elif units > 0 and (r.close > r.sma5 or (stop and r.close <= entry_px * (1 - stop))):
                eq = units * r.close * (1 - cost)
                trades.append((str(entry_day.date()), str(r.Index.date()), eq / eq_in - 1))
                units = 0.0
            continue
        if units == 0 and r.rsi2 < rsi_entry and (r.close > r.sma200 or not use_200):
            pending = "buy"
        elif units > 0 and (r.close > r.sma5 or (stop and r.close <= entry_px * (1 - stop))):
            pending = "sell"
    final = curve[-1] if curve else 1.0
    arr = np.array(curve)
    peak = np.maximum.accumulate(arr)
    rets = np.array([t[2] for t in trades])
    years = (d.index[-1] - d.index[0]).days / 365.25
    bh = d.close.iloc[-1] / d.close.iloc[0]
    bh_curve = d.close.values / d.close.values[0]
    return {
        "period": f"{d.index[0].date()} to {d.index[-1].date()}",
        "trades": len(trades), "win_rate": round(float((rets > 0).mean()), 3) if len(rets) else None,
        "avg_trade_pct": round(float(rets.mean() * 100), 2) if len(rets) else None,
        "worst_trade_pct": round(float(rets.min() * 100), 1) if len(rets) else None,
        "total_return_pct": round((final - 1) * 100, 1),
        "cagr_pct": round((final ** (1 / years) - 1) * 100, 1) if years > 0 else None,
        "max_dd_pct": round(float(((peak - arr) / peak).max() * 100), 1),
        "time_in_market_pct": None,
        "buy_hold_return_pct": round((bh - 1) * 100, 1),
        "buy_hold_max_dd_pct": round(float(((np.maximum.accumulate(bh_curve) - bh_curve) / np.maximum.accumulate(bh_curve)).max() * 100), 1),
        "trades_per_year": round(len(trades) / years, 1) if years > 0 else None,
        "t_stat": round(float(rets.mean() / (rets.std(ddof=1) / math.sqrt(len(rets)))), 2) if len(rets) > 2 else None,
        "last_trades": trades[-5:],
    }


def main():
    if len(sys.argv) > 1:
        import_robinhood_json(sys.argv[1], out_dir=LONG, merge=True)
    spy = load_daily("SPY", LONG)
    runs = {
        "BASE (rules fixed in advance)": {},
        "  2008-2016": {"end": "2016-12-31"},
        "  2017-2026": {"start": "2017-01-01"},
        "  last 2 years": {"start": "2024-11-18"},
        "act at the close (optimistic)": {"at_close": True},
        "stricter dip (RSI2 < 5)": {"rsi_entry": 5.0},
        "no 200-day trend filter": {"use_200": False},
        "with a 5% stop": {"stop": 0.05},
        "costs x4 (0.2% per side)": {"cost": 0.002},
    }
    out = {name: backtest(spy, **kw) for name, kw in runs.items()}
    (ROOT / "reports" / "dip_buy_backtest.json").write_text(json.dumps(out, indent=2, default=str))
    print(f"{'variant':32s} {'trades':>6s} {'/yr':>5s} {'win':>5s} {'avg%':>6s} {'worst%':>7s} {'CAGR%':>6s} {'total%':>8s} {'maxDD%':>7s} {'t':>5s} | buy&hold total / maxDD")
    for k, v in out.items():
        print(f"{k:32s} {v['trades']:6d} {v['trades_per_year']:5.1f} {v['win_rate']:5.0%} {v['avg_trade_pct']:6.2f} "
              f"{v['worst_trade_pct']:7.1f} {v['cagr_pct']:6.1f} {v['total_return_pct']:8.1f} {v['max_dd_pct']:7.1f} "
              f"{v['t_stat']:5.1f} | {v['buy_hold_return_pct']:.0f}% / {v['buy_hold_max_dd_pct']:.0f}%")
    d = spy.copy()
    d["sma200"], d["sma5"], d["rsi2"] = d.close.rolling(200).mean(), d.close.rolling(5).mean(), rsi(d.close, 2)
    last = d.iloc[-1]
    print(f"\nNOW ({d.index[-1].date()}): SPY close {last.close:.2f}, 200-day avg {last.sma200:.2f} "
          f"({'above' if last.close > last.sma200 else 'BELOW'}), RSI(2) {last.rsi2:.1f}, 5-day avg {last.sma5:.2f} -> "
          + ("BUY SIGNAL for tomorrow's open" if last.close > last.sma200 and last.rsi2 < 10 else "no buy signal"))


if __name__ == "__main__":
    main()
