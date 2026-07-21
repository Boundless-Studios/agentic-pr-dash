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


@pytest.fixture(autouse=True)
def _isolate_rate_limit_seen():
    """Reset github_api's per-tick rate-limit-seen flag (BOU-1923 review).

    The flag is process-global and sticky until reset; the cached PR-resolution
    path now reads it to detect a gh outage during the detail fetch. Reset it
    around each test so a rate-limit set by one test can't bleed into another.
    """
    github_api.reset_rate_limit_seen()
    yield
    github_api.reset_rate_limit_seen()


@pytest.fixture(autouse=True)
def _isolate_mutation_pacing():
    """Reset github_api's last-mutation timestamp (BOU-1923 Bucket 4).

    Without resetting this between tests, a mutation call in one test would
    leave a monotonic timestamp behind that a later test's mutation call would
    pace against — an unrelated, order-dependent sleep bleeding across tests.
    """
    github_api.reset_mutation_pacing()
    yield
    github_api.reset_mutation_pacing()


@pytest.fixture
def legacy_marker_writes(monkeypatch):
    """Simulate a pre-Stage-4 install: ``pr-watch.armed`` marker writes enabled.

    BOU-2223 Stage 4 flipped ``markers.marker_writes_enabled()`` OFF by default —
    the claim is now the sole ownership authority and the marker is a read-only
    compatibility shim. A test whose subject IS that legacy marker (its on-disk
    shape, the writer's re-stamp/takeover behaviour, or a marker-vs-claim parity
    comparison) opts back into the old dual-write with this fixture so it keeps
    exercising a real pre-Stage-4 arm rather than the retired writer's absence.
    """
    monkeypatch.setenv("AGENTIC_PR_DASH_MARKER_WRITES", "1")
    yield


@pytest.fixture(autouse=True)
def _isolate_resolve_fallback_logged():
    """Reset github_api's once-per-process resolve-fallback log flag (BOU-1974).

    Without resetting this, a test earlier in the run that triggers the
    App-token FORBIDDEN fallback would leave the flag set, so a later test
    asserting on the fallback log message would see nothing printed.
    """
    github_api.reset_resolve_fallback_logged()
    yield
    github_api.reset_resolve_fallback_logged()
