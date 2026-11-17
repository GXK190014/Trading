"""Static HTML dashboard: progress from $25 to $50, P&L, positions, risk, signals, journal.

Only real numbers are shown: equity from the paper/live state, trades from the
journal, signals from the latest scan. Missing data is shown as missing.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT
from .journal import Journal, analyze

REPORTS = ROOT / "reports"


def _money(x: float | None, sign: bool = False) -> str:
    if x is None:
        return "—"
    x = round(x, 2) + 0.0  # avoid "-$0.00" from float dust
    s = f"{x:+,.2f}" if sign else f"{x:,.2f}"
    return ("-$" + s[1:] if s.startswith("-") else ("+$" + s[1:] if s.startswith("+") else "$" + s))


def _cls(x: float | None) -> str:
    return "" if x is None or abs(x) < 0.005 else ("pos" if x > 0 else "neg")


def _sparkline(curve: list, start: float, target: float, w: int = 640, h: int = 140) -> str:
    if len(curve) < 2:
        return '<p class="muted">Equity curve appears after the first sessions are processed.</p>'
    vals = [float(v) for _, v in curve]
    lo, hi = min(vals + [start]), max(vals + [start])
    hi = max(hi, lo + 0.01)
    pad = 8

    def y(v: float) -> float:
        return h - pad - (v - lo) / (hi - lo) * (h - 2 * pad)

    step = (w - 2 * pad) / (len(vals) - 1)
    pts = " ".join(f"{pad + i * step:.1f},{y(v):.1f}" for i, v in enumerate(vals))
    base = y(start)
    tgt = f'<line x1="{pad}" x2="{w - pad}" y1="{y(target):.1f}" y2="{y(target):.1f}" class="tgt"/>' if lo <= target <= hi else ""
    return (f'<svg viewBox="0 0 {w} {h}" class="spark" role="img" aria-label="Equity curve">'
            f'<line x1="{pad}" x2="{w - pad}" y1="{base:.1f}" y2="{base:.1f}" class="base"/>{tgt}'
            f'<polyline points="{pts}" class="line"/></svg>'
            f'<div class="axis"><span>{html.escape(str(curve[0][0]))}</span>'
            f'<span>low {_money(min(vals))} · high {_money(max(vals))}</span>'
            f'<span>{html.escape(str(curve[-1][0]))}</span></div>')


def _table(headers: list[str], rows: list[list], empty: str) -> str:
    if not rows:
        return f'<p class="muted">{html.escape(empty)}</p>'
    th = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def build_dashboard(settings: dict, mode: str, st: dict, last_prices: dict[str, float],
                    signals: dict | None = None, backtest: dict | None = None, trades=None) -> str:
    start = settings["account"]["starting_balance"]
    target = settings["account"]["target_balance"]
    trades = Journal(mode).load() if trades is None else trades
    stats = analyze(trades, [v for _, v in st.get("equity_curve", [])])
    risk = st["risk"]
    broker = st.get("broker", {})
    positions = broker.get("positions", {}) or {}

    unreal = sum((last_prices.get(s, p["entry_price"]) - p["entry_price"]) * p["shares"] for s, p in positions.items())
    equity = broker.get("cash", risk["cash"]) + sum(last_prices.get(s, p["entry_price"]) * p["shares"]
                                                    for s, p in positions.items())
    if mode == "live":
        equity = risk["equity"]
    realized = sum(t.pnl for t in trades)
    curve = st.get("equity_curve", [])
    daily = equity - (curve[-2][1] if len(curve) >= 2 else risk.get("day_start_equity", start))
    progress = max(0.0, min(1.0, (equity - start) / (target - start)))
    open_risk = sum(p["shares"] * (p["entry_price"] - p["stop"]) for p in positions.values())
    deployed = sum(last_prices.get(s, p["entry_price"]) * p["shares"] for s, p in positions.items())

    status = []
    if mode == "live" and not settings.get("live_trading_approved"):
        status.append(("warn", "Live trading NOT approved — proposals only"))
    status.append(("ok" if not risk.get("halted") else "bad",
                   "Trading active" if not risk.get("halted") else f"HALTED: {risk.get('halt_reason')}"))
    if risk.get("day_halted"):
        status.append(("bad", "Daily drawdown limit hit — no new entries today"))

    pos_rows = []
    for s, p in positions.items():
        px = last_prices.get(s)
        upl = (px - p["entry_price"]) * p["shares"] if px is not None else None
        pos_rows.append([html.escape(s), f"{p['shares']:g}", _money(p["entry_price"]), _money(px),
                         _money(p["stop"]), _money(p["target"]),
                         f'<span class="{_cls(upl)}">{_money(upl, True)}</span>', html.escape(p["entry_date"])])

    sig_rows = []
    for pl in (signals or {}).get("plans", []):
        sig_rows.append([html.escape(pl["symbol"]), html.escape(pl["setup"].replace("_", " ")), _money(pl["entry"]),
                         _money(pl["stop"]), _money(pl["target"]), f"{pl['shares']:g}", _money(pl["max_loss"]),
                         f"{pl['reward_risk']:.2f}", f"{pl['confidence']}"])
    sig_note = ""
    if signals:
        m = signals.get("market") or {}
        sig_note = (f'<p class="muted">Scan of {html.escape(str(signals.get("day")))} close · market '
                    f'{html.escape(str(m.get("sentiment", "?")))} · {html.escape("; ".join(m.get("reasons", [])))}</p>')
        if signals.get("blocked"):
            sig_note += ('<p class="muted">Signals not taken: ' +
                         html.escape("; ".join(f"{s} ({r})" for s, r in signals["blocked"][:8])) + "</p>")

    j_rows = []
    for t in reversed(trades[-20:]):
        j_rows.append([html.escape(t.exit_time), html.escape(t.symbol), html.escape(t.setup.replace("_", " ")),
                       f"{t.shares:g}", _money(t.entry_price), _money(t.exit_price), html.escape(t.reason_exit),
                       f'<span class="{_cls(t.pnl)}">{_money(t.pnl, True)}</span>', f"{t.r_multiple:+.2f}R",
                       "yes" if t.followed_strategy else "<b>no</b>"])

    bt_html = ""
    if backtest:
        s, bst = backtest["summary"], backtest["summary"]["stats"]
        bt_html = f"""<section class="card"><h2>Backtest (real Robinhood daily bars)</h2>
<p>{html.escape(str(s['period'][0]))} → {html.escape(str(s['period'][1]))}, {len(s['symbols'])} symbols:
<b>{_money(s['starting_balance'])} → {_money(s['final_equity'])}</b> ({s['return_pct']:+.1f}%),
{bst.get('trades', 0)} trades, win rate {bst.get('win_rate', 0):.0%}, avg {bst.get('avg_r', 0):+.2f}R,
max drawdown {bst.get('max_drawdown', 0):.1%}. Target reached: {html.escape(str(s['target_hit_on'] or 'no'))}.</p>
<ul>{''.join(f'<li>{html.escape(w)}</li>' for w in bst.get('weaknesses', []))}</ul></section>"""

    weak = "".join(f"<li>{html.escape(w)}</li>" for w in stats.get("weaknesses", []))
    win_rate = f"{stats['win_rate']:.0%}" if stats.get("trades") else "—"
    pdt = len(risk.get("day_trades", []))
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    pills = "".join(f'<span class="pill {k}">{html.escape(v)}</span>' for k, v in status)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>$25 to $50 Tracker</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--muted:#667085;--line:#e4e7ec;--accent:#2f6fed;--pos:#12805c;--neg:#c4320a;--warn:#b54708;--track:#e9edf5}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0f1115;--card:#171a21;--ink:#e8eaee;--muted:#98a2b3;--line:#2a2f3a;--accent:#6e9bff;--pos:#3ccf91;--neg:#ff7a59;--warn:#f5b54a;--track:#262b36}}}}
:root[data-theme="dark"]{{--bg:#0f1115;--card:#171a21;--ink:#e8eaee;--muted:#98a2b3;--line:#2a2f3a;--accent:#6e9bff;--pos:#3ccf91;--neg:#ff7a59;--warn:#f5b54a;--track:#262b36}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
main{{max-width:980px;margin:0 auto;padding:20px 16px 40px}} h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:0 0 12px}}
.muted{{color:var(--muted);font-size:13px}} .card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-top:14px}}
.pill{{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;margin:6px 6px 0 0;border:1px solid var(--line)}}
.pill.ok{{color:var(--pos)}} .pill.bad{{color:var(--neg)}} .pill.warn{{color:var(--warn)}}
.prog-top{{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap}}
.big{{font-size:34px;font-weight:700;letter-spacing:-.5px}} .bar{{height:14px;background:var(--track);border-radius:999px;overflow:hidden;margin:10px 0 6px}}
.fill{{height:100%;background:var(--accent);border-radius:999px}} .ends{{display:flex;justify-content:space-between;font-size:13px;color:var(--muted)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-top:14px}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}} .tile .k{{font-size:12px;color:var(--muted)}} .tile .v{{font-size:20px;font-weight:650;margin-top:2px}}
.pos{{color:var(--pos)}} .neg{{color:var(--neg)}} .tw{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap}} th{{color:var(--muted);font-weight:600}}
.spark{{width:100%;height:auto}} .spark .line{{fill:none;stroke:var(--accent);stroke-width:2}} .spark .base{{stroke:var(--muted);stroke-dasharray:4 4}} .spark .tgt{{stroke:var(--pos);stroke-dasharray:2 4}}
.axis{{display:flex;justify-content:space-between;font-size:12px;color:var(--muted)}} ul{{margin:6px 0 0 18px;padding:0}}
</style></head><body><main>
<h1>$25 → $50</h1>
<div class="muted">Mode: <b>{html.escape(mode.upper())}</b> · updated {generated}</div>
<div>{pills}</div>
<section class="card"><div class="prog-top"><div><div class="muted">Account equity</div><div class="big">{_money(equity)}</div></div>
<div class="muted">{progress:.0%} of the way to {_money(target)}</div></div>
<div class="bar" role="progressbar" aria-valuemin="{start}" aria-valuemax="{target}" aria-valuenow="{equity:.2f}"><div class="fill" style="width:{progress * 100:.1f}%"></div></div>
<div class="ends"><span>Start {_money(start)}</span><span>Target {_money(target)}</span></div></section>
<div class="grid">
<div class="tile"><div class="k">Daily P&amp;L</div><div class="v {_cls(daily)}">{_money(daily, True)}</div></div>
<div class="tile"><div class="k">Realized P&amp;L</div><div class="v {_cls(realized)}">{_money(realized, True)}</div></div>
<div class="tile"><div class="k">Unrealized P&amp;L</div><div class="v {_cls(unreal)}">{_money(unreal, True)}</div></div>
<div class="tile"><div class="k">Win rate</div><div class="v">{win_rate}</div></div>
<div class="tile"><div class="k">Trades</div><div class="v">{stats.get('trades', 0)}</div></div>
<div class="tile"><div class="k">Open risk</div><div class="v">{_money(open_risk)}</div><div class="k">{(open_risk / equity if equity else 0):.1%} of equity · {_money(deployed)} deployed</div></div>
<div class="tile"><div class="k">Loss streak / day trades</div><div class="v">{risk.get('consecutive_losses', 0)} / {pdt}</div></div>
</div>
<section class="card"><h2>Equity curve</h2>{_sparkline(curve, start, target)}</section>
<section class="card"><h2>Open positions</h2>{_table(['Ticker','Shares','Entry','Last','Stop','Target','Unrealized','Since'], pos_rows, 'No open positions — holding cash.')}</section>
<section class="card"><h2>Current signals</h2>{sig_note}{_table(['Ticker','Setup','Entry','Stop','Target','Shares','Max loss','R:R','Conf.'], sig_rows, 'No setup meets every rule right now — hold cash.')}</section>
<section class="card"><h2>Trade journal</h2>{_table(['Closed','Ticker','Setup','Sh.','Entry','Exit','Exit reason','P&L','R','Rules followed'], j_rows, 'No closed trades yet.')}
<h2 style="margin-top:14px">Weaknesses</h2><ul>{weak}</ul></section>
{bt_html}
<p class="muted">No profits are promised. Every live order needs your approval in chat and again in the Robinhood app.</p>
</main></body></html>"""


def write_dashboard(content: str, mode: str) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"dashboard_{mode}.html"
    path.write_text(content)
    return path


def load_json(path: Path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None
