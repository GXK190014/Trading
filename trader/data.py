"""Market data loading.

The only market-data source available is the Robinhood MCP connector, which the
agent (not this Python code) calls. Its `get_equity_historicals` results are saved
as JSON files; `import_robinhood_json` converts them to per-symbol CSVs that the
engine reads. Nothing in this module invents prices: missing data stays missing.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pandas as pd

from .config import ROOT

DAILY_DIR = ROOT / "data" / "daily"
INTRADAY_DIR = ROOT / "data" / "intraday"


def import_robinhood_json(path: str | Path, out_dir: Path | None = None, merge: bool = True) -> dict[str, int]:
    """Convert a saved `get_equity_historicals` result into CSVs (one per symbol).

    Bars flagged `interpolated` are synthesized gap-fill with no information, so
    they are dropped. With merge=True, new bars are merged into an existing CSV
    (newer data wins on duplicate timestamps).
    """
    payload = json.loads(Path(path).read_text())
    results = payload.get("data", payload).get("results", [])
    written: dict[str, int] = {}
    for res in results:
        interval = res.get("interval", "day")
        target_dir = out_dir or (DAILY_DIR if interval in ("day", "week") else INTRADAY_DIR / interval)
        target_dir.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "date": b["begins_at"][:10] if interval == "day" else b["begins_at"],
                "open": float(b["open_price"]),
                "high": float(b["high_price"]),
                "low": float(b["low_price"]),
                "close": float(b["close_price"]),
                "volume": int(b["volume"]),
            }
            for b in res.get("bars", [])
            if not b.get("interpolated")
        ]
        if not rows:
            continue
        new = pd.DataFrame(rows)
        csv_path = target_dir / f"{res['symbol']}.csv"
        if merge and csv_path.exists():
            old = pd.read_csv(csv_path, dtype={"date": str})
            new = pd.concat([old, new]).drop_duplicates("date", keep="last")
        new = new.sort_values("date")
        new.to_csv(csv_path, index=False, quoting=csv.QUOTE_MINIMAL)
        written[res["symbol"]] = len(new)
    return written


def fill_daily_from_intraday(interval: str = "30minute", bars_per_session: int = 13) -> dict[str, list[str]]:
    """Robinhood publishes the newest *daily* bar late (it comes back `interpolated` for a
    while). Build missing daily bars from complete intraday sessions instead: a session
    counts only if all `bars_per_session` regular-hours bars are present."""
    added: dict[str, list[str]] = {}
    src = INTRADAY_DIR / interval
    for f in sorted(src.glob("*.csv")):
        intr = pd.read_csv(f)
        intr["ts"] = pd.to_datetime(intr["date"], utc=True)
        intr["session"] = intr["ts"].dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
        daily_path = DAILY_DIR / f.name
        have = set(pd.read_csv(daily_path, dtype={"date": str})["date"]) if daily_path.exists() else set()
        rows = []
        for session, g in intr.sort_values("ts").groupby("session"):
            if session in have or len(g) < bars_per_session:
                continue
            rows.append({"date": session, "open": g["open"].iloc[0], "high": g["high"].max(),
                         "low": g["low"].min(), "close": g["close"].iloc[-1], "volume": int(g["volume"].sum())})
        if rows:
            old = pd.read_csv(daily_path, dtype={"date": str}) if daily_path.exists() else pd.DataFrame()
            pd.concat([old, pd.DataFrame(rows)]).sort_values("date").to_csv(daily_path, index=False)
            added[f.stem] = [r["date"] for r in rows]
    return added


PROVISIONAL_DIR = ROOT / "data" / "provisional"


def build_provisional(interval: str = "30minute", bars_per_session: int = 13) -> dict[str, float]:
    """Today's bar *so far* from intraday bars, for intraday checks. Kept apart from
    data/daily (a partial bar there would block the complete one later). Volume is scaled by
    the share of the session elapsed so relative volume stays comparable to full days.
    Returns {symbol: fraction of session elapsed}."""
    PROVISIONAL_DIR.mkdir(parents=True, exist_ok=True)
    for old in PROVISIONAL_DIR.glob("*.csv"):
        old.unlink()
    out: dict[str, float] = {}
    for f in sorted((INTRADAY_DIR / interval).glob("*.csv")):
        intr = pd.read_csv(f)
        intr["ts"] = pd.to_datetime(intr["date"], utc=True)
        intr["session"] = intr["ts"].dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
        today = intr["session"].max()
        g = intr[intr["session"] == today].sort_values("ts")
        if g.empty or len(g) >= bars_per_session:
            continue  # complete sessions go to data/daily via fill_daily_from_intraday
        frac = len(g) / bars_per_session
        pd.DataFrame([{"date": today, "open": g["open"].iloc[0], "high": g["high"].max(), "low": g["low"].min(),
                       "close": g["close"].iloc[-1], "volume": float(g["volume"].sum()) / frac}]
                     ).to_csv(PROVISIONAL_DIR / f.name, index=False)
        out[f.stem] = round(frac, 2)
    return out


def load_daily(symbol: str, data_dir: Path | None = None, provisional: bool = False) -> pd.DataFrame:
    path = (data_dir or DAILY_DIR) / f"{symbol}.csv"
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
    if provisional and (PROVISIONAL_DIR / f"{symbol}.csv").exists():
        p = pd.read_csv(PROVISIONAL_DIR / f"{symbol}.csv", parse_dates=["date"]).set_index("date")
        df = pd.concat([df, p[p.index > df.index.max()]])
    df = df[~df.index.duplicated(keep="last")]
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def available_symbols(data_dir: Path | None = None) -> list[str]:
    return sorted(p.stem for p in (data_dir or DAILY_DIR).glob("*.csv"))


def load_universe(symbols: list[str] | None = None, data_dir: Path | None = None,
                  provisional: bool = False) -> dict[str, pd.DataFrame]:
    symbols = symbols or available_symbols(data_dir)
    return {s: load_daily(s, data_dir, provisional) for s in symbols}
