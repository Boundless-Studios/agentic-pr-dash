"""BOU-3095 — the board's OOB flag must not leak into the cached context.

``/partials/board`` sets ``board_oob`` so the header slots refresh out-of-band
with the board poll. It was setting it on the dict ``_dashboard_context_async``
returns — which is the *cached* dict — so once any partial poll had run, every
later full-page render also emitted those out-of-band slots. The page then
carried two elements with the same id, and htmx swaps the first match, so the
header indicator stopped updating and a copy appeared inside the board.

Caught in the browser while validating the observation-age indicator.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from agentic_pr_dash import app as app_module


@pytest.fixture(autouse=True)
def _clean_context_cache():
    app_module._dashboard_context_cache.clear()
    app_module._dashboard_context_tasks.clear()
    yield
    app_module._dashboard_context_cache.clear()
    app_module._dashboard_context_tasks.clear()


def test_board_partial_does_not_mutate_the_cached_context() -> None:
    async def scenario() -> None:
        await app_module._dashboard_context_async()
        cached = app_module._dashboard_context_cache[(False, "board")][1]
        assert "board_oob" not in cached

        client = TestClient(app_module.app)
        client.get("/partials/board")

        assert "board_oob" not in cached, (
            "the partial poll set board_oob on the cached context, so every "
            "later full-page render emits duplicate out-of-band slot ids"
        )

    asyncio.run(scenario())


def test_full_page_renders_each_oob_slot_exactly_once() -> None:
    client = TestClient(app_module.app)

    # A partial poll first — that is what used to poison the cache.
    client.get("/partials/board")
    page = client.get("/").text

    assert page.count('id="observation-age-slot"') == 1
    assert page.count('id="escalation-banner-slot"') == 1
    assert "hx-swap-oob" not in page


def test_board_partial_still_emits_the_oob_slots() -> None:
    client = TestClient(app_module.app)

    partial = client.get("/partials/board").text

    assert partial.count('id="observation-age-slot"') == 1
    assert partial.count('id="escalation-banner-slot"') == 1
    assert 'hx-swap-oob="true"' in partial
