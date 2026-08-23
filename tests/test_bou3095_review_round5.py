"""BOU-3095 PR #169 review round 5 (codex).

``_bootstrap_metadata_validator`` captures the REST validator in a SECOND
request, issued after the rich snapshot has already been cached, and throws away
that response's body. If the open set changes in the gap between those two
requests, the stored ETag describes the newer state while ``_metadata_cache``
still holds the older one — so every later 304 says "unchanged" about a cache
that was already wrong, and marks it freshly observed while doing so.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_pr_dash import github_api, orchestrator as orchestrator_module
from agentic_pr_dash.models import RunnerExecutionSummary
from agentic_pr_dash.observation import ObservationController
from agentic_pr_dash.quota import QuotaLedger


class ManualClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 22, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _raw_pr(number: int = 7) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "headRefName": f"feature/{number}",
        "headRefOid": "head-1",
        "baseRefName": "main",
        "url": f"https://github.com/org/widgets/pull/{number}",
        "isDraft": False,
        "reviewDecision": "",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "labels": [],
        "createdAt": "2026-08-22T00:00:00Z",
        "updatedAt": "2026-08-22T00:00:00Z",
    }


def _probe_pr(number: int) -> dict:
    raw = _raw_pr(number)
    raw["author"] = {"login": "alice"}
    return raw


@pytest.fixture
def boundaries(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        orchestrator_module, "_resolve_maintenance_roots", lambda cwd: ["/repos/widgets"]
    )
    monkeypatch.setattr(
        github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None
    )
    monkeypatch.setattr(
        github_api, "get_repo_info", lambda cwd=None: ("org", "widgets")
    )
    monkeypatch.setattr(
        github_api,
        "batch_fetch_pr_review_and_ci",
        lambda owner, repo, numbers, cwd=None: {},
    )
    monkeypatch.setattr(
        github_api, "get_mergeability", lambda number, cwd=None: ("CLEAN", "MERGEABLE")
    )
    monkeypatch.setattr(
        github_api,
        "get_latest_commit",
        lambda number, cwd=None: ("head-1", "2026-08-22T00:00:00Z"),
    )
    monkeypatch.setattr(github_api, "get_ci_checks", lambda number, cwd=None: [])
    monkeypatch.setattr(
        github_api,
        "get_workflow_queue_health",
        lambda number, cwd=None: ([], [], RunnerExecutionSummary()),
    )
    monkeypatch.setattr(
        github_api, "scan_review_threads", lambda number, latest, cwd=None: ([], [])
    )
    monkeypatch.setattr(
        orchestrator_module, "find_worktree_for_branch", lambda branch, root=None: None
    )


def _orchestrator(clock: ManualClock, *, budget: int = 500):
    return orchestrator_module.Orchestrator(
        repo_cwd="/repos/widgets",
        observation_controller=ObservationController(clock=clock),
        quota_ledger=QuotaLedger(clock=clock, background_hourly_budget=budget),
    )


def _tracked_numbers(orch) -> set[int]:
    return {number for _repo, number in orch.prs}


@pytest.mark.asyncio
async def test_bootstrap_probe_mismatch_forces_a_relist(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """PR 8 merges between the rich snapshot and the validator bootstrap.

    The stored ETag then describes a world without PR 8 while the cache still
    contains it, so every subsequent 304 confirms — and re-observes — a cache
    that was stale from the moment it was written.
    """
    clock = ManualClock()
    listed = {"value": [_raw_pr(7), _raw_pr(8)]}
    probe_body = {"value": [_probe_pr(7)]}  # PR 8 already gone by the bootstrap
    calls = {"list": 0}

    def list_open_prs(cwd=None):
        calls["list"] += 1
        return list(listed["value"])

    def probe(owner, repo, *, etag=None, last_modified=None, author=None, cwd=None):
        if etag is None:
            return github_api.ConditionalPRListProbe(
                200, list(probe_body["value"]), etag='"v2"', truncated=False
            )
        return github_api.ConditionalPRListProbe(304, [], etag='"v2"')

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)
    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)

    orch = _orchestrator(clock)
    await orch.refresh_prs(force=True)

    # The rich snapshot legitimately still had PR 8.
    assert _tracked_numbers(orch) == {7, 8}
    listed["value"] = [_raw_pr(7)]

    # Next tick: the mismatch must have scheduled a reconciling relist rather
    # than leaving 304s to confirm the stale cache until the hourly refresh.
    clock.advance(timedelta(seconds=orchestrator_module.POLL_INTERVAL_SECONDS))
    await orch.refresh_prs()

    assert _tracked_numbers(orch) == {7}, (
        "the bootstrap validator described a newer open set than the cache it "
        "was stored against, and later 304s confirmed the stale cache"
    )


@pytest.mark.asyncio
async def test_matching_bootstrap_probe_does_not_force_a_relist(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """The normal case must not spend an extra GraphQL relist every startup."""
    clock = ManualClock()
    calls = {"list": 0}

    def list_open_prs(cwd=None):
        calls["list"] += 1
        return [_raw_pr(7)]

    def probe(owner, repo, *, etag=None, last_modified=None, author=None, cwd=None):
        if etag is None:
            return github_api.ConditionalPRListProbe(
                200, [_probe_pr(7)], etag='"v1"', truncated=False
            )
        return github_api.ConditionalPRListProbe(304, [], etag='"v1"')

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)
    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)

    orch = _orchestrator(clock)
    await orch.refresh_prs(force=True)
    seeded = calls["list"]

    clock.advance(timedelta(seconds=orchestrator_module.POLL_INTERVAL_SECONDS))
    await orch.refresh_prs()

    assert calls["list"] == seeded, (
        "an agreeing bootstrap probe triggered a needless rich relist on every "
        "startup"
    )


@pytest.mark.asyncio
async def test_truncated_bootstrap_probe_is_not_treated_as_a_mismatch(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """A truncated bootstrap body cannot be compared, so it proves nothing.

    It must not be read as "the sets differ" and force a relist every startup;
    the truncation guard already stops its 304s counting as observations.
    """
    clock = ManualClock()
    calls = {"list": 0}

    def list_open_prs(cwd=None):
        calls["list"] += 1
        return [_raw_pr(7), _raw_pr(8)]

    def probe(owner, repo, *, etag=None, last_modified=None, author=None, cwd=None):
        if etag is None:
            return github_api.ConditionalPRListProbe(
                200, [_probe_pr(7)], etag='"v1"', truncated=True
            )
        return github_api.ConditionalPRListProbe(304, [], etag='"v1"')

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)
    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)

    orch = _orchestrator(clock)
    await orch.refresh_prs(force=True)
    seeded = calls["list"]

    clock.advance(timedelta(seconds=orchestrator_module.POLL_INTERVAL_SECONDS))
    await orch.refresh_prs()

    assert calls["list"] == seeded
    # And it still must not claim the open set was observed.
    assert orch.open_set_freshness().age_seconds(clock.current) != 0.0
