"""BOU-3095 — a cold board must not assert an empty PR set.

``_dashboard_context_async`` seeds a cold cache key with an empty context and
returns it immediately so a slow local scan cannot make the dashboard look dead.
Rendered as an ordinary board that skeleton claimed "No worktrees" in every
column with a zero count — so ``POST /api/refresh``, which clears the cache,
made the dashboard assert zero PRs for as long as the rebuild took. Observed
2026-08-22: the board collapsed from 138KB of cards to a 2KB all-empty skeleton
and stayed there across repeated polls while two PRs were open.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from agentic_pr_dash import app


def _render_board(context: dict) -> str:
    template = app.templates.env.get_template("partials/board.html")
    return template.render(**context)


def test_cold_skeleton_is_marked_not_loaded() -> None:
    async def scenario() -> None:
        app._dashboard_context_cache.clear()
        app._dashboard_context_tasks.clear()
        context = await app._dashboard_context_async()
        assert context["board_loaded"] is False

    asyncio.run(scenario())


def test_cold_board_renders_loading_not_an_empty_pr_set() -> None:
    context = app._dashboard_context_from_cards(
        [], 0, 0, show_agent_worktrees=False, active_tab="board", loaded=False
    )

    html = _render_board(context)

    assert "Loading" in html
    assert "No worktrees" not in html, (
        "a board that has observed nothing must not claim there are no PRs"
    )


def test_observed_empty_board_still_says_no_worktrees() -> None:
    """An observed-empty board is a real answer and must keep saying so."""
    context = app._dashboard_context_from_cards(
        [], 0, 0, show_agent_worktrees=False, active_tab="board"
    )

    html = _render_board(context)

    assert context["board_loaded"] is True
    assert "No worktrees" in html
    assert "column-loading" not in html


def test_force_refresh_never_serves_a_confident_empty_board(monkeypatch) -> None:
    """The reported reproduction: refresh, then poll during the rebuild."""

    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_to_thread(func, **kwargs):
            started.set()
            await release.wait()
            return app._dashboard_context_from_cards(
                [], 0, 0, show_agent_worktrees=False, active_tab="board"
            )

        monkeypatch.setattr(app.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(app.orchestrator, "refresh_prs", AsyncMock())
        app._dashboard_context_cache.clear()
        app._dashboard_context_tasks.clear()

        await app.force_refresh()

        during_rebuild = await app._dashboard_context_async()
        await started.wait()
        assert during_rebuild["board_loaded"] is False
        assert "No worktrees" not in _render_board(during_rebuild)

        release.set()
        await app._dashboard_context_tasks[(False, "board")]
        after = await app._dashboard_context_async()
        assert after["board_loaded"] is True

    asyncio.run(scenario())


def test_failed_rebuild_is_logged_rather_than_swallowed(monkeypatch) -> None:
    """Nothing awaits the rebuild task once a cached context exists."""

    async def scenario() -> None:
        logged: list[tuple[str, str]] = []

        async def fake_to_thread(func, **kwargs):
            raise RuntimeError("worktree scan exploded")

        monkeypatch.setattr(app.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(
            app.orchestrator,
            "log",
            lambda message, level="info", **kwargs: logged.append((message, level)),
        )
        app._dashboard_context_cache.clear()
        app._dashboard_context_tasks.clear()

        context = await app._dashboard_context_async()
        assert context["board_loaded"] is False
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert any(
            "rebuild failed" in message and level == "error"
            for message, level in logged
        ), f"the rebuild failure was swallowed: {logged}"

    asyncio.run(scenario())
