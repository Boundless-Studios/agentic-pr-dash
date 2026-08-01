import asyncio
from unittest.mock import AsyncMock

from agentic_pr_dash import app


def test_force_refresh_discards_inflight_context_result(monkeypatch):
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

        fresh_request = asyncio.create_task(app._dashboard_context_async())
        await asyncio.sleep(0)
        release_stale.set()

        # Invalidation leaves no completed snapshot, so the request receives
        # the cold-start skeleton instead of blocking on discovery.
        assert "columns" in await fresh_request
        assert "columns" in await stale_request
        assert await stale_build == {"version": "stale"}
        fresh_build = app._dashboard_context_tasks.get((False, "board"))
        if fresh_build:
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
