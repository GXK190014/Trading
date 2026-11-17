import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.config import load_settings  # noqa: E402
from trader.costs import CostModel  # noqa: E402


CONSERVATIVE_RISK = dict(max_risk_per_trade_pct=0.02, min_reward_risk=2.0, max_consecutive_losses=2,
                         max_daily_drawdown_pct=0.05, max_open_positions=2, max_total_open_risk_pct=0.04,
                         max_position_pct=0.90, equity_floor=0.0)


@pytest.fixture
def settings():
    """The committed settings with the conservative risk profile pinned, so these tests check the
    rules themselves rather than whichever profile config/settings.json currently holds."""
    s = load_settings(Path(__file__).resolve().parent.parent / "config" / "settings.json")
    s["risk"].update(CONSERVATIVE_RISK)
    return s


@pytest.fixture
def live_settings():
    """The settings exactly as committed (the profile actually used for live trading)."""
    return load_settings(Path(__file__).resolve().parent.parent / "config" / "settings.json")


@pytest.fixture
def costs(settings):
    return CostModel.from_settings(settings["costs"])
