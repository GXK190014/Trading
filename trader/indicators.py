"""Technical indicators computed from real OHLCV bars (pandas, no look-ahead).

Every rolling statistic that a signal compares *today's* bar against (prior highs,
average volume) is shifted by one bar so today's values never leak into it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # No losses in the window -> RSI 100; no gains -> 0.
    out = out.where(avg_loss != 0.0, 100.0)
    return out.where(avg_gain.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    sig = line.ewm(span=signal, adjust=False).mean()
    out = pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})
    out.iloc[: slow + signal] = np.nan  # warm-up
    return out


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR."""
    return true_range(df).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def relative_volume(volume: pd.Series, lookback: int = 20) -> pd.Series:
    """Today's volume / average of the prior `lookback` sessions."""
    return volume / volume.rolling(lookback).mean().shift(1)


def vwap(intraday: pd.DataFrame) -> pd.Series:
    """Session VWAP from intraday bars; resets each calendar day (index must be datetimes)."""
    typical = (intraday["high"] + intraday["low"] + intraday["close"]) / 3.0
    session = intraday.index.normalize()
    pv = (typical * intraday["volume"]).groupby(session).cumsum()
    vol = intraday["volume"].groupby(session).cumsum()
    return pv / vol.replace(0, np.nan)


def pivot_levels(df: pd.DataFrame, window: int = 5) -> tuple[pd.Series, pd.Series]:
    """Swing highs/lows: a bar whose high (low) is the extreme of +/- `window` bars.

    A pivot is only *known* `window` bars after it forms, so the returned series are
    shifted by `window` to stay free of look-ahead.
    """
    span = 2 * window + 1
    is_high = df["high"] == df["high"].rolling(span, center=True).max()
    is_low = df["low"] == df["low"].rolling(span, center=True).min()
    highs = df["high"].where(is_high).shift(window)
    lows = df["low"].where(is_low).shift(window)
    return highs, lows


def nearest_resistance(df: pd.DataFrame, i: int, price: float, lookback: int, pivot_highs: pd.Series) -> float | None:
    """Lowest swing high above `price` within the lookback window ending at bar i."""
    lo = max(0, i - lookback + 1)
    window = pivot_highs.iloc[lo : i + 1].dropna()
    above = window[window > price]
    return float(above.min()) if len(above) else None


def nearest_support(df: pd.DataFrame, i: int, price: float, lookback: int, pivot_lows: pd.Series) -> float | None:
    """Highest swing low below `price` within the lookback window ending at bar i."""
    lo = max(0, i - lookback + 1)
    window = pivot_lows.iloc[lo : i + 1].dropna()
    below = window[window < price]
    return float(below.max()) if len(below) else None


def add_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Return a copy of daily bars with every indicator the strategy uses."""
    out = df.copy()
    out["ema_fast"] = ema(out["close"], cfg["ema_fast"])
    out["ema_mid"] = ema(out["close"], cfg["ema_mid"])
    out["ema_slow"] = ema(out["close"], cfg["ema_slow"])
    out["rsi"] = rsi(out["close"], cfg["rsi_period"])
    out = out.join(macd(out["close"]))
    out["atr"] = atr(out, cfg["atr_period"])
    out["atr_pct"] = out["atr"] / out["close"]
    out["rvol"] = relative_volume(out["volume"], cfg["rvol_lookback"])
    out["avg_dollar_vol"] = (out["close"] * out["volume"]).rolling(cfg["rvol_lookback"]).mean()
    out["prior_high"] = out["high"].rolling(cfg["breakout_lookback"]).max().shift(1)
    out["prior_low5"] = out["low"].rolling(5).min().shift(1)
    out["gap_pct"] = out["open"] / out["close"].shift(1) - 1.0
    out["day_ret"] = out["close"].pct_change()
    out["realized_vol20"] = out["day_ret"].rolling(20).std() * np.sqrt(252)
    out["pivot_high"], out["pivot_low"] = pivot_levels(out)
    return out
