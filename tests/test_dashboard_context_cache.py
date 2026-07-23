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
        await app.force_refresh()

        fresh_request = asyncio.create_task(app._dashboard_context_async())
        await asyncio.sleep(0)
        release_stale.set()

        assert await fresh_request == {"version": "fresh"}
        assert await stale_request == {"version": "stale"}
        assert await app._dashboard_context_async() == {"version": "fresh"}
        assert builds == 2

    asyncio.run(scenario())
