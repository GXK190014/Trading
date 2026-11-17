"""Would crypto double $25 faster? A proxy test on real data.

Robinhood's MCP exposes no crypto price history, so this uses the spot Bitcoin and
Ether ETFs (IBIT, ETHA) as price proxies -- real Robinhood daily bars, but they only
trade on weekdays, so weekend crypto moves appear as gaps.

Same strategy and the same 2% risk rule as the stock system, with crypto-specific
differences: fractional sizing (crypto accepts it with broker-held stop orders),
BTC's own 50-day trend as the regime filter (SPY's volatility limits don't fit
crypto), and Robinhood crypto costs measured from live quotes on 2026-11-17:
~1.9% between bid and ask on BTC/ETH/SOL/DOGE/XRP (~0.95% per side vs mark)
on market-maker routing.

    python tools/crypto_proxy.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.backtest import REPORTS, run_backtest  # noqa: E402
from trader.config import ROOT, load_settings  # noqa: E402
from trader.data import load_universe  # noqa: E402
from trader.journal import max_drawdown  # noqa: E402

PROXY_DIR = ROOT / "data" / "crypto_proxy"


def crypto_settings(base: dict, fee_pct: float) -> dict:
    s = copy.deepcopy(base)
    s["strategy"].update(market_symbol="BTC_REGIME", market_max_atr_pct=1.0, market_max_abs_day_move=1.0)
    s["universe"].update(min_price=1.0, max_price=1e9)
    s["risk"]["whole_shares_only"] = False
    s["costs"].update(fee_pct=fee_pct, half_spread_pct=0.0, min_half_spread=0.0, slippage_pct=0.001,
                      stop_slippage_pct=0.003, sec_fee_rate=0.0, taf_per_share=0.0)
    return s


def buy_and_hold(df, start_cash: float, fee_pct: float) -> dict:
    first, last = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
    units = start_cash * (1 - fee_pct) / first
    curve = list(units * df["close"].astype(float))
    final = curve[-1] * (1 - fee_pct)
    return {"final": round(final, 2), "return_pct": round((final / start_cash - 1) * 100, 1),
            "max_dd": round(max_drawdown(curve), 3), "peak": round(max(curve), 2),
            "first_close": first, "last_close": last}


def main():
    base = load_settings()
    data = load_universe(data_dir=PROXY_DIR)
    data["BTC_REGIME"] = data["IBIT"].copy()
    rows = {}
    for name, fee in (("strategy, Robinhood crypto costs (0.95%/side)", 0.0095),
                      ("strategy, hypothetical low costs (0.10%/side)", 0.0010)):
        res = run_backtest(crypto_settings(base, fee), data)
        s, st = res["summary"], res["summary"]["stats"]
        rows[name] = {"final": s["final_equity"], "return_pct": s["return_pct"], "trades": st.get("trades", 0),
                      "win_rate": st.get("win_rate"), "avg_r": st.get("avg_r"), "max_dd": st.get("max_drawdown"),
                      "fees": st.get("total_fees"), "trades_detail": res["trades"]}
    for sym in ("IBIT", "ETHA"):
        rows[f"buy & hold {sym} with all $25"] = buy_and_hold(data[sym], 25.0, 0.0095)
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "crypto_proxy.json").write_text(json.dumps(rows, indent=2, default=str))
    for k, v in rows.items():
        extra = (f"trades {v['trades']}, win {v['win_rate']}, avgR {v['avg_r']}, maxDD {v['max_dd']}, "
                 f"costs ${v['fees']}") if "trades" in v else (
            f"maxDD {v['max_dd']:.0%}, peak ${v['peak']}, close {v['first_close']:.2f} -> {v['last_close']:.2f}")
        print(f"{k:50s} ${v['final']:6.2f} ({v['return_pct']:+.1f}%)  {extra}")
    for k, v in rows.items():
        for t in v.get("trades_detail", []):
            print(f"   [{k[:22]}] {t['entry_time']} {t['symbol']} {t['shares']:.4f} {t['entry_price']:.2f}->"
                  f"{t['exit_price']:.2f} {t['reason_exit']} pnl {t['pnl']:+.2f} ({t['r_multiple']:+.2f}R) fees {t['fees']:.2f}")


if __name__ == "__main__":
    main()
