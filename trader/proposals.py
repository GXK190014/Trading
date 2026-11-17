"""Order proposals for manual approval.

The engine never sends orders. For each plan it writes the exact Robinhood MCP
calls the agent would make, in order, and each call is gated:

  1. review_equity_order   (simulation: buying power, PDT, halts)
  2. place_equity_order    -- only after the user says "yes" in chat; Robinhood then
                              asks for a second approval in its app
  3. review_advanced_order -- after the entry FILLS: sell OCO (stop-loss + take-profit)
  4. place_advanced_order  -- same double approval

Whole shares only: Robinhood takes fractional quantities on market orders only, so
a fractional position could not carry a broker-held stop-loss.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from .config import ROOT
from .strategy import TradePlan

# Proposal files are committed to git, so they never contain the real account number.
# The agent substitutes it only at the moment it makes the MCP call.
ACCOUNT_PLACEHOLDER = "<RH_ACCOUNT_NUMBER>"

PROPOSALS_DIR = ROOT / "proposals"


def live_enabled(settings: dict) -> bool:
    return settings.get("mode") == "live" and settings.get("live_trading_approved") is True


def oco_checks(plan: TradePlan, rules: dict, ref_price: float | None = None) -> list[str]:
    ref = ref_price or plan.entry
    problems = []
    if (ref - plan.stop) / ref < rules["oco_min_distance_pct"]:
        problems.append("stop is closer than 0.25% to the market (Robinhood rejects the OCO)")
    if (plan.target - ref) / ref < rules["oco_min_distance_pct"]:
        problems.append("target is closer than 0.25% to the market (Robinhood rejects the OCO)")
    if plan.target - plan.stop < rules["oco_min_leg_gap"]:
        problems.append("OCO legs are less than $0.10 apart (Robinhood rejects the OCO)")
    if plan.shares != int(plan.shares):
        problems.append("OCO and limit orders need whole shares")
    return problems


def build_proposal(plan: TradePlan, settings: dict) -> dict:
    acct = ACCOUNT_PLACEHOLDER
    qty = str(int(plan.shares))
    entry_params = {
        "account_number": acct, "symbol": plan.symbol, "side": "buy", "type": "limit",
        "quantity": qty, "limit_price": f"{plan.entry:.2f}", "time_in_force": "gfd",
        "market_hours": "regular_hours",
    }
    oco_params = {
        "account_number": acct, "symbol": plan.symbol, "side": "sell", "quantity": qty,
        "take_profit_limit_price": f"{plan.target:.2f}", "stop_loss_stop_price": f"{plan.stop:.2f}",
        "time_in_force": "gtc", "market_hours": "regular_hours",
    }
    ref = f"{plan.symbol}-{plan.signal_date}-{uuid.uuid4().hex[:8]}"
    enabled = live_enabled(settings)
    return {
        "id": ref,
        "status": "AWAITING_USER_APPROVAL" if enabled else "PAPER_ONLY: live trading not approved",
        "plan": plan.as_dict(),
        "oco_problems": oco_checks(plan, settings["broker_rules"]),
        "steps": [
            {"step": 1, "tool": "mcp__Robinhood__review_equity_order", "params": entry_params,
             "gate": "pre-trade checks in plan.pre_trade_checks pass"},
            {"step": 2, "tool": "mcp__Robinhood__place_equity_order",
             "params": {**entry_params, "ref_id": ref + "-entry"},
             "gate": "explicit user 'yes' in chat, then approval in the Robinhood app"},
            {"step": 3, "tool": "mcp__Robinhood__review_advanced_order", "params": oco_params,
             "gate": "entry order FILLED (get_equity_orders)"},
            {"step": 4, "tool": "mcp__Robinhood__place_advanced_order",
             "params": {**oco_params, "ref_id": ref + "-oco"},
             "gate": "explicit user 'yes' in chat, then approval in the Robinhood app"},
        ],
    }


def proposal_markdown(p: dict) -> str:
    pl = p["plan"]
    lines = [
        f"### {pl['symbol']} -- {pl['setup'].replace('_', ' ')} ({p['status']})",
        f"| Field | Value |", "|---|---|",
        f"| Entry (buy limit, day order) | ${pl['entry']:.2f} |",
        f"| Stop-loss (stop-market, GTC) | ${pl['stop']:.2f} |",
        f"| Take-profit (limit, GTC) | ${pl['target']:.2f} |",
        f"| Position size | {pl['shares']:g} share(s) = ${pl['position_value']:.2f} |",
        f"| Max loss (incl. est. costs) | ${pl['max_loss']:.2f} |",
        f"| Potential profit (net) | ${pl['potential_profit']:.2f} |",
        f"| Reward:risk (net) | {pl['reward_risk']:.2f} : 1 |",
        f"| Confidence | {pl['confidence']}/100 |",
        f"| Market | {pl['sentiment']['sentiment']} (breadth {pl['sentiment'].get('breadth_above_50ema')}) |",
        "", "**Why:** " + "; ".join(pl["reasons"]),
        "", "**Indicators:** " + ", ".join(f"{k} {v}" for k, v in pl["indicators"].items() if v is not None),
        "", "**Before placing:** " + "; ".join(pl["pre_trade_checks"]),
    ]
    if pl["warnings"]:
        lines += ["", "**Warnings:** " + "; ".join(pl["warnings"])]
    if p["oco_problems"]:
        lines += ["", "**OCO problems:** " + "; ".join(p["oco_problems"])]
    return "\n".join(lines)


def save_proposals(proposals: list[dict], day: str) -> Path:
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    path = PROPOSALS_DIR / f"{day}.json"
    path.write_text(json.dumps(proposals, indent=2, default=str))
    (PROPOSALS_DIR / f"{day}.md").write_text("\n\n".join(proposal_markdown(p) for p in proposals) or "No trades.")
    return path
