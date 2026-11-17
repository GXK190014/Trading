"""Persistent account state for forward paper trading and live tracking."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .config import ROOT
from .costs import CostModel
from .paper import PaperBroker
from .risk import RiskState

STATE_DIR = ROOT / "state"


def state_path(mode: str) -> Path:
    return STATE_DIR / f"{mode}_state.json"


def load_state(mode: str, settings: dict) -> dict:
    p = state_path(mode)
    if p.exists():
        return json.loads(p.read_text())
    start = settings["account"]["starting_balance"]
    return {
        "mode": mode,
        "last_processed": None,
        "risk": RiskState(equity=start, cash=start, day_start_equity=start).to_dict(),
        "broker": {"cash": start, "positions": {}, "pending": []},
        "entry_fees": {},
        "equity_curve": [],
        "halts": 0,
    }


def save_state(mode: str, st: dict) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = state_path(mode)
    p.write_text(json.dumps(st, indent=2, default=str))
    return p


def broker_from_state(st: dict, settings: dict) -> PaperBroker:
    return PaperBroker.from_dict(st["broker"], CostModel.from_settings(settings["costs"]),
                                 settings["strategy"]["max_hold_days"])


def risk_from_state(st: dict) -> RiskState:
    return RiskState.from_dict(st["risk"])
