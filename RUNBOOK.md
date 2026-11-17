# Live trading runbook (for Claude's scheduled checks)

Profile: `config/settings.json` → `mode: live`, `live_trading_approved: true` (user's request,
2026-11-17: "be my trader, research and execute, I'm OK with risk"). Aggressive limits:
10% max loss per trade, 15% daily loss stop, pause after 3 losses in a row, stop below $15,
2 positions max, whole shares, no margin, no averaging down, SPY 50-EMA market filter ON.

Account: the Robinhood account with `agentic_allowed=true` from `get_accounts` (nickname "MCP").
Never write its number into the repo; proposals hold `<RH_ACCOUNT_NUMBER>`.
Stock orders need the user's approval in the Robinhood app. **Crypto orders do not, and the
system does not trade crypto (spread ~1.9%, see README).**

## Evening check (weekdays ~4:20 PM ET)

1. **Account:** `get_portfolio`, `get_equity_positions`, `get_equity_orders` (state_group open,
   then created today), `get_advanced_orders`.
2. **Reconcile:**
   - entry filled but no OCO → `review_advanced_order` then `place_advanced_order`
     (sell, same qty, take-profit = plan target, stop = plan stop, gtc) and
     `python -m trader live fill --side buy --symbol S --qty Q --price P --time YYYY-MM-DD`
   - position closed (OCO leg filled) → `python -m trader live fill --side sell ... --reason "take-profit|stop-loss"`
   - position held 10 sessions → cancel its OCO (`cancel_advanced_order`), sell (limit at bid), record the fill
   - `python -m trader live sync --equity <total_value> --cash <buying_power>`
3. **Data:** for the symbols in `data/daily/` (batches of 10), fetch `get_equity_historicals`
   interval `30minute`, start = 10 calendar days ago (large results save to a file), then
   `python -m trader import-bars <files>` and
   `python -c "from trader.data import fill_daily_from_intraday as f; print(len(f()))"`.
   (Robinhood's newest *daily* bar is interpolated for hours; the 30-minute rebuild is real data.)
4. **Scan:** `python -m trader scan --equity E --cash C` → `proposals/<date>.md`.
5. **Tell the user** in one short message: account value, open positions, tomorrow's planned trades
   (or "no setups, holding cash" plus the reason).
6. `python -m trader dashboard --mode live`; commit and push `state/ journal/ proposals/ data/ reports/`.

## Morning check (weekdays ~10:07 AM ET)

1. Newest `proposals/<date>.json`. No plans → stop quietly unless positions need attention.
2. For each plan, all must pass or skip it (never chase, never loosen):
   - `get_equity_quotes`: last price ≤ plan entry and ≥ plan stop + 25% of (entry − stop)
   - `get_equity_tradability` on the account: tradable, not halted
   - `get_earnings_results` / `get_earnings_calendar`: no earnings within 3 trading days
   - news check (web search) for a negative catalyst
   - `get_equity_technical_indicators` type `vwap`, interval `5minute`, output `latest`: price ≥ VWAP
3. `review_equity_order`: buy, limit = min(plan entry, current ask), whole-share qty from the plan
   (re-size with `RiskManager` if equity changed), gfd, regular_hours.
   If `customer_approval_required` and no order_checks → `place_equity_order` (goes to the
   user's app). Tell the user to approve it. Any alert (PDT, buying power) → stop and report.
4. When filled → OCO as in evening step 2, then record the fill.
