import asyncio
import threading
from unittest.mock import AsyncMock

from agentic_pr_dash import app


def test_cold_context_rebuilds_during_first_ttl_of_host_uptime(monkeypatch):
    async def scenario():
        monkeypatch.setattr(app.time, "monotonic", lambda: 5.0)
        monkeypatch.setattr(
            app.asyncio,
            "to_thread",
            AsyncMock(return_value={"version": "fresh"}),
        )
        app._dashboard_context_cache.clear()
        app._dashboard_context_tasks.clear()

        context = await app._dashboard_context_async()

        assert "columns" in context
        task = app._dashboard_context_tasks[(False, "board")]
        await task
        assert await app._dashboard_context_async() == {"version": "fresh"}

    asyncio.run(scenario())


def test_invalidated_inflight_result_stays_stale_during_first_ttl(monkeypatch):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        builds = 0

        async def fake_to_thread(func, **kwargs):
            nonlocal builds
            builds += 1
            started.set()
            await release.wait()
            return {"version": builds}

        monkeypatch.setattr(app.time, "monotonic", lambda: 5.0)
        monkeypatch.setattr(app.asyncio, "to_thread", fake_to_thread)
        app._dashboard_context_cache.clear()
        app._dashboard_context_tasks.clear()

        await app._dashboard_context_async()
        await started.wait()
        app._invalidate_dashboard_context()
        release.set()
        await app._dashboard_context_tasks[(False, "board")]

        assert await app._dashboard_context_async() == {"version": 1}
        await asyncio.sleep(0)
        assert builds == 2

    asyncio.run(scenario())


def test_invalidation_clears_cached_card_details():
    app._ownership_card_cache[("/worktree", 182, "/repo")] = (
        1.0,
        {"owner_session_id": "stale"},
    )

    app._invalidate_dashboard_context()

    assert app._ownership_card_cache == {}


def test_invalidated_card_build_cannot_repopulate_ownership_cache(monkeypatch):
    build_generation = app._dashboard_context_generation
    monkeypatch.setattr(
        app,
        "_ownership_for_card",
        lambda **_kwargs: {"owner_session_id": "stale"},
    )

    app._invalidate_dashboard_context()
    ownership = app._cached_ownership_for_card(
        worktree_path="/worktree",
        pr_number=182,
        repo_cwd="/repo",
        ownership_cache_generation=build_generation,
    )

    assert ownership == {"owner_session_id": "stale"}
    assert app._ownership_card_cache == {}


def test_card_cache_miss_does_not_block_invalidation(monkeypatch):
    load_started = threading.Event()
    release_load = threading.Event()
    invalidation_finished = threading.Event()

    def slow_load(**_kwargs):
        load_started.set()
        assert release_load.wait(timeout=2)
        return {"owner_session_id": "loaded"}

    monkeypatch.setattr(app, "_ownership_for_card", slow_load)
    worker = threading.Thread(
        target=app._cached_ownership_for_card,
        kwargs={
            "worktree_path": "/worktree",
            "pr_number": 182,
            "repo_cwd": "/repo",
        },
    )
    worker.start()
    assert load_started.wait(timeout=2)

    invalidator = threading.Thread(
        target=lambda: (app._invalidate_dashboard_context(), invalidation_finished.set())
    )
    invalidator.start()
    try:
        assert invalidation_finished.wait(timeout=0.5)
    finally:
        release_load.set()
        worker.join(timeout=2)
        invalidator.join(timeout=2)

    assert not worker.is_alive()
    assert not invalidator.is_alive()


def test_force_refresh_does_not_duplicate_inflight_context_build(monkeypatch):
    async def scenario():
        started = asyncio.Event()
        release_stale = asyncio.Event()
        builds = 0

        async def fake_to_thread(func, **kwargs):
            nonlocal builds
            builds += 1
            if builds == 1:
                started.set()
                await release_stale.wait()
                return {"version": "stale"}
            return {"version": "fresh"}

        monkeypatch.setattr(app.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(app.orchestrator, "refresh_prs", AsyncMock())
        app._dashboard_context_cache.clear()
        app._dashboard_context_tasks.clear()

        stale_request = asyncio.create_task(app._dashboard_context_async())
        await started.wait()
        stale_build = app._dashboard_context_tasks[(False, "board")]
        await app.force_refresh()
        app.orchestrator.refresh_prs.assert_awaited_once_with(force=True)

        fresh_request = asyncio.create_task(app._dashboard_context_async())
        await asyncio.sleep(0)
        # Invalidating a context cannot cancel the worker already running in a
        # thread. Keep that build authoritative until it finishes instead of
        # orphaning it and starting another expensive scan in parallel.
        assert builds == 1
        release_stale.set()

        # Invalidation leaves no completed snapshot, so the request receives
        # the cold-start skeleton instead of blocking on discovery.
        assert "columns" in await fresh_request
        assert "columns" in await stale_request
        assert await stale_build == {"version": "stale"}
        await app._dashboard_context_async()
        fresh_build = app._dashboard_context_tasks[(False, "board")]
        await fresh_build
        assert await app._dashboard_context_async() == {"version": "fresh"}
        assert builds == 2

    asyncio.run(scenario())


def test_stale_context_returns_immediately_while_one_rebuild_runs(monkeypatch):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        builds = 0

        async def fake_to_thread(func, **kwargs):
            nonlocal builds
            builds += 1
            started.set()
            await release.wait()
            return {"version": "fresh"}

        monkeypatch.setattr(app.asyncio, "to_thread", fake_to_thread)
        app._dashboard_context_cache.clear()
        app._dashboard_context_tasks.clear()
        key = (False, "board")
        app._dashboard_context_cache[key] = (0.0, {"version": "stale"})

        assert await app._dashboard_context_async() == {"version": "stale"}
        await started.wait()
        assert builds == 1
        assert await app._dashboard_context_async() == {"version": "stale"}
        assert builds == 1

        release.set()
        await app._dashboard_context_tasks[key]
        assert await app._dashboard_context_async() == {"version": "fresh"}

    asyncio.run(scenario())
