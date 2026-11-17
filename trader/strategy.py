"""Signal generation and trade plans.

Two long-only daily setups, both requiring an uptrend (9 > 20 > 50 EMA) in a
healthy market (SPY above its 50 EMA, volatility not abnormal):

  * momentum_breakout: close breaks the prior 20-day high on >= 1.5x relative
    volume, RSI 55-72, MACD above signal and rising. Rejected when extended more
    than 1 ATR past the breakout, on a huge range bar, or after a large gap
    (never chase).
  * trend_pullback: price dips to the 20 EMA and closes back up with RSI reset
    to 40-58 and MACD histogram turning up.

Entries are next-session buy limits a hair above the signal close (a gap above
the limit means no fill -- by design). Stops sit at structure (old resistance,
recent swing low) clamped to 1-2 ATR. Targets are set for >= 2:1 reward:risk
*after costs*, and the trade is dropped if a known swing-high resistance sits
below the target.

Intraday VWAP confirmation, earnings and news checks are applied at execution
time (live/paper), where that data exists; the daily backtest cannot model them.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import pandas as pd

from .costs import CostModel
from .indicators import nearest_resistance, nearest_support


@dataclass
class MarketContext:
    ok: bool
    sentiment: str  # bullish / neutral / bearish
    reasons: list[str]
    spy_close: float | None = None
    spy_rsi: float | None = None
    spy_atr_pct: float | None = None
    breadth_above_50ema: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class TradePlan:
    symbol: str
    setup: str
    signal_date: str
    entry: float                 # 1. entry (buy-limit) price
    stop: float                  # 2. stop-loss price
    target: float                # 3. take-profit price
    shares: float                # 4. position size
    max_loss: float              # 5. maximum potential loss incl. expected exit costs
    potential_profit: float      # 6. profit at target net of costs
    reward_risk: float           # 7. net reward:risk
    indicators: dict             # 8. technical indicators at signal
    sentiment: dict              # 9. market sentiment / regime
    confidence: int              # 10. confidence score 0-100
    position_value: float = 0.0
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pre_trade_checks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _r2(x: float) -> float:
    return round(float(x) + 1e-9, 2)


def round_price_down(p: float) -> float:
    return math.floor(p * 100 + 1e-6) / 100


def round_price_up(p: float) -> float:
    return math.ceil(p * 100 - 1e-6) / 100


def market_context(spy: pd.DataFrame | None, i_date, cfg: dict, breadth: float | None = None) -> MarketContext:
    if spy is None or i_date not in spy.index:
        return MarketContext(False, "unknown", ["no market (SPY) data for this date"])
    row = spy.loc[i_date]
    reasons, ok = [], True
    if pd.isna(row["ema_slow"]) or pd.isna(row["atr_pct"]):
        return MarketContext(False, "unknown", ["market indicators warming up"])
    if row["close"] <= row["ema_slow"]:
        ok = False
        reasons.append("SPY below its 50 EMA (downtrend regime)")
    if row["atr_pct"] > cfg["market_max_atr_pct"]:
        ok = False
        reasons.append(f"SPY ATR {row['atr_pct']:.1%} > {cfg['market_max_atr_pct']:.1%} (abnormal volatility)")
    if abs(row["day_ret"]) > cfg["market_max_abs_day_move"]:
        ok = False
        reasons.append(f"SPY moved {row['day_ret']:+.1%} today (abnormal session)")
    if row["close"] > row["ema_mid"] > row["ema_slow"] and row["rsi"] > 50:
        sentiment = "bullish"
    elif row["close"] > row["ema_slow"]:
        sentiment = "neutral"
    else:
        sentiment = "bearish"
    if ok:
        reasons.append(f"SPY regime {sentiment}")
    return MarketContext(ok, sentiment, reasons, float(row["close"]), float(row["rsi"]),
                         float(row["atr_pct"]), breadth)


def _snapshot(row: pd.Series) -> dict:
    keys = ["close", "ema_fast", "ema_mid", "ema_slow", "rsi", "macd", "macd_signal", "macd_hist",
            "atr", "atr_pct", "rvol", "prior_high", "realized_vol20", "avg_dollar_vol"]
    return {k: (None if pd.isna(row[k]) else round(float(row[k]), 4)) for k in keys}


def _confidence(setup: str, row: pd.Series, prev: pd.Series, mkt: MarketContext, room_r: float | None) -> int:
    score = 0.0
    if row["close"] > row["ema_fast"] > row["ema_mid"] > row["ema_slow"]:
        score += 20
    elif row["ema_fast"] > row["ema_mid"] > row["ema_slow"]:
        score += 12
    if row["macd"] > row["macd_signal"]:
        score += 8
    if row["macd_hist"] > prev["macd_hist"]:
        score += 7
    rsi = row["rsi"]
    if setup == "momentum_breakout":
        score += 10 if 58 <= rsi <= 68 else 5
        score += min(max(row["rvol"], 0) / 2.5, 1.0) * 15
    else:
        score += 10 if 45 <= rsi <= 55 else 5
        score += 10 if row["rvol"] >= 1.0 else 4
    score += {"bullish": 20, "neutral": 10}.get(mkt.sentiment, 0)
    if room_r is None or room_r >= 3:
        score += 10
    elif room_r >= 2:
        score += 5
    if 0.02 <= row["atr_pct"] <= 0.06:
        score += 10
    elif row["atr_pct"] < 0.08:
        score += 5
    return int(round(min(score, 100)))


def evaluate(symbol: str, df: pd.DataFrame, i: int, mkt: MarketContext, cfg: dict, ucfg: dict,
             costs: CostModel) -> tuple[dict | None, str]:
    """Evaluate bar i (signal at its close). Returns (candidate, reason).

    candidate = dict(setup, entry, stop, target, net_rr, confidence, reasons, warnings, indicators)
    with no position size yet (sizing is the risk manager's job)."""
    if i < 60:
        return None, "warm-up"
    row, prev = df.iloc[i], df.iloc[i - 1]
    need = ["ema_slow", "rsi", "macd_hist", "atr", "rvol", "prior_high", "avg_dollar_vol"]
    if any(pd.isna(row[k]) for k in need):
        return None, "indicators warming up"
    if not mkt.ok:
        return None, "market filter"
    price = row["close"]
    if not (ucfg["min_price"] <= price <= ucfg["max_price"]):
        return None, "price outside universe range"
    if row["avg_dollar_vol"] < ucfg["min_avg_dollar_volume"]:
        return None, "illiquid"
    if not (row["ema_fast"] > row["ema_mid"] > row["ema_slow"] and price > row["ema_slow"]):
        return None, "no uptrend"
    atr_v = row["atr"]
    if abs(row["gap_pct"]) > cfg["max_gap_pct"]:
        return None, "large gap (don't chase)"
    if (row["high"] - row["low"]) > cfg["max_bar_range_atr"] * atr_v:
        return None, "abnormal range bar"

    setup, reasons = None, []
    macd_up = row["macd"] > row["macd_signal"] and row["macd_hist"] > 0 and row["macd_hist"] > prev["macd_hist"]
    if (price > row["prior_high"] and row["rvol"] >= cfg["min_rvol_breakout"]
            and cfg["breakout_rsi_min"] <= row["rsi"] <= cfg["breakout_rsi_max"] and macd_up):
        if price - row["prior_high"] > cfg["max_extension_atr"] * atr_v:
            return None, "extended past breakout (don't chase)"
        setup = "momentum_breakout"
        reasons = [
            f"close {price:.2f} broke {cfg['breakout_lookback']}-day high {row['prior_high']:.2f}",
            f"relative volume {row['rvol']:.1f}x",
            f"RSI {row['rsi']:.0f}, MACD above signal and rising",
            "EMA 9 > 20 > 50 uptrend",
        ]
        structural = row["prior_high"] - 0.25 * atr_v
    elif (row["low"] <= row["ema_mid"] + 0.25 * atr_v and price > row["ema_mid"] and price > row["open"]
          and price > prev["close"] and cfg["pullback_rsi_min"] <= row["rsi"] <= cfg["pullback_rsi_max"]
          and row["macd_hist"] > prev["macd_hist"]):
        setup = "trend_pullback"
        reasons = [
            f"pulled back to 20 EMA {row['ema_mid']:.2f} and closed up at {price:.2f}",
            f"RSI reset to {row['rsi']:.0f}, MACD histogram turning up",
            "EMA 9 > 20 > 50 uptrend",
        ]
        structural = min(row["prior_low5"], row["low"]) - 0.25 * atr_v
    else:
        return None, "no setup"

    entry = round_price_up(price * (1 + cfg["entry_limit_buffer_pct"]))
    lo, hi = entry - cfg["max_stop_atr_mult"] * atr_v, entry - cfg["min_stop_atr_mult"] * atr_v
    stop_raw = structural if lo <= structural <= hi else entry - cfg["stop_atr_mult"] * atr_v
    stop = round_price_down(stop_raw)
    if stop <= 0 or stop >= entry:
        return None, "invalid stop"
    risk_ps = costs.risk_per_share(entry, stop)
    target = round_price_up(costs.target_for(entry, stop, 2.0) + 0.01)
    reward_ps = costs.net_reward_per_share(entry, target)
    net_rr = reward_ps / risk_ps

    res = nearest_resistance(df, i, entry, cfg["resistance_lookback"], df["pivot_high"])
    room_r = None if res is None else (res - entry) / risk_ps
    warnings = []
    if res is not None and res < target:
        return None, "resistance before target"
    sup = nearest_support(df, i, entry, cfg["resistance_lookback"], df["pivot_low"])
    if sup is not None:
        reasons.append(f"nearest support {sup:.2f}" + (f", resistance {res:.2f}" if res else ", no overhead resistance in 120d"))
    reasons.append(mkt.reasons[-1] if mkt.reasons else "market ok")

    conf = _confidence(setup, row, prev, mkt, room_r)
    if conf < cfg["min_confidence"]:
        return None, "low confidence"
    return {
        "setup": setup, "entry": entry, "stop": stop, "target": target, "net_rr": round(net_rr, 2),
        "confidence": conf, "reasons": reasons, "warnings": warnings, "indicators": _snapshot(row),
        "risk_per_share": risk_ps,
    }, "signal"


def build_plan(symbol: str, signal_date: str, cand: dict, shares: float, mkt: MarketContext,
               costs: CostModel) -> TradePlan:
    entry, stop, target = cand["entry"], cand["stop"], cand["target"]
    risk_ps = cand["risk_per_share"]
    reward_ps = costs.net_reward_per_share(entry, target)
    return TradePlan(
        symbol=symbol, setup=cand["setup"], signal_date=signal_date,
        entry=entry, stop=stop, target=target, shares=shares,
        max_loss=_r2(shares * risk_ps), potential_profit=_r2(shares * reward_ps),
        reward_risk=round(reward_ps / risk_ps, 2), indicators=cand["indicators"],
        sentiment=mkt.as_dict(), confidence=cand["confidence"], position_value=_r2(shares * entry),
        reasons=cand["reasons"], warnings=list(cand["warnings"]),
        pre_trade_checks=[
            "earnings not within blackout window (get_earnings_results / get_earnings_calendar)",
            "no adverse news catalyst (web search)",
            "live price above session VWAP and not > 1 ATR above signal close (don't chase)",
            "symbol tradable and not halted (get_equity_tradability / quote state)",
            "review_equity_order returns no buying-power or PDT alert",
        ],
    )
