"""The service worker must never cache or replay live data (BOU-2193).

The dashboard registers a service worker whose original ``fetch`` handler cached
every GET and, on any network failure, fell back to ``caches.match(request)``.
That included ``/partials/board``. So when the dashboard server died, the board
kept receiving a *stale cached 200*: htmx swapped in old content, no error event
fired, and the "Live" chip stayed green. The dashboard looked healthy and current
while showing data from hours earlier — the reported "stuck in view" symptom.

Verified in a browser before the fix: with the server killed, ``caches.keys()``
held ``/partials/board``, ``/partials/event-log``, ``/partials/runner-fleet`` and
``/partials/bug-bash-banner``, and the board kept rendering.

There is no JS test runner in this repo, so this asserts the policy on the source
text. It is deliberately coarse — it guards the invariant (live endpoints bypass
the cache entirely) rather than the implementation.
"""

from pathlib import Path

import pytest

SW = Path(__file__).resolve().parents[1] / "src" / "agentic_pr_dash" / "static" / "sw.js"
SHELL_CACHE_LIST_END = "];"


@pytest.fixture(scope="module")
def sw_source() -> str:
    return SW.read_text()


def test_service_worker_exists(sw_source):
    assert sw_source.strip(), "sw.js must not be empty"


@pytest.mark.parametrize("live_prefix", ["/partials/", "/api/"])
def test_live_endpoints_bypass_the_cache(sw_source, live_prefix):
    """Live-data requests must be recognised and short-circuited."""
    assert live_prefix in sw_source, (
        f"sw.js must name {live_prefix} so those requests can bypass the cache; "
        "caching them replays a stale board as a fresh 200 when the server is down"
    )


def test_fetch_handler_returns_early_for_live_data(sw_source):
    """The bypass must be a bare `return` — NOT respondWith(cache fallback).

    Returning from the fetch handler without calling respondWith lets the request
    go to the network untouched, so a failure reaches htmx as a failure.
    """
    assert "isLiveData" in sw_source, "expected an explicit live-data predicate"

    fetch_handler = sw_source.split('addEventListener("fetch"', 1)[1]
    bypass_index = fetch_handler.find("isLiveData")
    respond_index = fetch_handler.find("respondWith")

    assert bypass_index != -1 and respond_index != -1
    assert bypass_index < respond_index, (
        "the live-data bypass must come BEFORE respondWith, otherwise live requests "
        "still get the cache-fallback treatment"
    )


def test_shell_precache_contains_no_live_endpoints(sw_source):
    """The precache list must be static assets only."""
    shell = sw_source.split("const SHELL", 1)[1].split(SHELL_CACHE_LIST_END, 1)[0]

    assert "/partials/" not in shell, "live partials must never be precached"
    assert "/api/" not in shell, "API endpoints must never be precached"


def test_cache_version_was_bumped_past_the_polluted_one(sw_source):
    """v2 caches on existing installs hold stale partials.

    The activate handler deletes every cache whose name != CACHE, so the version
    must move off "pr-dash-v2" for already-installed clients to be cleaned up.
    """
    assert '"pr-dash-v2"' not in sw_source, (
        "cache name must be bumped past pr-dash-v2 so existing clients purge the "
        "cache that contains stale /partials/ responses"
    )
