"""BOU-2895 PR #141 review follow-ups.

Three defects the observation rewrite introduced, each pinned here:

* queue diagnostics were gated on the full REVIEW+CI plan, so the 30-second
  CI-only polls that a pending PR actually gets never refreshed them;
* a PR whose new head was never successfully observed kept reporting (and
  auto-dispatching against) the previous head's cached blockers;
* a failed *first* metadata list blanked the board for a full 15-minute
  reconciliation interval instead of retrying on the next poll tick.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agentic_pr_dash import github_api, orchestrator
from agentic_pr_dash.models import (
    CICheck,
    PRStatus,
    QueuedWorkflowJob,
    RunnerExecutionSummary,
    RunnerPoolHealth,
)
from agentic_pr_dash.observation import ObservationController, ObservationSlice
from agentic_pr_dash.quota import QuotaLedger


class ManualClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 8, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _raw_pr(number: int = 7, *, head: str = "head-1") -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "headRefName": "feature/widgets",
        "headRefOid": head,
        "baseRefName": "main",
        "url": f"https://github.com/org/widgets/pull/{number}",
        "isDraft": False,
        "reviewDecision": "",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "labels": [],
        "createdAt": "2026-08-08T00:00:00Z",
    }


@pytest.fixture
def observation_boundaries(monkeypatch):
    monkeypatch.setattr(
        orchestrator, "_resolve_maintenance_roots", lambda cwd: ["/repos/widgets"]
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
        lambda number, cwd=None: ("head-1", "2026-08-08T00:00:00Z"),
    )
    monkeypatch.setattr(github_api, "get_ci_checks", lambda number, cwd=None: [])
    monkeypatch.setattr(
        github_api,
        "get_workflow_queue_health",
        lambda number, cwd=None: ([], [], RunnerExecutionSummary()),
    )
    monkeypatch.setattr(
        github_api,
        "scan_review_threads",
        lambda number, latest, cwd=None: ([], []),
    )
    monkeypatch.setattr(
        orchestrator, "find_worktree_for_branch", lambda branch, root=None: None
    )


@pytest.mark.asyncio
async def test_pending_ci_only_poll_refreshes_queue_diagnostics(
    monkeypatch, observation_boundaries
):
    """A job starting between full observations must move the runner panel.

    After the first full read a still-pending PR is polled every 30 seconds
    with a CI-only plan. Gating queue health on REVIEW+CI froze queued_jobs and
    runner-pool health at their initial values until the hourly review
    reconciliation or a terminal CI result.
    """
    clock = ManualClock()
    calls = {"queue": 0}
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr()])
    monkeypatch.setattr(
        github_api,
        "get_ci_checks",
        lambda number, cwd=None: [CICheck(name="build", status="queued")],
    )

    def queue_health(number, cwd=None):
        calls["queue"] += 1
        return (
            [
                QueuedWorkflowJob(
                    name="build",
                    status="queued",
                    queue_seconds=30 * calls["queue"],
                )
            ],
            [RunnerPoolHealth(pool="desktop", total_count=calls["queue"])],
            RunnerExecutionSummary(desktop_count=calls["queue"]),
        )

    monkeypatch.setattr(github_api, "get_workflow_queue_health", queue_health)

    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)

    await orch.refresh_prs()
    pr = orch.get_pr(7)
    assert pr is not None
    assert calls["queue"] == 1
    assert pr.queued_jobs[0].queue_seconds == 30

    # Only the CI slice is due at 30s — the diagnostics must still move.
    clock.advance(timedelta(seconds=30))
    await orch.refresh_prs()

    assert calls["queue"] == 2
    assert pr.queued_jobs[0].queue_seconds == 60
    assert pr.runner_pool_health[0].total_count == 2
    assert pr.runner_execution_summary.desktop_count == 2


@pytest.mark.asyncio
async def test_terminal_ci_still_clears_queue_diagnostics(
    monkeypatch, observation_boundaries
):
    """The other half of the branch: nothing pending means nothing queued."""
    clock = ManualClock()
    statuses = iter(
        [
            [CICheck(name="build", status="queued")],
            [CICheck(name="build", status="completed", conclusion="success")],
        ]
    )
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr()])
    monkeypatch.setattr(
        github_api, "get_ci_checks", lambda number, cwd=None: next(statuses)
    )
    monkeypatch.setattr(
        github_api,
        "get_workflow_queue_health",
        lambda number, cwd=None: (
            [QueuedWorkflowJob(name="build", status="queued")],
            [RunnerPoolHealth(pool="desktop", total_count=1)],
            RunnerExecutionSummary(desktop_count=1),
        ),
    )

    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)

    await orch.refresh_prs()
    pr = orch.get_pr(7)
    assert pr is not None
    assert pr.queued_jobs

    clock.advance(timedelta(seconds=30))
    await orch.refresh_prs()

    assert pr.queued_jobs == []
    assert pr.runner_pool_health == []
    assert pr.runner_execution_summary.desktop_count == 0


@pytest.mark.asyncio
async def test_unobserved_new_head_is_unavailable_and_not_dispatched(
    monkeypatch, observation_boundaries
):
    """A new head we could not observe must not dispatch the old head's blockers.

    head-1 is fully observed as CI_FAILING. The push to head-2 then fails its
    first observation, so every blocker on the PR belongs to head-1 — and the
    push may be precisely the fix. Reporting CI_FAILING here would auto-dispatch
    maintenance against work that no longer exists.
    """
    clock = ManualClock()
    current_head = {"value": "head-1"}
    head_2_latest_available = {"value": False}
    dispatched: list[int] = []
    scanned_dates: list[str] = []

    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: [_raw_pr(head=current_head["value"])],
    )
    monkeypatch.setattr(
        github_api,
        "get_latest_commit",
        lambda number, cwd=None: (
            ("head-1", "2026-08-08T00:00:00Z")
            if current_head["value"] == "head-1"
            else (
                ("head-2", "2026-08-08T01:00:00Z")
                if head_2_latest_available["value"]
                else ("", "")
            )
        ),
    )
    monkeypatch.setattr(
        github_api,
        "scan_review_threads_observation",
        lambda number, latest, cwd=None: (
            scanned_dates.append(latest)
            or github_api.ObservationReadResult.observed(([], []))
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_ci_checks",
        lambda number, cwd=None: [
            CICheck(name="build", status="completed", conclusion="failure")
        ],
    )
    monkeypatch.setattr(
        orchestrator, "find_worktree_for_branch", lambda branch, root=None: "/wt/widgets"
    )
    monkeypatch.setattr(
        orchestrator.coordinator,
        "dispatch_decision_for_pr",
        lambda pr: SimpleNamespace(
            state="ready", should_dispatch=True, reason="test"
        ),
    )

    async def record_dispatch(self, pr):
        dispatched.append(pr.number)

    monkeypatch.setattr(
        orchestrator.Orchestrator, "dispatch_pr_maintenance", record_dispatch
    )

    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)

    await orch.refresh_prs()
    await asyncio.sleep(0)  # let the create_task dispatch run
    pr = orch.get_pr(7)
    assert pr is not None
    assert pr.status is PRStatus.CI_FAILING
    assert dispatched == [7]
    dispatched.clear()

    # The push lands, but its first review/CI observation fails.
    current_head["value"] = "head-2"
    await orch.handle_github_event(
        "pull_request", "org/widgets", 7, "head-2", action="synchronize"
    )
    clock.advance(timedelta(seconds=2))
    await orch.refresh_prs()
    await asyncio.sleep(0)

    assert pr.failing_checks == ["build"]  # cached, and stale
    assert pr.status is PRStatus.OBSERVATION_UNAVAILABLE
    assert dispatched == []

    # The partial retry must refetch the immutable head prerequisite rather
    # than scanning head-2 review state with head-1's cached commit date.
    head_2_latest_available["value"] = True
    scanned_dates.clear()
    await orch.refresh_prs()
    assert scanned_dates == ["2026-08-08T01:00:00Z"]


@pytest.mark.asyncio
async def test_cold_start_metadata_failure_retries_on_the_next_poll(
    monkeypatch, observation_boundaries
):
    """A transient at daemon startup must not blank the board for 15 minutes.

    With no cached projection there is nothing to preserve, so the long
    reconciliation backoff buys nothing — it only delays the first usable board.
    """
    clock = ManualClock()
    calls = {"list": 0}

    def list_open_prs(cwd=None):
        calls["list"] += 1
        return None if calls["list"] == 1 else [_raw_pr()]

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)

    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)

    await orch.refresh_prs()
    assert calls["list"] == 1
    assert orch.get_pr(7) is None

    clock.advance(timedelta(seconds=orchestrator.POLL_INTERVAL_SECONDS))
    await orch.refresh_prs()

    assert calls["list"] == 2
    assert orch.get_pr(7) is not None


@pytest.mark.asyncio
async def test_cold_start_retry_does_not_spin_within_one_tick(
    monkeypatch, observation_boundaries
):
    """The short retry is a poll-tick floor, not an unbounded relist loop."""
    clock = ManualClock()
    calls = {"list": 0}

    def list_open_prs(cwd=None):
        calls["list"] += 1
        return None

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)

    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)

    await orch.refresh_prs()
    clock.advance(timedelta(seconds=orchestrator.POLL_INTERVAL_SECONDS - 1))
    await orch.refresh_prs()

    assert calls["list"] == 1


@pytest.mark.asyncio
async def test_partial_observation_applies_and_acknowledges_ci_independently(
    monkeypatch, observation_boundaries
):
    """A denied review slice must not discard an authoritative CI result."""

    clock = ManualClock()
    review_available = {"value": False}
    ci_available = {"value": True}
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr()])
    monkeypatch.setattr(
        github_api,
        "batch_fetch_pr_review_and_ci",
        lambda owner, repo, numbers, cwd=None, *, quota_context=None: {},
    )
    monkeypatch.setattr(
        github_api,
        "scan_review_threads_observation",
        lambda number, latest, cwd=None: (
            github_api.ObservationReadResult.observed(([], []))
            if review_available["value"]
            else github_api.ObservationReadResult.unavailable(
                "review quota denied"
            )
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_ci_checks_rest_observation",
        lambda head, cwd=None, *, pr_number=None: (
            github_api.ObservationReadResult.observed(
                [
                    CICheck(
                        name="build",
                        status="completed",
                        conclusion="failure",
                    )
                ]
            )
            if ci_available["value"]
            else github_api.ObservationReadResult.unavailable(
                "CI observation unavailable"
            )
        ),
    )

    orch = orchestrator.Orchestrator(
        repo_cwd="/repos/widgets",
        quota_ledger=QuotaLedger(clock=clock, maintenance_reserve=0),
    )
    orch.observation_controller = ObservationController(clock=clock)

    await orch.refresh_prs()

    pr = orch.get_pr(7)
    assert pr is not None
    assert pr.failing_checks == ["build"]
    assert pr.status is PRStatus.OBSERVATION_UNAVAILABLE

    retry = orch.observation_controller.plan_for(
        "org/widgets", 7, "head-1", now=clock(), ci_pending=False
    )
    assert retry is not None
    assert retry.slices == frozenset({ObservationSlice.REVIEW})

    review_available["value"] = True
    clock.advance(timedelta(seconds=30))
    await orch.refresh_prs()
    assert pr.status is PRStatus.CI_FAILING

    # A later transient CI outage must preserve the now-authoritative current
    # head state instead of returning to OBSERVATION_UNAVAILABLE.
    ci_available["value"] = False
    await orch.handle_github_event("check_suite", "org/widgets", 7, "head-1")
    clock.advance(timedelta(seconds=2))
    await orch.refresh_prs()
    assert pr.status is PRStatus.CI_FAILING


@pytest.mark.asyncio
async def test_full_observation_rejects_a_head_newer_than_its_plan(
    monkeypatch, observation_boundaries
):
    """A metadata/observation race must not acknowledge the superseded key."""

    calls = {"review": 0, "ci": 0}
    monkeypatch.setattr(
        github_api, "list_open_prs", lambda cwd=None: [_raw_pr(head="head-1")]
    )
    monkeypatch.setattr(
        github_api,
        "get_latest_commit",
        lambda number, cwd=None: ("head-2", "2026-08-08T01:00:00Z"),
    )

    def review_observation(number, latest, cwd=None):
        calls["review"] += 1
        return github_api.ObservationReadResult.observed(([], []))

    def ci_observation(head, cwd=None, *, pr_number=None):
        calls["ci"] += 1
        return github_api.ObservationReadResult.observed([])

    monkeypatch.setattr(
        github_api, "scan_review_threads_observation", review_observation
    )
    monkeypatch.setattr(
        github_api, "get_ci_checks_rest_observation", ci_observation
    )

    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    await orch.refresh_prs()

    pr = orch.get_pr(7)
    assert pr is not None
    assert pr.status is PRStatus.OBSERVATION_UNAVAILABLE
    assert calls == {"review": 0, "ci": 0}
    retry = orch.observation_controller.plan_for(
        "org/widgets", 7, "head-1", ci_pending=False
    )
    assert retry is not None
    assert retry.slices == frozenset(
        {ObservationSlice.METADATA, ObservationSlice.REVIEW, ObservationSlice.CI}
    )


@pytest.mark.asyncio
async def test_legacy_head_review_only_retry_recovers_after_partial_observation(
    monkeypatch, observation_boundaries
):
    """A synthetic plan key must reuse the real SHA learned by its full read."""

    raw = _raw_pr()
    raw.pop("headRefOid")
    clock = ManualClock()
    review_available = {"value": False}
    review_calls = 0
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [raw])
    monkeypatch.setattr(
        github_api,
        "get_latest_commit",
        lambda number, cwd=None: ("real-head", "2026-08-08T01:00:00Z"),
    )

    def review_observation(number, latest, cwd=None):
        nonlocal review_calls
        review_calls += 1
        if review_available["value"]:
            return github_api.ObservationReadResult.observed(([], []))
        return github_api.ObservationReadResult.unavailable("review unavailable")

    monkeypatch.setattr(
        github_api, "scan_review_threads_observation", review_observation
    )

    orch = orchestrator.Orchestrator(
        repo_cwd="/repos/widgets",
        observation_controller=ObservationController(clock=clock),
        quota_ledger=QuotaLedger(clock=clock, maintenance_reserve=0),
    )
    await orch.refresh_prs()

    pr = orch.get_pr(7)
    assert pr is not None
    assert pr.latest_commit_sha == "real-head"
    assert pr.status is PRStatus.OBSERVATION_UNAVAILABLE
    retry = orch.observation_controller.plan_for(
        "org/widgets", 7, "legacy-head-7", ci_pending=False
    )
    assert retry is not None
    assert retry.slices == frozenset({ObservationSlice.REVIEW})

    review_available["value"] = True
    clock.advance(timedelta(seconds=30))
    await orch.refresh_prs()

    assert review_calls == 2
    assert pr.status is PRStatus.CLEAN


@pytest.mark.asyncio
async def test_slice_event_cannot_clear_unavailable_until_both_slices_observed(
    monkeypatch, observation_boundaries
):
    """Repeated REVIEW success cannot make a never-observed CI slice look clean."""

    clock = ManualClock()
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr()])
    monkeypatch.setattr(
        github_api,
        "scan_review_threads_observation",
        lambda number, latest, cwd=None: (
            github_api.ObservationReadResult.observed(([], []))
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_ci_checks_rest_observation",
        lambda head, cwd=None, *, pr_number=None: (
            github_api.ObservationReadResult.unavailable("CI unavailable")
        ),
    )

    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)
    await orch.refresh_prs()

    pr = orch.get_pr(7)
    assert pr is not None
    assert pr.status is PRStatus.OBSERVATION_UNAVAILABLE

    await orch.handle_github_event(
        "pull_request_review", "org/widgets", 7, "head-1", action="submitted"
    )
    clock.advance(timedelta(seconds=2))
    await orch.refresh_prs()

    assert pr.status is PRStatus.OBSERVATION_UNAVAILABLE
    assert orch.observation_controller.plan_for(
        "org/widgets", 7, "head-1", now=clock(), ci_pending=False
    ) is not None


@pytest.mark.asyncio
async def test_warm_key_latest_head_race_forces_unavailable_without_clean_release(
    monkeypatch, observation_boundaries
):
    """Old-key authority cannot make a superseded plan actionable."""

    clock = ManualClock()
    latest_head = {"value": "head-1"}
    raw = _raw_pr()
    dispatched: list[int] = []
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [raw])
    monkeypatch.setattr(
        github_api,
        "get_latest_commit",
        lambda number, cwd=None: (
            latest_head["value"],
            "2026-08-08T01:00:00Z",
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "find_worktree_for_branch",
        lambda branch, root=None: "/wt/widgets",
    )
    monkeypatch.setattr(
        orchestrator.coordinator,
        "dispatch_decision_for_pr",
        lambda pr: SimpleNamespace(state="ready", should_dispatch=True, reason="test"),
    )

    async def record_dispatch(self, pr):
        dispatched.append(pr.number)

    monkeypatch.setattr(
        orchestrator.Orchestrator, "dispatch_pr_maintenance", record_dispatch
    )

    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)
    await orch.refresh_prs()

    pr = orch.get_pr(7)
    assert pr is not None
    assert pr.status is PRStatus.CLEAN
    pr.activity_message = "preserve until the current head is observed"

    raw["mergeStateStatus"] = "DIRTY"
    raw["mergeable"] = "CONFLICTING"
    latest_head["value"] = "head-2"
    await orch.handle_github_event(
        "pull_request", "org/widgets", 7, "head-1", action="synchronize"
    )
    clock.advance(timedelta(seconds=2))
    await orch.refresh_prs()
    await asyncio.sleep(0)

    assert pr.status is PRStatus.OBSERVATION_UNAVAILABLE
    assert pr.activity_message == "preserve until the current head is observed"
    assert dispatched == []
