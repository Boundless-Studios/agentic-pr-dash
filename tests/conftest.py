"""Test isolation.

`config.load()` now falls back to a global `~/.config/agentic-pr-dash/config.toml`
and reads `AGENTIC_PR_DASH_*` / legacy `GAIA_*` env vars. To keep tests hermetic
(asserting on *defaults*), point HOME at an empty temp dir and strip those env
vars before every test, and clear the lru_cache around each test.
"""

import os

import pytest

from agentic_pr_dash import config


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    for var in list(os.environ):
        if var.startswith(("AGENTIC_PR_DASH_", "GAIA_")):
            monkeypatch.delenv(var, raising=False)
    config.load.cache_clear()
    yield
    config.load.cache_clear()
