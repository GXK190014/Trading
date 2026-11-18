"""Same dip-buy rules as tools/dip_buy_backtest.py, applied to individual stocks (daily bars).
Buy next open when close > 200-day SMA and RSI(2) < 10; sell next open after close > 5-day SMA.
Per-trade stats across the 48 stocks with ~2 years of history; costs 0.1% per side."""
import sys
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trader.data import load_daily, available_symbols
from trader.indicators import rsi

def trades_for(df, cost=0.001, stop=None):
    d = df.copy(); d["s200"], d["s5"], d["r2"] = d.close.rolling(200).mean(), d.close.rolling(5).mean(), rsi(d.close, 2)
    out, pos, pending, ent = [], False, None, None
    for r in d.itertuples():
        if pending == "buy": ent, ent_day, pos = r.open * (1 + cost), r.Index, True
        elif pending == "sell": out.append((ent_day, r.open * (1 - cost) / ent - 1)); pos = False
        pending = None
        if pd.isna(r.s200): continue
        if not pos and r.close > r.s200 and r.r2 < 10: pending = "buy"
        elif pos and (r.close > r.s5 or (stop and r.close <= ent * (1 - stop))): pending = "sell"
    return out

def stats(name, rets):
    r = np.array(rets)
    t = r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))
    print(f"{name:42s} trades {len(r):4d}  win {np.mean(r>0):.0%}  avg {r.mean():+.2%}  median {np.median(r):+.2%}  "
          f"worst {r.min():+.1%}  best {r.max():+.1%}  t {t:.1f}")

syms = [s for s in available_symbols() if s != "SPY" and len(load_daily(s)) > 400]
allr, stopped = [], []
for s in syms:
    df = load_daily(s)
    allr += [x for _, x in trades_for(df)]
    stopped += [x for _, x in trades_for(df, stop=0.05)]
spy = [x for d, x in trades_for(load_daily("SPY", Path("data/long")), cost=0.0005) if d >= pd.Timestamp("2025-09-01")]
print(f"{len(syms)} stocks, same period (signals from ~Sep 2025, after the 200-day warm-up):")
stats("SPY (same period)", spy)
stats("individual stocks", allr)
stats("individual stocks with 5% stop", stopped)
