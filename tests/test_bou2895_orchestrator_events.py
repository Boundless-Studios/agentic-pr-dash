"""BOU-2895 Orchestrator metadata-cache and GitHub-event coverage."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agentic_pr_dash import github_api, orchestrator
from agentic_pr_dash.models import CICheck, ReviewComment, RunnerExecutionSummary
from agentic_pr_dash.observation import ObservationController


class ManualClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 8, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _raw_pr(number: int = 7, *, head: str = "head-1", repo: str = "org/widgets") -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "headRefName": "feature/widgets",
        "headRefOid": head,
        "baseRefName": "main",
        "url": f"https://github.com/{repo}/pull/{number}",
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
async def test_refresh_reuses_metadata_until_reconciliation_and_prunes_after_success(
    monkeypatch, observation_boundaries
):
    clock = ManualClock()
    raw = [_raw_pr()]
    calls = {"list": 0, "latest": 0, "ci": 0, "review": 0}

    def list_open_prs(cwd=None):
        calls["list"] += 1
        return list(raw)

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)
    monkeypatch.setattr(
        github_api,
        "get_latest_commit",
        lambda number, cwd=None: (
            calls.__setitem__("latest", calls["latest"] + 1)
            or ("head-1", "2026-08-08T00:00:00Z")
        ),
    )
    ci_results = iter(
        [
            [CICheck(name="checks", status="queued")],
            [CICheck(name="checks", status="queued")],
        ]
    )
    monkeypatch.setattr(
        github_api,
        "get_ci_checks",
        lambda number, cwd=None: (
            calls.__setitem__("ci", calls["ci"] + 1) or next(ci_results)
        ),
    )
    monkeypatch.setattr(
        github_api,
        "scan_review_threads",
        lambda number, latest, cwd=None: (
            calls.__setitem__("review", calls["review"] + 1) or ([], [])
        ),
    )

    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)

    await orch.refresh_prs()
    await orch.refresh_prs()
    assert calls == {"list": 1, "latest": 1, "ci": 1, "review": 1}

    clock.advance(timedelta(seconds=30))
    await orch.refresh_prs()
    assert calls == {"list": 1, "latest": 1, "ci": 2, "review": 1}

    clock.advance(timedelta(minutes=15))
    raw.clear()
    await orch.refresh_prs()

    assert calls["list"] == 2
    assert orch.get_pr(7) is None


@pytest.mark.asyncio
async def test_failed_metadata_relist_keeps_cached_prs(monkeypatch, observation_boundaries):
    clock = ManualClock()
    raw = [_raw_pr()]
    list_results = [raw, None]
    calls = {"list": 0}

    def list_open_prs(cwd=None):
        calls["list"] += 1
        return list_results.pop(0)

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)
    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)

    await orch.refresh_prs()
    clock.advance(timedelta(minutes=15))
    await orch.refresh_prs()

    assert calls["list"] == 2
    assert orch.get_pr(7) is not None


@pytest.mark.asyncio
async def test_failed_metadata_relist_backoff_uses_latest_attempt(
    monkeypatch, observation_boundaries
):
    clock = ManualClock()
    raw = [_raw_pr()]
    calls = {"list": 0}

    def list_open_prs(cwd=None):
        calls["list"] += 1
        return list(raw) if calls["list"] == 1 else None

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)
    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)

    await orch.refresh_prs()
    clock.advance(timedelta(minutes=15))
    await orch.refresh_prs()
    clock.advance(timedelta(seconds=15))
    await orch.refresh_prs()

    assert calls["list"] == 2
    assert orch.get_pr(7) is not None


@pytest.mark.asyncio
async def test_opened_event_uses_cached_empty_root_identity(
    monkeypatch, observation_boundaries
):
    clock = ManualClock()
    calls = {"list": 0, "repo_info": 0}
    raw = [_raw_pr()]

    def list_open_prs(cwd=None):
        calls["list"] += 1
        return [] if calls["list"] == 1 else list(raw)

    def get_repo_info(cwd=None):
        calls["repo_info"] += 1
        return "ORG", "WIDGETS"

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)
    monkeypatch.setattr(github_api, "get_repo_info", get_repo_info)
    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)

    await orch.refresh_prs()
    await orch.refresh_prs()
    assert calls == {"list": 1, "repo_info": 1}

    await orch.handle_github_event(
        "pull_request", "org/widgets", 7, "head-1", action="opened"
    )
    clock.advance(timedelta(seconds=2))
    await orch.refresh_prs()

    assert calls == {"list": 2, "repo_info": 1}
    assert orch.get_pr(7, repo="org/widgets") is not None


@pytest.mark.asyncio
async def test_review_event_refresh_reuses_latest_commit_and_skips_ci(
    monkeypatch, observation_boundaries
):
    clock = ManualClock()
    calls = {"list": 0, "latest": 0, "ci": 0, "review": 0}
    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: (
            calls.__setitem__("list", calls["list"] + 1) or [_raw_pr()]
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_latest_commit",
        lambda number, cwd=None: (
            calls.__setitem__("latest", calls["latest"] + 1)
            or ("head-1", "2026-08-08T00:00:00Z")
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_ci_checks",
        lambda number, cwd=None: (
            calls.__setitem__("ci", calls["ci"] + 1) or []
        ),
    )
    monkeypatch.setattr(
        github_api,
        "scan_review_threads",
        lambda number, latest, cwd=None: (
            calls.__setitem__("review", calls["review"] + 1) or ([], [])
        ),
    )

    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)
    await orch.refresh_prs()

    await orch.handle_github_event(
        "pull_request_review", "ORG/WIDGETS", 7, "head-1"
    )
    clock.advance(timedelta(seconds=2))
    await orch.refresh_prs()

    assert calls == {"list": 1, "latest": 1, "ci": 1, "review": 2}


@pytest.mark.asyncio
async def test_check_event_reads_ci_only(monkeypatch, observation_boundaries):
    clock = ManualClock()
    calls = {"list": 0, "latest": 0, "ci": 0, "review": 0}
    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: (
            calls.__setitem__("list", calls["list"] + 1) or [_raw_pr()]
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_latest_commit",
        lambda number, cwd=None: (
            calls.__setitem__("latest", calls["latest"] + 1)
            or ("head-1", "2026-08-08T00:00:00Z")
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_ci_checks",
        lambda number, cwd=None: (
            calls.__setitem__("ci", calls["ci"] + 1) or []
        ),
    )
    monkeypatch.setattr(
        github_api,
        "scan_review_threads",
        lambda number, latest, cwd=None: (
            calls.__setitem__("review", calls["review"] + 1) or ([], [])
        ),
    )

    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)
    await orch.refresh_prs()

    await orch.handle_github_event("check_suite", "org/widgets", 7, "head-1")
    clock.advance(timedelta(seconds=2))
    await orch.refresh_prs()

    assert calls == {"list": 1, "latest": 1, "ci": 2, "review": 1}


@pytest.mark.asyncio
async def test_synchronize_event_relists_new_head_and_reads_all_slices(
    monkeypatch, observation_boundaries
):
    clock = ManualClock()
    current_head = {"value": "head-1"}
    calls = {"list": 0, "latest": 0, "ci": 0, "review": 0}
    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: (
            calls.__setitem__("list", calls["list"] + 1)
            or [_raw_pr(head=current_head["value"])]
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_latest_commit",
        lambda number, cwd=None: (
            calls.__setitem__("latest", calls["latest"] + 1)
            or (current_head["value"], "2026-08-08T00:00:00Z")
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_ci_checks",
        lambda number, cwd=None: (
            calls.__setitem__("ci", calls["ci"] + 1) or []
        ),
    )
    monkeypatch.setattr(
        github_api,
        "scan_review_threads",
        lambda number, latest, cwd=None: (
            calls.__setitem__("review", calls["review"] + 1) or ([], [])
        ),
    )

    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)
    await orch.refresh_prs()

    current_head["value"] = "head-2"
    await orch.handle_github_event(
        "pull_request", "ORG/WIDGETS", 7, "head-2", action="synchronize"
    )
    clock.advance(timedelta(seconds=2))
    await orch.refresh_prs()

    assert calls == {"list": 2, "latest": 2, "ci": 2, "review": 2}


@pytest.mark.asyncio
async def test_review_reconciliation_can_run_from_recent_metadata_cache(
    monkeypatch, observation_boundaries
):
    clock = ManualClock()
    calls = {"list": 0, "latest": 0, "ci": 0, "review": 0}
    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: (
            calls.__setitem__("list", calls["list"] + 1) or [_raw_pr()]
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_latest_commit",
        lambda number, cwd=None: (
            calls.__setitem__("latest", calls["latest"] + 1)
            or ("head-1", "2026-08-08T00:00:00Z")
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_ci_checks",
        lambda number, cwd=None: (
            calls.__setitem__("ci", calls["ci"] + 1) or []
        ),
    )
    monkeypatch.setattr(
        github_api,
        "scan_review_threads",
        lambda number, latest, cwd=None: (
            calls.__setitem__("review", calls["review"] + 1) or ([], [])
        ),
    )

    controller = ObservationController(
        clock=clock,
        review_reconciliation_interval=timedelta(minutes=1),
    )
    orch = orchestrator.Orchestrator(
        repo_cwd="/repos/widgets", observation_controller=controller
    )
    await orch.refresh_prs()

    clock.advance(timedelta(minutes=1))
    await orch.refresh_prs()

    assert calls == {"list": 1, "latest": 2, "ci": 2, "review": 2}


@pytest.mark.asyncio
async def test_force_refresh_relists_and_reads_all_observations(
    monkeypatch, observation_boundaries
):
    clock = ManualClock()
    calls = {"list": 0, "latest": 0, "ci": 0, "review": 0}
    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: (
            calls.__setitem__("list", calls["list"] + 1) or [_raw_pr()]
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_latest_commit",
        lambda number, cwd=None: (
            calls.__setitem__("latest", calls["latest"] + 1)
            or ("head-1", "2026-08-08T00:00:00Z")
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_ci_checks",
        lambda number, cwd=None: (
            calls.__setitem__("ci", calls["ci"] + 1) or []
        ),
    )
    monkeypatch.setattr(
        github_api,
        "scan_review_threads",
        lambda number, latest, cwd=None: (
            calls.__setitem__("review", calls["review"] + 1) or ([], [])
        ),
    )

    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)
    await orch.refresh_prs()
    await orch.refresh_prs(force=True)

    assert calls == {"list": 2, "latest": 2, "ci": 2, "review": 2}


@pytest.mark.asyncio
async def test_draft_events_project_mutable_metadata_and_suppress_dispatch(
    monkeypatch, observation_boundaries
):
    clock = ManualClock()
    state = {
        "draft": False,
        "branch": "feature/widgets",
        "url": "https://github.com/org/widgets/pull/7?revision=1",
    }

    def list_open_prs(cwd=None):
        raw = _raw_pr()
        raw["isDraft"] = state["draft"]
        raw["headRefName"] = state["branch"]
        raw["url"] = state["url"]
        return [raw]

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)
    monkeypatch.setattr(
        orchestrator, "find_worktree_for_branch", lambda branch, root=None: "/worktree"
    )
    monkeypatch.setattr(
        orchestrator.maintenance, "load_state", lambda worktree, number: None
    )
    monkeypatch.setattr(
        orchestrator.coordinator,
        "dispatch_decision_for_pr",
        lambda pr: SimpleNamespace(should_dispatch=True, state="dispatch", reason=""),
    )
    dispatched = []

    def capture_task(coro):
        dispatched.append(coro)
        coro.close()

    monkeypatch.setattr(orchestrator.asyncio, "create_task", capture_task)

    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)
    await orch.refresh_prs()
    pr = orch.get_pr(7, repo="org/widgets")
    assert pr is not None
    pr.review_comments = [
        ReviewComment(
            id=1,
            author="reviewer",
            body="Please fix this",
            created_at="2026-08-08T00:00:00Z",
        )
    ]

    state.update(
        draft=True,
        branch="feature/widgets-draft",
        url="https://github.com/org/widgets/pull/7?revision=2",
    )
    await orch.handle_github_event(
        "pull_request", "org/widgets", 7, "head-1", action="converted_to_draft"
    )
    clock.advance(timedelta(seconds=2))
    await orch.refresh_prs()

    assert pr.is_draft is True
    assert pr.branch == "feature/widgets-draft"
    assert pr.url.endswith("revision=2")
    assert dispatched == []

    state.update(
        draft=False,
        branch="feature/widgets-ready",
        url="https://github.com/org/widgets/pull/7?revision=3",
    )
    await orch.handle_github_event(
        "pull_request", "org/widgets", 7, "head-1", action="ready_for_review"
    )
    clock.advance(timedelta(seconds=2))
    await orch.refresh_prs()

    assert pr.is_draft is False
    assert pr.branch == "feature/widgets-ready"
    assert pr.url.endswith("revision=3")
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_event_waits_for_refresh_transaction_and_is_not_lost(
    monkeypatch, observation_boundaries
):
    clock = ManualClock()
    list_entered = asyncio.Event()
    release_list = asyncio.Event()
    raw = [_raw_pr()]
    calls = {"list": 0, "review": 0}

    async def fake_to_thread(fn, *args, **kwargs):
        if fn is github_api.list_open_prs:
            calls["list"] += 1
            if calls["list"] == 1:
                list_entered.set()
                await release_list.wait()
            return list(raw)
        return fn(*args, **kwargs)

    monkeypatch.setattr(orchestrator.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: raw)
    monkeypatch.setattr(
        github_api,
        "scan_review_threads",
        lambda number, latest, cwd=None: (
            calls.__setitem__("review", calls["review"] + 1) or ([], [])
        ),
    )

    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)
    refresh = asyncio.create_task(orch.refresh_prs(force=True))
    await list_entered.wait()
    event = asyncio.create_task(
        orch.handle_github_event("pull_request_review", "org/widgets", 7, "head-1")
    )
    await asyncio.get_running_loop().run_in_executor(None, lambda: None)
    assert not event.done()

    release_list.set()
    await refresh
    await event
    clock.advance(timedelta(seconds=2))
    await orch.refresh_prs()

    assert calls["review"] == 2


@pytest.mark.asyncio
async def test_unknown_repo_event_does_not_invalidate_another_repo(
    monkeypatch, observation_boundaries
):
    clock = ManualClock()
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr()])
    calls = {"review": 0}
    monkeypatch.setattr(
        github_api,
        "scan_review_threads",
        lambda number, latest, cwd=None: (
            calls.__setitem__("review", calls["review"] + 1) or ([], [])
        ),
    )
    orch = orchestrator.Orchestrator(repo_cwd="/repos/widgets")
    orch.observation_controller = ObservationController(clock=clock)
    await orch.refresh_prs()

    await orch.handle_github_event(
        "pull_request_review", "org/other", 7, "head-1"
    )
    clock.advance(timedelta(seconds=2))
    await orch.refresh_prs()

    assert calls["review"] == 1
