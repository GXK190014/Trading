"""Command line: python -m trader <command>

  import-bars FILE...            convert saved Robinhood get_equity_historicals JSON to CSVs
  backtest [--start --end]       backtest on the CSVs in data/daily
  paper step                     forward paper trading: process new sessions, queue tomorrow's orders
  scan --equity E --cash C       live scan: trade plans + order proposals (never sends orders)
  live sync --equity E --cash C  record the broker's current equity/cash
  live fill ...                  record a real fill reported by Robinhood
  live reset-halt --reviewed     resume after a loss-streak halt (only after reviewing the journal)
  dashboard [--mode paper|live]  write reports/dashboard_<mode>.html
  journal [--mode paper|live]    performance analysis and weaknesses
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .backtest import REPORTS, run_backtest, save_report, summary_markdown
from .config import load_settings
from .costs import CostModel
from .dashboard import build_dashboard, load_json, write_dashboard
from .data import import_robinhood_json, load_universe
from .engine import Engine
from .journal import Journal, TradeRecord, analyze
from .paper import Position
from .proposals import build_proposal, live_enabled, proposal_markdown, save_proposals
from .risk import RiskManager
from .state import broker_from_state, load_state, risk_from_state, save_state


def _last_prices(data) -> dict[str, float]:
    return {s: float(df["close"].iloc[-1]) for s, df in data.items() if len(df)}


def _signals_payload(res) -> dict:
    return {
        "day": res.day,
        "market": res.market.as_dict() if res.market else None,
        "plans": [p.as_dict() for p in res.plans],
        "blocked": res.blocked,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def cmd_import(args, settings):
    for f in args.files:
        print(f, "->", import_robinhood_json(f))


def cmd_backtest(args, settings):
    data = load_universe()
    res = run_backtest(settings, data, args.start, args.end)
    path = save_report(res, args.name)
    print(summary_markdown(res, f"Backtest: {args.name}"))
    print(f"\nreport: {path}")


def cmd_paper(args, settings):
    data = load_universe()
    st = load_state("paper", settings)
    broker, risk_state = broker_from_state(st, settings), risk_from_state(st)
    eng = Engine(settings, data, mode="paper", broker=broker, state=risk_state, journal=Journal("paper"))
    eng.entry_fees = st.get("entry_fees", {})
    eng.halts = st.get("halts", 0)
    days = eng.trading_days()
    if not days:
        sys.exit("no data")
    last = st.get("last_processed")
    new_days = [d for d in days if last is None or d.strftime("%Y-%m-%d") > last]
    if last is None:
        new_days = new_days[-1:]  # start paper trading from the latest session, not the past
    if not new_days:
        print(f"paper: nothing new since {last}; import fresh bars first")
    res = None
    for d in new_days:
        res = eng.step(d, generate=True)
        st["equity_curve"].append([res.day, round(res.equity, 4)])
        for f in res.fills:
            print(f"{res.day} {f.kind:5s} {f.symbol} {f.shares:g} @ {f.price:.2f} ({f.reason})")
    if res is not None:
        st["last_processed"] = res.day
        (REPORTS / "latest_signals_paper.json").parent.mkdir(parents=True, exist_ok=True)
        (REPORTS / "latest_signals_paper.json").write_text(json.dumps(_signals_payload(res), indent=2, default=str))
        print(f"paper equity {res.equity:.2f}; queued for next session: "
              f"{[p.symbol for p in res.plans] or 'nothing (hold cash)'}")
        if res.market:
            print("market:", "; ".join(res.market.reasons))
    st["broker"], st["risk"] = eng.broker.to_dict(), eng.state.to_dict()
    st["entry_fees"], st["halts"] = eng.entry_fees, eng.halts
    save_state("paper", st)


def cmd_market(args, settings):
    """Fast market-filter check from one live SPY price (no bulk data needed)."""
    from .data import load_daily
    from .indicators import ema
    sc = settings["strategy"]
    spy = load_daily(sc["market_symbol"])
    prev_ema = float(ema(spy["close"], sc["ema_slow"]).iloc[-1])
    prev_close = float(spy["close"].iloc[-1])
    alpha = 2.0 / (sc["ema_slow"] + 1)
    ema_now = prev_ema + alpha * (args.spy - prev_ema)
    gap, day = args.spy / ema_now - 1, args.spy / prev_close - 1
    trend_ok = gap > 0 or not sc.get("market_require_uptrend", True)
    calm = abs(day) <= sc["market_max_abs_day_move"]
    verdict = "PASS: scan for setups" if trend_ok and calm else "FAIL: no new buys"
    print(f"SPY {args.spy:.2f} vs 50-EMA {ema_now:.2f} ({gap:+.2%}), today {day:+.2%} "
          f"(last daily bar {spy.index[-1].date()}) -> market filter {verdict}")


def cmd_scan(args, settings):
    data = load_universe(provisional=args.provisional)
    if args.provisional:
        print("Using TODAY'S PROVISIONAL bars (session still open): signals can change by the close.")
    st = load_state("live", settings)
    rs = risk_from_state(st)
    if args.equity is not None:
        rs.equity = args.equity
    if args.cash is not None:
        rs.cash = args.cash
    eng = Engine(settings, data, mode="live", state=rs, earnings=_load_earnings())
    day = eng.trading_days()[-1]
    eng.risk.start_day(eng.state, day.strftime("%Y-%m-%d"))
    from .engine import SessionResult
    res = SessionResult(day.strftime("%Y-%m-%d"), equity=rs.equity)
    eng._signals(day, res.day, res)
    payload = _signals_payload(res)
    (REPORTS / "latest_signals_live.json").parent.mkdir(parents=True, exist_ok=True)
    (REPORTS / "latest_signals_live.json").write_text(json.dumps(payload, indent=2, default=str))
    proposals = [build_proposal(p, settings) for p in res.plans]
    path = save_proposals(proposals, res.day)
    print("market:", "; ".join(res.market.reasons) if res.market else "?")
    if not proposals:
        print("No setup meets every rule. HOLD CASH.")
    for p in proposals:
        print(proposal_markdown(p), "\n")
    if res.blocked:
        print("not taken:", "; ".join(f"{s} ({r})" for s, r in res.blocked))
    if not live_enabled(settings):
        print("\nLIVE TRADING NOT APPROVED: these are proposals only.")
    print(f"proposals: {path}")


def _load_earnings() -> dict:
    p = Path(__file__).resolve().parent.parent / "data" / "earnings.json"
    return json.loads(p.read_text()) if p.exists() else {}


def cmd_live(args, settings):
    st = load_state("live", settings)
    costs = CostModel.from_settings(settings["costs"])
    rm = RiskManager(settings["risk"], costs)
    rs = risk_from_state(st)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if args.action == "sync":
        rm.start_day(rs, today)
        rm.update_equity(rs, args.equity, args.cash)
        st["broker"]["cash"] = args.cash
        curve = st["equity_curve"]
        if curve and curve[-1][0] == today:
            curve[-1][1] = args.equity
        else:
            curve.append([today, args.equity])
        print(f"live: equity {args.equity:.2f}, cash {args.cash:.2f}, halted={rs.halted}, day_halted={rs.day_halted}")
    elif args.action == "fill":
        day = (args.time or today)[:10]
        positions = st["broker"]["positions"]
        if args.side == "buy":
            if args.symbol in positions:
                sys.exit("already holding this symbol: adding/averaging down is not allowed")
            plan = _find_plan(args.symbol) or {}
            stop = args.stop or plan.get("stop")
            target = args.target or plan.get("target")
            if stop is None or target is None:
                sys.exit("no stop/target: pass --stop and --target (every position needs a stop)")
            plan = {**plan, "stop": stop, "target": target}
            pos = Position(args.symbol, args.qty, args.price, day, stop, target, plan.get("setup", "manual"), plan)
            positions[args.symbol] = pos.__dict__
            st["broker"]["cash"] -= args.qty * args.price
            rm.register_entry(rs, args.symbol, args.qty, args.price, stop)
            print(f"recorded BUY {args.qty:g} {args.symbol} @ {args.price:.2f}; stop {stop}, target {target}")
        else:
            p = positions.pop(args.symbol, None)
            if p is None:
                sys.exit("no open position in that symbol")
            fees = costs.sell_fees(args.price, args.qty)
            pnl = (args.price - p["entry_price"]) * args.qty - fees
            st["broker"]["cash"] += args.price * args.qty - fees
            planned = p["plan"].get("max_loss") or (p["entry_price"] - p["stop"]) * p["shares"]
            rec = TradeRecord("live", args.symbol, p["setup"], p["entry_date"], p["entry_price"], day, args.price,
                              args.qty, p["stop"], p["target"], "; ".join(p["plan"].get("reasons", [])),
                              args.reason or "manual", round(pnl, 4), round(fees, 4),
                              round(pnl / planned, 2) if planned else 0.0, rs.equity,
                              int(p["plan"].get("confidence", 0)), not args.rule_break,
                              [args.note] if args.note else [])
            Journal("live").append(rec)
            rm.record_exit(rs, args.symbol, pnl, day, p["entry_date"])
            print(f"recorded SELL {args.qty:g} {args.symbol} @ {args.price:.2f}: P&L {pnl:+.2f}"
                  + (f" -- HALTED: {rs.halt_reason}" if rs.halted else ""))
    elif args.action == "reset-halt":
        if not args.reviewed:
            sys.exit("review the journal first (python -m trader journal --mode live), then pass --reviewed")
        rm.manual_reset(rs)
        print("halt cleared")
    st["risk"] = rs.to_dict()
    save_state("live", st)


def _find_plan(symbol: str) -> dict | None:
    pdir = Path(__file__).resolve().parent.parent / "proposals"
    for f in sorted(pdir.glob("*.json"), reverse=True):
        for p in json.loads(f.read_text()):
            if p["plan"]["symbol"] == symbol:
                return p["plan"]
    return None


def cmd_dashboard(args, settings):
    data = load_universe()
    bt = load_json(REPORTS / "backtest_baseline.json")
    if args.mode == "backtest":
        # Replay view: the dashboard populated with the baseline backtest's real-data trades.
        if bt is None:
            sys.exit("run `python -m trader backtest` first")
        final = bt["summary"]["final_equity"]
        from .risk import RiskState
        st = {"risk": RiskState(equity=final, cash=final, day_start_equity=final).to_dict(),
              "broker": {"cash": final, "positions": {}, "pending": []}, "equity_curve": bt["equity_curve"]}
        trades = [TradeRecord(**t) for t in bt["trades"]]
        content = build_dashboard(settings, "backtest", st, {}, None, bt, trades)
    else:
        st = load_state(args.mode, settings)
        signals = load_json(REPORTS / f"latest_signals_{args.mode}.json")
        content = build_dashboard(settings, args.mode, st, _last_prices(data), signals, bt)
    print(write_dashboard(content, args.mode))


def cmd_journal(args, settings):
    trades = Journal(args.mode).load()
    st = load_state(args.mode, settings)
    print(json.dumps(analyze(trades, [v for _, v in st.get("equity_curve", [])]), indent=2))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="trader", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("import-bars"); p.add_argument("files", nargs="+"); p.set_defaults(fn=cmd_import)
    p = sub.add_parser("backtest"); p.add_argument("--start"); p.add_argument("--end")
    p.add_argument("--name", default="backtest_baseline"); p.set_defaults(fn=cmd_backtest)
    p = sub.add_parser("paper"); p.add_argument("action", choices=["step"]); p.set_defaults(fn=cmd_paper)
    p = sub.add_parser("scan"); p.add_argument("--equity", type=float); p.add_argument("--cash", type=float)
    p.add_argument("--provisional", action="store_true", help="include today's partial bar (intraday check)")
    p.set_defaults(fn=cmd_scan)
    p = sub.add_parser("market"); p.add_argument("--spy", type=float, required=True, help="live SPY price")
    p.set_defaults(fn=cmd_market)
    p = sub.add_parser("live"); p.add_argument("action", choices=["sync", "fill", "reset-halt"])
    p.add_argument("--equity", type=float); p.add_argument("--cash", type=float)
    p.add_argument("--symbol"); p.add_argument("--side", choices=["buy", "sell"]); p.add_argument("--qty", type=float)
    p.add_argument("--price", type=float); p.add_argument("--time"); p.add_argument("--stop", type=float)
    p.add_argument("--target", type=float); p.add_argument("--reason"); p.add_argument("--note")
    p.add_argument("--rule-break", action="store_true", help="mark the trade as not following the strategy")
    p.add_argument("--reviewed", action="store_true"); p.set_defaults(fn=cmd_live)
    p = sub.add_parser("dashboard"); p.add_argument("--mode", choices=["paper", "live", "backtest"], default="paper")
    p.set_defaults(fn=cmd_dashboard)
    p = sub.add_parser("journal"); p.add_argument("--mode", choices=["paper", "live", "backtest"], default="paper")
    p.set_defaults(fn=cmd_journal)
    args = ap.parse_args(argv)
    args.fn(args, load_settings())


if __name__ == "__main__":
    main()
