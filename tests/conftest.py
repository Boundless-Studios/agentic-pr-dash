"""Test isolation.

`config.load()` now falls back to a global `~/.config/agentic-pr-dash/config.toml`
and reads `AGENTIC_PR_DASH_*` / legacy `GAIA_*` env vars. To keep tests hermetic
(asserting on *defaults*), point HOME at an empty temp dir and strip those env
vars before every test, and clear the lru_cache around each test.
"""

import os

import pytest

from agentic_pr_dash import config, github_api


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    for var in list(os.environ):
        if var.startswith(("AGENTIC_PR_DASH_", "GAIA_")):
            monkeypatch.delenv(var, raising=False)
    # The session_ledger default dir (_DEFAULT_DIR) is import-frozen to the REAL
    # ~/.gaia, so the HOME override above does NOT redirect it — a test would read
    # the developer's real ledger (e.g. the BOU-1924 ownership gate resolving THIS
    # live session as a PR's owner). Pin it under the temp home so every test's
    # ledger/claim access is hermetic. Individual tests may still override.
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(home / ".gaia" / "pr-watch" / "ledger"))
    config.load.cache_clear()
    yield
    config.load.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_rate_limit_backoff():
    """Reset github_api's process-level rate-limit-backoff toggle (BOU-1953).

    ``_stop_gate_impl`` disables backoff as a side effect of running; without
    resetting it, one test invoking the stop-gate would silently disable
    backoff for every test that runs after it in the same pytest process.
    """
    github_api.set_rate_limit_backoff(True)
    yield
    github_api.set_rate_limit_backoff(True)
