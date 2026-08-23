"""
Shared pytest fixtures.

The exchange restores season state from SEASON_PATH the moment an
ExchangeServer is constructed (that is the point of a season). Without
isolation, any real data/season.json on the machine would leak starting
capital and positions into every test that builds a server. This redirects
season persistence to a throwaway directory for the whole test run.
"""

import os
import tempfile

# Must be set BEFORE plugins.securities.defaults is first imported: a real
# data/base_prices.json (make sync-prices) would otherwise re-level every
# security at import time and break tests that assume the static bases.
os.environ["BASE_PRICES_PATH"] = os.path.join(
    tempfile.gettempdir(), "algoarena-tests-no-base-prices.json")

import pytest

import exchange.persistence as persistence


@pytest.fixture(autouse=True)
def _isolate_season_state(monkeypatch, tmp_path_factory):
    """Point season persistence at a per-test temporary file."""
    d = tmp_path_factory.mktemp("season")
    monkeypatch.setattr(persistence, "SEASON_PATH", str(d / "season.json"))
    yield


@pytest.fixture(autouse=True)
def _no_scenario_env(monkeypatch):
    """Tests must not inherit a week from the developer's shell."""
    monkeypatch.delenv("GAME_WEEK", raising=False)
    monkeypatch.delenv("SCENARIO_PATH", raising=False)
    yield
