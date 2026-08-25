import asyncio
from unittest.mock import AsyncMock

import pytest

from agentic_pr_dash import orchestrator as orchestrator_module


@pytest.mark.asyncio
async def test_poll_loop_waits_after_a_slow_refresh(monkeypatch):
    """A slow/rate-limited refresh must not turn the daemon into a hot loop."""

    orch = object.__new__(orchestrator_module.Orchestrator)
    orch.refresh_prs = AsyncMock()
    delays: list[float] = []

    async def stop_after_first_delay(delay: float) -> None:
        delays.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", stop_after_first_delay)

    with pytest.raises(asyncio.CancelledError):
        await orch._poll_loop()

    assert delays == [orchestrator_module.POLL_INTERVAL_SECONDS]
    orch.refresh_prs.assert_awaited_once_with()
