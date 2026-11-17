import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.config import load_settings  # noqa: E402
from trader.costs import CostModel  # noqa: E402


@pytest.fixture
def settings():
    return load_settings(Path(__file__).resolve().parent.parent / "config" / "settings.json")


@pytest.fixture
def costs(settings):
    return CostModel.from_settings(settings["costs"])
