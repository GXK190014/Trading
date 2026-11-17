"""Settings loading. `config/settings.json` is committed; `config/local.json` (gitignored)
can override any key, e.g. to hold the live account number."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.json"
LOCAL_PATH = ROOT / "config" / "local.json"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings(path: Path | None = None) -> dict:
    settings = json.loads((path or SETTINGS_PATH).read_text())
    if path is None and LOCAL_PATH.exists():
        settings = _merge(settings, json.loads(LOCAL_PATH.read_text()))
    return settings


def account_number(settings: dict) -> str | None:
    """The live account number is never committed: it comes from config/local.json
    (account.account_number) or the environment variable named in settings."""
    acct = settings.get("account", {})
    return acct.get("account_number") or os.environ.get(acct.get("account_number_env", ""), None)
