"""Same-day momentum strategy on 30-minute bars (opening-range breakout above VWAP).

Rules were fixed BEFORE the first backtest run (2026-11-17) and are not tuned to results:

  Universe (from the prior day's daily bar): price $2-$22.50, 20-day avg dollar volume >= $20M.
  Opening range (OR) = the 9:30-10:00 bar.
  Signal, checked at the close of the 10:00, 10:30 and 11:00 bars (i.e. 10:30-11:30 AM):
    close > OR high, close > session VWAP, close > today's open,
    cumulative volume >= 1.5x the average cumulative volume at that time over the prior 10 days,
    opening gap <= 8%, close no more than 0.5 daily ATR above the OR high (don't chase),
    SPY's close > SPY's session VWAP (market moving up intraday).
  One trade per day: the earliest signalling bar, highest relative volume first.
  Entry at the NEXT bar's open (time to alert the user and get approval) plus spread/slippage.
  Stop = the tighter of OR low and entry - 0.5 daily ATR; target = 2x risk net of costs.
  Exit: stop (checked first if both are hit in one bar), target, or the 4:00 PM close.
  Risk: 10% of equity max loss per trade, whole shares, cash only, at most 3 trades per
  5 business days (pattern-day-trader limit), 5-business-day pause after 3 losses in a row.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import ROOT
from .costs import CostModel
from .indicators import atr

INTRADAY_30 = ROOT / "data" / "intraday" / "30minute"
DAILY = ROOT / "data" / "daily"


@dataclass
class IntradayRules:
    signal_bars: tuple = (1, 2, 3)
    min_rvol: float = 1.5
    max_gap: float = 0.08
    max_ext_atr: float = 0.5
    stop_atr: float = 0.5
    reward_risk: float = 2.0
    risk_pct: float = 0.10
    min_price: float = 2.0
    max_price: float = 22.5
    min_dollar_vol: float = 20e6
    require_spy_vwap: bool = True
    require_spy_50ema: bool = False
    max_trades_5d: int = 3
    loss_streak_pause: int = 3
    pause_days: int = 5
    rvol_lookback: int = 10


@dataclass
class Trade:
    day: str
    symbol: str
    signal_bar: int
    entry: float
    stop: float
    target: float
    shares: int
    exit: float
    reason: str
    pnl: float
    r: float
    equity_after: float


def load_sessions(symbol: str) -> dict[str, pd.DataFrame]:
    df = pd.read_csv(INTRADAY_30 / f"{symbol}.csv")
    ts = pd.to_datetime(df["date"], utc=True).dt.tz_convert("America/New_York")
    df["session"] = ts.dt.strftime("%Y-%m-%d")
    df["t"] = ts
    out = {}
    for day, g in df.sort_values("t").groupby("session"):
        if len(g) == 13:
            g = g.reset_index(drop=True)
            tp = (g["high"] + g["low"] + g["close"]) / 3
            g["vwap"] = (tp * g["volume"]).cumsum() / g["volume"].cumsum().replace(0, np.nan)
            g["cumvol"] = g["volume"].cumsum()
            out[day] = g
    return out


def daily_context(symbol: str) -> pd.DataFrame:
    d = pd.read_csv(DAILY / f"{symbol}.csv", parse_dates=["date"]).set_index("date").sort_index()
    ctx = pd.DataFrame(index=d.index)
    ctx["prev_close"] = d["close"].shift(1)
    ctx["atr"] = atr(d, 14).shift(1)
    ctx["dollar_vol"] = (d["close"] * d["volume"]).rolling(20).mean().shift(1)
    ctx["ema50"] = d["close"].ewm(span=50, adjust=False).mean().shift(1)
    ctx.index = ctx.index.strftime("%Y-%m-%d")
    return ctx


def run_intraday(symbols: list[str], costs: CostModel, rules: IntradayRules | None = None,
                 start_equity: float = 25.0, start: str | None = None, end: str | None = None) -> dict:
    rules = rules or IntradayRules()
    sessions = {s: load_sessions(s) for s in symbols + ["SPY"]}
    ctx = {s: daily_context(s) for s in symbols + ["SPY"]}
    spy = sessions["SPY"]
    days = sorted(d for d in spy if (start is None or d >= start) and (end is None or d <= end))
    equity, trades, curve = start_equity, [], []
    day_trades: list[str] = []
    streak, paused_until = 0, None
    skipped = {"pdt_limit": 0, "paused": 0, "too_small_or_risky": 0}

    for day in days:
        if paused_until and day < paused_until:
            skipped["paused"] += 1
            curve.append((day, equity))
            continue
        d64 = np.datetime64(day)
        window_start = np.busday_offset(d64, -4, roll="backward")
        if sum(1 for t in day_trades if np.datetime64(t) >= window_start) >= rules.max_trades_5d:
            skipped["pdt_limit"] += 1
            curve.append((day, equity))
            continue
        sp = spy[day]
        if rules.require_spy_50ema:
            c = ctx["SPY"].loc[day] if day in ctx["SPY"].index else None
            if c is None or not (sp["open"].iloc[0] > c["ema50"]):
                curve.append((day, equity))
                continue
        trade = None
        for k in rules.signal_bars:
            if rules.require_spy_vwap and not (sp["close"].iloc[k] > sp["vwap"].iloc[k]):
                continue
            cands = []
            for s in symbols:
                g = sessions[s].get(day)
                if g is None or day not in ctx[s].index:
                    continue
                c = ctx[s].loc[day]
                if pd.isna(c["atr"]) or pd.isna(c["dollar_vol"]):
                    continue
                if not (rules.min_price <= c["prev_close"] <= rules.max_price) or c["dollar_vol"] < rules.min_dollar_vol:
                    continue
                prior = [sessions[s][p]["cumvol"].iloc[k] for p in _prior_days(sessions[s], day, rules.rvol_lookback)]
                if len(prior) < rules.rvol_lookback:
                    continue
                rvol = g["cumvol"].iloc[k] / (np.mean(prior) or np.nan)
                orh, orl, o = g["high"].iloc[0], g["low"].iloc[0], g["open"].iloc[0]
                close = g["close"].iloc[k]
                if (close > orh and close > g["vwap"].iloc[k] and close > o and rvol >= rules.min_rvol
                        and o / c["prev_close"] - 1 <= rules.max_gap
                        and close - orh <= rules.max_ext_atr * c["atr"]):
                    cands.append((rvol, s, orl, c["atr"]))
            for rvol, s, orl, a in sorted(cands, reverse=True):
                g = sessions[s][day]
                raw = g["open"].iloc[k + 1]
                entry = raw + costs.half_spread(raw) + raw * costs.slippage_pct
                stop = max(orl, entry - rules.stop_atr * a)
                stop = math.floor(stop * 100) / 100
                if stop >= entry * 0.9985:
                    skipped["too_small_or_risky"] += 1
                    continue
                risk_ps = costs.risk_per_share(entry, stop)
                target = math.ceil(costs.target_for(entry, stop, rules.reward_risk) * 100) / 100
                shares = int(min(equity * rules.risk_pct / risk_ps, equity / entry))
                if shares < 1:
                    skipped["too_small_or_risky"] += 1
                    continue
                trade = (s, k, entry, stop, target, shares)
                break
            if trade or cands:
                break
        if trade is None:
            curve.append((day, equity))
            continue
        s, k, entry, stop, target, shares = trade
        g = sessions[s][day]
        exit_px, reason = None, None
        for j in range(k + 1, 13):
            lo, hi, op = g["low"].iloc[j], g["high"].iloc[j], g["open"].iloc[j]
            if lo <= stop:
                exit_px, reason = costs.stop_sell_fill(min(stop, op) if j > k + 1 else stop), "stop"
                break
            if hi >= target:
                exit_px, reason = target, "target"
                break
        if exit_px is None:
            exit_px, reason = costs.market_sell_fill(g["close"].iloc[12]), "close"
        pnl = (exit_px - entry) * shares - costs.sell_fees(exit_px, shares)
        equity += pnl
        r = pnl / (costs.risk_per_share(entry, stop) * shares)
        trades.append(Trade(day, s, k, round(entry, 4), stop, target, shares, round(exit_px, 4), reason,
                            round(pnl, 4), round(r, 2), round(equity, 4)))
        day_trades.append(day)
        streak = streak + 1 if pnl < 0 else 0
        if streak >= rules.loss_streak_pause:
            paused_until = str(np.busday_offset(d64, rules.pause_days, roll="forward"))
            streak = 0
        curve.append((day, equity))
    return {"trades": trades, "curve": curve, "skipped": skipped, "days": len(days)}


def _prior_days(sess: dict, day: str, n: int) -> list[str]:
    keys = [d for d in sess if d < day]
    return keys[-n:]


def summarize(res: dict, start_equity: float = 25.0) -> dict:
    t = res["trades"]
    eq = np.array([start_equity] + [e for _, e in res["curve"]])
    peak = np.maximum.accumulate(eq)
    out = {
        "days": res["days"], "trades": len(t), "final": round(float(eq[-1]), 2),
        "return_pct": round((eq[-1] / start_equity - 1) * 100, 1),
        "max_dd": round(float(((peak - eq) / peak).max()), 3),
        "skipped": res["skipped"],
    }
    if t:
        r = np.array([x.r for x in t])
        out.update(win_rate=round(float((r > 0).mean()), 3), avg_r=round(float(r.mean()), 3),
                   t_stat=round(float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))), 2) if len(r) > 2 else None,
                   exits={k: sum(1 for x in t if x.reason == k) for k in ("target", "stop", "close")})
    return out
