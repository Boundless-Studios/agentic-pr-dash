"""BOU-2895 regression coverage for quota-aware PR observation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from agentic_pr_dash import github_api, orchestrator
from agentic_pr_dash.models import (
    CICheck,
    PRStatus,
    ReviewComment,
    RunnerExecutionSummary,
)
from agentic_pr_dash.observation import ObservationController


class ManualClock:
    """A timezone-aware clock advanced explicitly by the test."""

    def __init__(self) -> None:
        self.current = datetime(2026, 8, 8, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


@pytest.mark.asyncio
async def test_unchanged_terminal_pr_reuses_batch_observation(monkeypatch):
    anchor_root = "/repos/anchor"
    raw_pr = {
        "number": 2895,
        "title": "Quota-aware observation",
        "headRefName": "feature/quota-aware-observation",
        "headRefOid": "stable-head-sha",
        "baseRefName": "main",
        "url": "https://github.com/org/anchor-repo/pull/2895",
        "isDraft": False,
        "reviewDecision": "",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "labels": [],
        "createdAt": "2026-06-11T12:00:00Z",
    }
    observation_calls = {
        "batch": 0,
        "latest_commit": 0,
        "ci_checks": 0,
        "review_thread_scan": 0,
    }

    monkeypatch.setattr(
        orchestrator, "_resolve_maintenance_roots", lambda cwd: [anchor_root]
    )
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [raw_pr])
    monkeypatch.setattr(
        github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None
    )
    monkeypatch.setattr(
        github_api, "get_repo_info", lambda cwd=None: ("org", "anchor-repo")
    )
    monkeypatch.setattr(
        github_api, "get_mergeability", lambda num, cwd=None: ("CLEAN", "MERGEABLE")
    )

    def fake_latest_commit(num, cwd=None):
        observation_calls["latest_commit"] += 1
        return "stable-head-sha", "2026-06-11T12:00:00Z"

    def fake_ci_checks(num, cwd=None):
        observation_calls["ci_checks"] += 1
        return []

    def fake_scan_review_threads(num, latest_commit_date, cwd=None):
        observation_calls["review_thread_scan"] += 1
        return [], []

    monkeypatch.setattr(github_api, "get_latest_commit", fake_latest_commit)
    monkeypatch.setattr(github_api, "get_ci_checks", fake_ci_checks)
    monkeypatch.setattr(github_api, "scan_review_threads", fake_scan_review_threads)
    monkeypatch.setattr(
        orchestrator, "find_worktree_for_branch", lambda branch, root=None: None
    )

    def fake_batch(owner, repo, numbers, cwd=None):
        observation_calls["batch"] += 1
        return {
            number: {
                "latest_commit": ("stable-head-sha", "2026-06-11T12:00:00Z"),
                "ci_checks": [],
                "threads": [],
                "required_pending": False,
                "head_sha": "stable-head-sha",
                "merge_state": "CLEAN",
                "mergeable": "MERGEABLE",
                "review_decision": "none",
            }
            for number in numbers
        }

    monkeypatch.setattr(github_api, "batch_fetch_pr_review_and_ci", fake_batch)

    orch = orchestrator.Orchestrator(repo_cwd=anchor_root)
    await orch.refresh_prs()
    await orch.refresh_prs()

    assert observation_calls == {
        "batch": 1,
        "latest_commit": 1,
        "ci_checks": 1,
        "review_thread_scan": 1,
    }


@pytest.mark.asyncio
async def test_pending_ci_poll_reuses_review_until_ci_is_terminal(monkeypatch):
    anchor_root = "/repos/anchor"
    raw_pr = {
        "number": 2895,
        "title": "Quota-aware observation",
        "headRefName": "feature/quota-aware-observation",
        "headRefOid": "pending-head-sha",
        "baseRefName": "main",
        "url": "https://github.com/org/anchor-repo/pull/2895",
        "isDraft": False,
        "reviewDecision": "",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "labels": [],
        "createdAt": "2026-06-11T12:00:00Z",
    }
    observation_calls = {
        "batch": 0,
        "latest_commit": 0,
        "ci_checks": 0,
        "review_thread_scan": 0,
    }
    ci_results = iter(
        [
            [CICheck(name="checks", status="queued")],
            [CICheck(name="checks", status="completed", conclusion="success")],
        ]
    )

    monkeypatch.setattr(
        orchestrator, "_resolve_maintenance_roots", lambda cwd: [anchor_root]
    )
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [raw_pr])
    monkeypatch.setattr(
        github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None
    )
    monkeypatch.setattr(
        github_api, "get_repo_info", lambda cwd=None: ("org", "anchor-repo")
    )
    monkeypatch.setattr(
        github_api, "get_mergeability", lambda num, cwd=None: ("CLEAN", "MERGEABLE")
    )
    monkeypatch.setattr(
        github_api,
        "get_workflow_queue_health",
        lambda num, cwd=None: ([], [], RunnerExecutionSummary()),
    )
    monkeypatch.setattr(
        orchestrator, "find_worktree_for_branch", lambda branch, root=None: None
    )

    def fake_latest_commit(num, cwd=None):
        observation_calls["latest_commit"] += 1
        return "pending-head-sha", "2026-06-11T12:00:00Z"

    def fake_ci_checks(num, cwd=None):
        observation_calls["ci_checks"] += 1
        return next(ci_results)

    def fake_scan_review_threads(num, latest_commit_date, cwd=None):
        observation_calls["review_thread_scan"] += 1
        return [], []

    monkeypatch.setattr(github_api, "get_latest_commit", fake_latest_commit)
    monkeypatch.setattr(github_api, "get_ci_checks", fake_ci_checks)
    monkeypatch.setattr(github_api, "scan_review_threads", fake_scan_review_threads)

    def fake_batch(owner, repo, numbers, cwd=None):
        observation_calls["batch"] += 1
        return {
            number: {
                "latest_commit": (
                    "pending-head-sha",
                    "2026-06-11T12:00:00Z",
                ),
                "ci_checks": [],
                "threads": [],
                "required_pending": False,
                "head_sha": "pending-head-sha",
                "merge_state": "CLEAN",
                "mergeable": "MERGEABLE",
                "review_decision": "none",
            }
            for number in numbers
        }

    monkeypatch.setattr(
        github_api, "batch_fetch_pr_review_and_ci", fake_batch
    )

    clock = ManualClock()
    orch = orchestrator.Orchestrator(repo_cwd=anchor_root)
    orch.observation_controller = ObservationController(clock=clock)

    await orch.refresh_prs()
    await orch.refresh_prs()

    clock.advance(timedelta(seconds=30))
    await orch.refresh_prs()

    clock.advance(timedelta(seconds=30))
    await orch.refresh_prs()

    assert observation_calls == {
        "batch": 1,
        "latest_commit": 1,
        "ci_checks": 2,
        "review_thread_scan": 1,
    }


@pytest.mark.asyncio
async def test_unobservable_refresh_is_never_clean_and_preserves_known_blockers(
    monkeypatch,
):
    anchor_root = "/repos/anchor"
    raw_pr = {
        "number": 2895,
        "title": "Quota-aware observation",
        "headRefName": "feature/quota-aware-observation",
        "headRefOid": "failing-head-sha",
        "baseRefName": "main",
        "url": "https://github.com/org/anchor-repo/pull/2895",
        "isDraft": False,
        "reviewDecision": "",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "labels": [],
        "createdAt": "2026-06-11T12:00:00Z",
    }
    clock = ManualClock()
    phase = {"unobservable": True}
    calls = {"batch": 0, "latest": 0, "ci": 0, "review": 0}

    monkeypatch.setattr(
        orchestrator, "_resolve_maintenance_roots", lambda cwd: [anchor_root]
    )
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [raw_pr])
    monkeypatch.setattr(
        github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None
    )
    monkeypatch.setattr(
        github_api, "get_repo_info", lambda cwd=None: ("org", "anchor-repo")
    )
    monkeypatch.setattr(
        github_api, "get_mergeability", lambda num, cwd=None: ("CLEAN", "MERGEABLE")
    )
    monkeypatch.setattr(
        orchestrator, "find_worktree_for_branch", lambda branch, root=None: None
    )

    def fake_latest_commit(num, cwd=None):
        calls["latest"] += 1
        if phase["unobservable"]:
            return "", ""
        return "failing-head-sha", "2026-06-11T12:00:00Z"

    def fake_ci_checks(num, cwd=None):
        calls["ci"] += 1
        if phase["unobservable"]:
            return github_api.ObservationReadResult.unavailable(
                "CI quota exhausted"
            )
        return [CICheck(name="checks", status="completed", conclusion="failure")]

    def fake_scan_review_threads(num, latest_commit_date, cwd=None):
        calls["review"] += 1
        if phase["unobservable"]:
            return github_api.ObservationReadResult.unavailable(
                "review quota exhausted"
            )
        return [
            ReviewComment(
                id=101,
                author="reviewer",
                body="Please fix this",
                created_at="2026-06-11T12:00:00Z",
            )
        ], []

    def fake_batch(owner, repo, numbers, cwd=None):
        calls["batch"] += 1
        return {}

    monkeypatch.setattr(github_api, "get_latest_commit", fake_latest_commit)
    monkeypatch.setattr(github_api, "get_ci_checks", fake_ci_checks)
    monkeypatch.setattr(github_api, "scan_review_threads", fake_scan_review_threads)
    monkeypatch.setattr(github_api, "batch_fetch_pr_review_and_ci", fake_batch)

    orch = orchestrator.Orchestrator(repo_cwd=anchor_root)
    orch.observation_controller = ObservationController(clock=clock)

    await orch.refresh_prs()
    pr = orch.get_pr(2895)
    assert pr is not None
    assert pr.status is PRStatus.OBSERVATION_UNAVAILABLE

    phase["unobservable"] = False
    await orch.refresh_prs()
    pr = orch.get_pr(2895)
    assert pr is not None
    assert pr.status is PRStatus.CI_AND_COMMENTS

    phase["unobservable"] = True
    clock.advance(timedelta(hours=1))
    await orch.refresh_prs()

    pr = orch.get_pr(2895)
    assert pr is not None
    assert pr.status is PRStatus.CI_AND_COMMENTS
    assert pr.failing_checks == ["checks"]
    assert [comment.id for comment in pr.review_comments] == [101]

    await orch.refresh_prs()
    assert calls == {"batch": 4, "latest": 4, "ci": 4, "review": 4}


@pytest.mark.asyncio
async def test_overlapping_refreshes_are_serialized(monkeypatch):
    anchor_root = "/repos/anchor"
    raw_pr = {
        "number": 2895,
        "title": "Quota-aware observation",
        "headRefName": "feature/quota-aware-observation",
        "headRefOid": "stable-head-sha",
        "baseRefName": "main",
        "url": "https://github.com/org/anchor-repo/pull/2895",
        "isDraft": False,
        "reviewDecision": "",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "labels": [],
        "createdAt": "2026-06-11T12:00:00Z",
    }
    list_entered = asyncio.Event()
    release_list = asyncio.Event()
    calls = {"list": 0, "batch": 0, "latest": 0, "ci": 0, "review": 0}

    monkeypatch.setattr(
        orchestrator, "_resolve_maintenance_roots", lambda cwd: [anchor_root]
    )
    monkeypatch.setattr(
        github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None
    )
    monkeypatch.setattr(
        github_api, "get_repo_info", lambda cwd=None: ("org", "anchor-repo")
    )
    monkeypatch.setattr(
        github_api, "get_mergeability", lambda num, cwd=None: ("CLEAN", "MERGEABLE")
    )
    monkeypatch.setattr(
        github_api, "get_latest_commit", lambda num, cwd=None: (
            calls.__setitem__("latest", calls["latest"] + 1)
            or ("stable-head-sha", "2026-06-11T12:00:00Z")
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_ci_checks",
        lambda num, cwd=None: (
            calls.__setitem__("ci", calls["ci"] + 1) or []
        ),
    )
    monkeypatch.setattr(
        github_api,
        "scan_review_threads",
        lambda num, latest_commit_date, cwd=None: (
            calls.__setitem__("review", calls["review"] + 1) or ([], [])
        ),
    )
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda branch, root=None: None)

    def fake_batch(owner, repo, numbers, cwd=None):
        calls["batch"] += 1
        return {
            number: {
                "latest_commit": ("stable-head-sha", "2026-06-11T12:00:00Z"),
                "ci_checks": [],
                "threads": [],
                "required_pending": False,
                "head_sha": "stable-head-sha",
                "merge_state": "CLEAN",
                "mergeable": "MERGEABLE",
                "review_decision": "none",
            }
            for number in numbers
        }

    monkeypatch.setattr(github_api, "batch_fetch_pr_review_and_ci", fake_batch)

    async def fake_to_thread(fn, *args, **kwargs):
        if fn is github_api.list_open_prs:
            calls["list"] += 1
            if calls["list"] == 1:
                list_entered.set()
                await release_list.wait()
            return [raw_pr]
        return fn(*args, **kwargs)

    monkeypatch.setattr(orchestrator.asyncio, "to_thread", fake_to_thread)

    orch = orchestrator.Orchestrator(repo_cwd=anchor_root)
    first = asyncio.create_task(orch.refresh_prs())
    await list_entered.wait()
    second = asyncio.create_task(orch.refresh_prs())

    turn = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(turn.set_result, None)
    await turn
    assert calls["list"] == 1

    release_list.set()
    await asyncio.gather(first, second)

    # The second waiter joins the completed transaction and reuses its
    # successful metadata cache; only the first immediate tick lists PRs.
    assert calls == {"list": 1, "batch": 1, "latest": 1, "ci": 1, "review": 1}
