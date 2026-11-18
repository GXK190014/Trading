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

## SPY dip-buy position (approved by the user 2026-11-17)

Strategy: `tools/dip_buy_backtest.py` (19-year test). Entry order `6afd1da7-33d1-4bca-a00f-b01da5c9a380`:
buy $24.50 SPY, fractional market order at the 2026-11-18 open.

- **After it fills** (`get_equity_orders` order_id above): record it with
  `python -m trader live fill --side buy --symbol SPY --qty <filled qty> --price <avg price>
  --time 2026-11-18 --stop <avg price × 0.95> --target <current SPY 5-day average>` and tell the user the fill.
- **Every evening check**, using SPY's official close (`get_equity_quotes` → `close.price`) and
  `data/long/SPY.csv` updated with it:
  - close **above its 5-day simple average** → exit signal, or
  - close **≤ 95% of the entry price** → protective exit.
  On either, place a fractional **market sell of the full position** (`quantity` = shares held,
  gfd, regular_hours) so it fills at the next open. The user approved these exits with the
  trade, so no new "go" is needed; tell the user what was sent and why. Record the fill with
  `python -m trader live fill --side sell ...` once it executes.
- New dip-buy entries (SPY close above its 200-day average and 2-day RSI below 10) always
  need a fresh "go" from the user.

## Intraday check (every 30 min, weekdays ~10:07 AM – 3:37 PM ET)

1. `get_equity_quotes` for SPY, then `python -m trader market --spy <last_trade_price>`.
2. **FAIL** → one line to the user (SPY vs its 50-EMA, today's move) and stop. No bulk data.
3. **PASS** → refresh `30minute` bars for the universe (as in evening step 3), then
   `python -c "from trader.data import fill_daily_from_intraday as f, build_provisional as b; f(); print(b())"`
   and `python -m trader scan --provisional --equity E --cash C`.
4. Before 3:00 PM ET: report setups that are *forming* (they can still fail by the close). No orders.
5. From 3:00 PM ET: for each plan, run the morning pre-trade checks. If a plan passes, message:
   "Ready to execute: BUY N SYM limit $X, stop $Y, target $Z, max loss $L. Reply **go**."
   Place it only after the user's "go" (the order then also needs approval in the Robinhood app),
   and put the OCO on as soon as it fills, before 4:00 PM.

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
