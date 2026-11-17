# $25 → $50 trading assistant

A disciplined, rules-first trading assistant whose objective is to grow **$25 into $50**
without ever risking more than 2% of equity on a trade. The $50 is an objective, not a
deadline. When nothing meets every rule, the system holds cash.

**No profits are promised.** Backtests below are on real data and are reported as-is.

## What is actually connected (inspected 2026-11-17)

| Integration | Access | Used for |
|---|---|---|
| **Robinhood MCP connector** (claude.ai) | Read + **trade**, but only on the agent-enabled "MCP" account. **Trade approval is ON**: every order must also be approved by you in the Robinhood app. | Quotes, daily/intraday bars, indicators, earnings, order review/placement, positions, orders, P&L |
| Robinhood main account | Read-only to the agent | Not traded |
| **SnapTrade connector** | **Read-only** (cannot place orders) | Cross-checking balances/positions/history |
| Other market-data hosts (Yahoo, Stooq, Alpaca…) | Blocked by the environment's network policy | — |

Broker constraints that shaped the design (from Robinhood's MCP tool rules):

* **Fractional shares only on market orders.** Limit, stop and OCO orders need **whole shares**.
  A fractional position therefore could not carry a broker-held stop-loss, so the system
  trades **whole shares only**. With $25 and a $0.50 risk cap, that limits trades to
  low-priced, low-volatility setups; signals where one share risks more than 2% are skipped.
* **OCO exits** (stop-market + take-profit limit, GTC) protect every position at the broker.
  Legs must be ≥ 0.25% from the market and ≥ $0.10 apart.
* The account is under $25k, so the **pattern-day-trader guard** blocks new entries after
  3 day trades in 5 business days.

## Architecture

```
Robinhood MCP (agent calls) ──bars JSON──▶ trader/data.py ──CSV──▶ data/daily/
                                                          │
        trader/indicators.py  EMA 9/20/50, RSI14, MACD, ATR, VWAP, RVOL, pivots (S/R)
        trader/strategy.py    market regime + 2 setups ──▶ candidate (entry/stop/target)
        trader/risk.py        sizing + hard rules ──▶ TradePlan (all 10 decision fields)
        trader/costs.py       spread, slippage, SEC/FINRA fees
                     │
     ┌───────────────┼──────────────────────────────┐
 backtest.py     paper.py (forward paper)      proposals.py (live)
 real history    simulated fills + OCO         exact MCP calls, double-gated
     └───────────────┴──────────────┬───────────────┘
                     journal.py (every trade + analysis) ──▶ dashboard.py (HTML)
```

The Python code never talks to the broker. The agent (Claude) fetches data and, **only after
you say yes**, sends the order; Robinhood then asks you to approve it again in the app.

### Strategy (long-only, daily bars)

Market filter: SPY above its 50 EMA, SPY ATR ≤ 2.5%, SPY day move ≤ 3%. Otherwise no entries.

* **Momentum breakout:** close above the prior 20-day high, relative volume ≥ 1.5×,
  RSI 55–72, MACD above signal and rising, EMA 9 > 20 > 50.
* **Trend pullback:** dip to the 20 EMA and close back up, RSI 40–58, MACD histogram turning up.
* **Never chase:** skip if > 1 ATR past the breakout, after a > 6% gap, or on a > 2.5 ATR bar;
  entries are next-day *limit* orders, so a gap above the limit means no trade.
* **Stop:** at structure (old resistance / recent swing low), clamped to 1–2 ATR.
  **Target:** ≥ 2:1 reward:risk *after costs*; skipped if a swing-high resistance sits below it.
* **Exit:** OCO stop / target, or a time exit after 10 sessions.
* Confidence score 0–100 (trend, momentum, RSI zone, volume, regime, room to resistance,
  volatility); minimum 60.
* At execution time only (not in the daily backtest): earnings blackout, news check, price
  above session VWAP, tradability/halt check.

### Risk rules (enforced in `trader/risk.py`, tested in `tests/test_risk.py`)

| Rule | Setting |
|---|---|
| Max risk per trade (incl. est. exit costs) | 2% of equity |
| Min reward:risk after costs | 2.0 |
| Stop-loss before entry | required; OCO placed on fill |
| Margin / leverage | never (cash only; leveraged ETFs excluded) |
| Averaging down / adding | never |
| Two consecutive losses | **halt** until you review and reset |
| 5% daily drawdown | no new entries that day |
| Max open positions / total open risk | 2 / 4% |
| Pattern-day-trader guard | ≤ 3 day trades per 5 business days |

## Backtest (real Robinhood daily bars, 2024-12-02 → 2026-11-13)

48 liquid stocks/ETFs + SPY, $25 start, whole shares, costs included, all intrabar
ambiguities resolved against us (stop assumed before target; gaps fill at the open).

| | Result |
|---|---|
| Equity | **$25.00 → $29.16 (+16.6%)** |
| Trades | 12 (9 wins / 3 losses, 75%), avg +0.88R, profit factor 4.7 |
| Max drawdown | 4.9% |
| Halts | 1 (two losses in a row) |
| $50 reached | **No** |

Robustness (`python tools/robustness.py`): every variation finished positive
(+2.6% to +30.2%), both halves of the period were positive, and doubling all costs still
gave +14.5%.

**Read this before trusting it:**

* **12 trades is a tiny sample.** It's suggestive, not proof of an edge.
* **Selection bias:** the symbols were chosen in 2026 from names that are still listed and
  liquid.
* **Slow by design:** at the backtested pace (~8%/year) doubling would take years, not weeks.
  The binding constraint is the 2% risk cap with whole shares on a $25 account. Many valid
  signals were skipped because one share risked more than $0.50. The system will not raise
  risk to go faster.
* VWAP, news and earnings filters can't be tested on daily history; they apply live only.

## Crypto: checked, not adopted (2026-11-17)

Robinhood crypto removes the whole-share problem: fractional amounts can carry a broker-held
stop order, there's no pattern-day-trader rule, and it trades 24/7. But:

* **Costs:** live quotes on market-maker routing showed **~1.9% between bid and ask** on
  BTC, ETH, SOL, DOGE and XRP (~0.95% each way). Stocks here cost ~0.1–0.4% round trip.
  With a typical 5% crypto stop, roughly a quarter of each trade's risk goes to the spread.
* **No app approval:** a crypto order preview on the MCP account returned
  `customer_approval_required: false`, so crypto orders would skip the Robinhood-app approval
  that stock orders get.
* **No crypto history in the MCP:** `tools/crypto_proxy.py` uses the spot ETFs IBIT/ETHA
  as price proxies (real Robinhood bars, weekdays only).

| Dec 2024 → Nov 2026, $25 start | Result |
|---|---|
| Buy & hold Bitcoin ETF (IBIT) | **$17.53 (−30%)**, worst drawdown −53% |
| Buy & hold Ether ETF (ETHA) | **$13.12 (−48%)**, worst drawdown −68% |
| This strategy on IBIT/ETHA, 2% risk, crypto costs | $24.56 (−1.8%): 1 trade (a loss). The trend filter kept it in cash during the decline |

Both proxies are below their 50-day EMA as of 2026-11-13, so the rules would not buy crypto now.
Revisit if BTC regains its trend *and* a cheaper routing (Robinhood app → Settings → Crypto →
Fee Tiers) measurably cuts the spread.

## Usage

```bash
pip install pandas numpy pytest
python -m pytest -q                         # 35 tests
python -m trader backtest                    # reports/backtest_baseline.json
python tools/robustness.py                   # reports/robustness.json
python -m trader paper step                  # forward paper trading (state/paper_state.json)
python -m trader scan --equity 25 --cash 25  # live plans + proposals/<date>.md (never sends orders)
python -m trader dashboard --mode paper      # reports/dashboard_paper.html
python -m trader journal --mode paper        # stats + weaknesses
```

### Daily runbook (agent-operated)

1. **Data:** fetch the latest daily bars for the universe with `get_equity_historicals`
   (10 symbols per call) and `python -m trader import-bars <saved JSON>`. Robinhood may return
   the newest daily bar as `interpolated` until it is finalized; those bars are dropped, so refetch later.
2. **Paper:** `python -m trader paper step`. Each new session is processed, and tomorrow's paper orders are queued.
3. **Live scan** *(only after live approval)*: `get_portfolio` → `python -m trader scan --equity … --cash …`.
4. **Pre-trade checks** per proposal: earnings (`get_earnings_results`), news, tradability,
   live price vs VWAP, not > 1 ATR above the signal close.
5. **Approval:** show you the proposal (all 10 fields). Nothing is sent without your "yes".
   Then `review_equity_order` → `place_equity_order` → you approve in the app.
6. **On fill:** `python -m trader live fill --side buy …` then OCO (`review_advanced_order` →
   `place_advanced_order`) → you approve in the app.
7. **On exit:** `python -m trader live fill --side sell …`, `live sync`, `dashboard --mode live`.

### Live profile in use (2026-11-17)

At the user's request the system now trades live with an **aggressive** profile: 10% max loss
per trade, 15% daily loss stop, pause after 3 straight losses, stop below $15 equity, market
filter ON. Backtest of this profile (same data): $25 → $44.36 (+77%) over two years, but
**−11% over the most recent year** (2 of 9 trades won). See `RUNBOOK.md` for the scheduled checks.

### Enabling live trading

`config/settings.json` ships with `"mode": "paper"` and `"live_trading_approved": false`.
Proposals stay marked `PAPER_ONLY` until both are changed **at your explicit request**.
The live account number is never written to the repo: proposal files use the placeholder
`<RH_ACCOUNT_NUMBER>`, which the agent replaces only when it makes the Robinhood call.
`config/local.json` and `.env` are gitignored for any other local overrides.

## Layout

```
config/settings.json     all parameters (risk, strategy, costs, universe, broker rules)
trader/                  engine package (see Architecture)
tests/                   risk, paper-fill, indicator, engine and live-gate tests
tools/robustness.py      sensitivity analysis (not an optimizer)
data/daily/*.csv         real Robinhood daily bars
reports/                 backtests, robustness, latest signals, dashboards
state/, journal/         paper/live account state and trade journals
proposals/               order proposals awaiting approval
```
