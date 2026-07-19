import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_coordinator.models import OwnerIdentity
from agent_coordinator.service import TaskCoordinator
from agent_coordinator.store import JsonlClaimStore
from agentic_pr_dash import coordinator as pr_coordinator
from agentic_pr_dash import app as dashboard_app
from agentic_pr_dash import github_api, maintenance, orchestrator, session_registry
from agentic_pr_dash.models import AgentProcess, MaintenanceStatus, PRData, PRStatus, ReviewComment


@pytest.fixture(autouse=True)
def _isolate_coordinator_store(tmp_path: Path, monkeypatch):
    """Point the agent-coordinator store at a per-test path so claims created by
    these tests never read/write an ambient store and expectations can't depend
    on claims left by other runs (codex P2)."""
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(tmp_path / "coordinator-claims.jsonl"))


def _comment(comment_id: int = 55) -> ReviewComment:
    return ReviewComment(
        id=comment_id,
        author="reviewer",
        body="still needs work",
        path="file.py",
        line=10,
        created_at="2026-06-11T12:00:00Z",
    )


def _claim_current_pr(pr: PRData) -> None:
    TaskCoordinator(JsonlClaimStore(pr_coordinator.store_path())).claim_task(
        pr_coordinator.task_identity_for_pr(pr),
        OwnerIdentity(session_id="owner-1", pid=os.getpid(), worktree_path=pr.worktree_path),
        lease_seconds=300,
    )


def test_dashboard_dispatch_claims_and_suppresses_duplicate_requeue(monkeypatch, tmp_path: Path):
    """The dashboard dispatch path must create a coordinator claim so the next
    60s refresh with unchanged blockers does NOT re-queue the same maintenance
    (codex P1)."""
    worktree = tmp_path / "feature-one"
    worktree.mkdir()

    pr = PRData(
        number=321,
        title="Fix comments",
        branch="feature/one",
        url="https://github.com/Boundless-Studios/gaia-free/pull/321",
        worktree_path=str(worktree),
        status=PRStatus.HAS_COMMENTS,
        review_comments=[_comment()],
    )

    monkeypatch.setattr(github_api, "get_failed_logs", lambda *a, **k: {})

    orch = orchestrator.Orchestrator(repo_cwd=None)

    # First dispatch creates the claim.
    assert pr_coordinator.dispatch_decision_for_pr(pr).should_dispatch is True
    asyncio.run(orch.dispatch_pr_maintenance(pr))

    # An unchanged-blocker refresh now sees the active claim and is suppressed.
    decision = pr_coordinator.dispatch_decision_for_pr(pr)
    assert decision.should_dispatch is False
    assert decision.state == "active"


def test_dashboard_skips_dispatch_when_live_in_session_owner_holds_marker(monkeypatch, tmp_path: Path):
    """Session precedence: the dashboard must NOT dispatch headless maintenance
    (nor claim) for a PR whose worktree a LIVE in-session owner holds — a
    pid-alive .gaia/pr-watch.armed marker naming another session. The live
    session owns its PR; the detached loop only services unowned worktrees."""
    from agentic_pr_dash import maintenance_check as mc  # noqa: PLC0415

    worktree = tmp_path / "feature-owned"
    worktree.mkdir()
    # Live in-session owner marker (a different session, pid alive).
    mc._write_arm_marker(str(worktree), "live-session-x", os.getpid(), 321)

    pr = PRData(
        number=321,
        title="Fix comments",
        branch="feature/owned",
        url="https://github.com/Boundless-Studios/gaia-free/pull/321",
        worktree_path=str(worktree),
        status=PRStatus.HAS_COMMENTS,
        review_comments=[_comment()],
    )
    monkeypatch.setattr(github_api, "get_failed_logs", lambda *a, **k: {})

    orch = orchestrator.Orchestrator(repo_cwd=None)
    asyncio.run(orch.dispatch_pr_maintenance(pr))

    # The dashboard deferred: no maintenance queued and no coordinator claim.
    assert pr.maintenance is None
    assert pr.coordinator_claim is None
    # Nothing claimed it, so it stays dispatchable (for the live session / an
    # unowned-worktree takeover later) — the dashboard simply stood down.
    assert pr_coordinator.dispatch_decision_for_pr(pr).should_dispatch is True


def test_refresh_requeues_matching_active_state_when_owner_session_ended(monkeypatch, tmp_path: Path):
    """A queued state from a dead/closed session must not suppress open comments."""

    worktree = tmp_path / "feature-one"
    worktree.mkdir()
    registry = tmp_path / "sessions.jsonl"
    monkeypatch.setenv("AGENTIC_PR_DASH_SESSION_REGISTRY", str(registry))

    queued_state = maintenance.build_maintenance_state(
        pr_number=123,
        branch="feature/one",
        worktree_path=str(worktree),
        blockers=["review_comments"],
        state=MaintenanceStatus.QUEUED,
        review_comment_ids=[55],
    )
    maintenance.save_state(queued_state)

    session_registry.record_event(
        event="started",
        session_id="s1",
        cli="codex",
        launch_source="launch-worktree-cli",
        pid=123,
        worktree_path=str(worktree),
        branch="feature/one",
        pr_number=123,
        feature_pipeline=True,
        path=registry,
    )
    session_registry.record_event(
        event="failed",
        session_id="s1",
        cli="codex",
        launch_source="launch-worktree-cli",
        worktree_path=str(worktree),
        branch="feature/one",
        pr_number=123,
        feature_pipeline=True,
        path=registry,
    )

    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: [
            {
                "number": 123,
                "title": "Fix comments",
                "headRefName": "feature/one",
                "baseRefName": "main",
                "url": "https://example.com/pr/123",
                "isDraft": False,
                "labels": [],
            }
        ],
    )
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda branch, root=None: str(worktree))
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(github_api, "get_latest_commit", lambda pr_number, cwd=None: ("sha", "2026-06-11T12:00:00Z"))
    monkeypatch.setattr(github_api, "get_ci_checks", lambda pr_number, cwd=None: [])
    monkeypatch.setattr(github_api, "scan_review_threads", lambda pr_number, latest_commit_date, cwd=None: ([_comment()], []))

    created_tasks = []

    def fake_create_task(coro):
        created_tasks.append(coro)
        coro.close()

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    orch = orchestrator.Orchestrator(repo_cwd=None)

    asyncio.run(orch.refresh_prs())

    assert len(created_tasks) == 1
    assert orch.get_pr(123).status == PRStatus.HAS_COMMENTS


def test_refresh_requeues_fresh_queued_state_without_active_coordinator_claim(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "feature-one"
    worktree.mkdir()
    registry = tmp_path / "sessions.jsonl"
    session_registry.record_event(
        event="started",
        session_id="s1",
        cli="codex",
        launch_source="launch-worktree-cli",
        pid=123,
        worktree_path=str(worktree),
        branch="feature/one",
        pr_number=123,
        feature_pipeline=True,
        path=registry,
    )
    session_registry.record_event(
        event="failed",
        session_id="s1",
        cli="codex",
        launch_source="launch-worktree-cli",
        worktree_path=str(worktree),
        branch="feature/one",
        pr_number=123,
        feature_pipeline=True,
        path=registry,
    )
    monkeypatch.setenv("AGENTIC_PR_DASH_SESSION_REGISTRY", str(registry))

    queued_state = maintenance.build_maintenance_state(
        pr_number=123,
        branch="feature/one",
        worktree_path=str(worktree),
        blockers=["review_comments"],
        state=MaintenanceStatus.QUEUED,
        review_comment_ids=[55],
    )
    maintenance.save_state(queued_state)

    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: [
            {
                "number": 123,
                "title": "Fix comments",
                "headRefName": "feature/one",
                "baseRefName": "main",
                "url": "https://example.com/pr/123",
                "isDraft": False,
                "labels": [],
            }
        ],
    )
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda branch, root=None: str(worktree))
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(github_api, "get_latest_commit", lambda pr_number, cwd=None: ("sha", "2026-06-11T12:00:00Z"))
    monkeypatch.setattr(github_api, "get_ci_checks", lambda pr_number, cwd=None: [])
    monkeypatch.setattr(github_api, "scan_review_threads", lambda pr_number, latest_commit_date, cwd=None: ([_comment()], []))

    created_tasks = []

    def fake_create_task(coro):
        created_tasks.append(coro)
        coro.close()

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    orch = orchestrator.Orchestrator(repo_cwd=None)

    asyncio.run(orch.refresh_prs())

    assert len(created_tasks) == 1


def test_refresh_requeues_when_prequeue_owner_session_is_dead(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "feature-one"
    worktree.mkdir()
    registry = tmp_path / "sessions.jsonl"
    session_registry.record_event(
        event="started",
        session_id="s1",
        cli="codex",
        launch_source="launch-worktree-cli",
        pid=123,
        worktree_path=str(worktree),
        branch="feature/one",
        pr_number=123,
        feature_pipeline=True,
        path=registry,
    )
    monkeypatch.setenv("AGENTIC_PR_DASH_SESSION_REGISTRY", str(registry))
    monkeypatch.setattr(session_registry, "pid_is_live", lambda pid: False)

    queued_state = maintenance.build_maintenance_state(
        pr_number=123,
        branch="feature/one",
        worktree_path=str(worktree),
        blockers=["review_comments"],
        state=MaintenanceStatus.QUEUED,
        review_comment_ids=[55],
    )
    queued_state.last_signal_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    maintenance.save_state(queued_state)

    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: [
            {
                "number": 123,
                "title": "Fix comments",
                "headRefName": "feature/one",
                "baseRefName": "main",
                "url": "https://example.com/pr/123",
                "isDraft": False,
                "labels": [],
            }
        ],
    )
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda branch, root=None: str(worktree))
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(github_api, "get_latest_commit", lambda pr_number, cwd=None: ("sha", "2026-06-11T12:00:00Z"))
    monkeypatch.setattr(github_api, "get_ci_checks", lambda pr_number, cwd=None: [])
    monkeypatch.setattr(github_api, "scan_review_threads", lambda pr_number, latest_commit_date, cwd=None: ([_comment()], []))

    created_tasks = []

    def fake_create_task(coro):
        created_tasks.append(coro)
        coro.close()

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    orch = orchestrator.Orchestrator(repo_cwd=None)

    asyncio.run(orch.refresh_prs())

    assert len(created_tasks) == 1


def test_matching_session_owner_ignores_dead_owner_before_fresh_queue(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "feature-one"
    worktree.mkdir()
    registry = tmp_path / "sessions.jsonl"
    session_registry.record_event(
        event="started",
        session_id="s1",
        cli="codex",
        launch_source="launch-worktree-cli",
        pid=123,
        worktree_path=str(worktree),
        branch="feature/one",
        pr_number=123,
        feature_pipeline=True,
        path=registry,
    )
    monkeypatch.setenv("AGENTIC_PR_DASH_SESSION_REGISTRY", str(registry))
    monkeypatch.setattr(session_registry, "pid_is_live", lambda pid: False)
    monkeypatch.setattr(orchestrator, "_has_process_owner", lambda pr: False)

    pr = PRData(
        number=123,
        title="Queued after dead owner",
        branch="feature/one",
        url="https://example.com/pr/123",
        worktree_path=str(worktree),
        status=PRStatus.HAS_COMMENTS,
    )
    queued_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    assert orchestrator._has_matching_session_owner(pr, queued_at=queued_at) is None


def test_refresh_keeps_matching_active_state_when_owner_session_is_live(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "feature-one"
    worktree.mkdir()
    registry = tmp_path / "sessions.jsonl"
    session_registry.record_event(
        event="started",
        session_id="s1",
        cli="codex",
        launch_source="launch-worktree-cli",
        pid=123,
        worktree_path=str(worktree),
        branch="feature/one",
        pr_number=123,
        feature_pipeline=True,
        path=registry,
    )
    monkeypatch.setenv("AGENTIC_PR_DASH_SESSION_REGISTRY", str(registry))
    monkeypatch.setattr(session_registry, "pid_is_live", lambda pid: pid == 123)

    queued_state = maintenance.build_maintenance_state(
        pr_number=123,
        branch="feature/one",
        worktree_path=str(worktree),
        blockers=["review_comments"],
        state=MaintenanceStatus.QUEUED,
        review_comment_ids=[55],
    )
    maintenance.save_state(queued_state)

    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: [
            {
                "number": 123,
                "title": "Fix comments",
                "headRefName": "feature/one",
                "baseRefName": "main",
                "url": "https://example.com/pr/123",
                "isDraft": False,
                "labels": [],
            }
        ],
    )
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda branch, root=None: str(worktree))
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(github_api, "get_latest_commit", lambda pr_number, cwd=None: ("sha", "2026-06-11T12:00:00Z"))
    monkeypatch.setattr(github_api, "get_ci_checks", lambda pr_number, cwd=None: [])
    monkeypatch.setattr(github_api, "scan_review_threads", lambda pr_number, latest_commit_date, cwd=None: ([_comment()], []))
    _claim_current_pr(
        PRData(
            number=123,
            title="Fix comments",
            branch="feature/one",
            url="https://example.com/pr/123",
            worktree_path=str(worktree),
            review_comments=[_comment()],
        )
    )

    created_tasks = []

    def fake_create_task(coro):
        created_tasks.append(coro)
        coro.close()

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    orch = orchestrator.Orchestrator(repo_cwd=None)

    asyncio.run(orch.refresh_prs())

    assert created_tasks == []


def test_refresh_keeps_matching_active_state_for_process_only_owner(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "feature-one"
    worktree.mkdir()
    registry = tmp_path / "sessions.jsonl"
    session_registry.record_event(
        event="started",
        session_id="s1",
        cli="codex",
        launch_source="launch-worktree-cli",
        pid=123,
        worktree_path=str(worktree),
        branch="feature/one",
        pr_number=123,
        feature_pipeline=True,
        path=registry,
    )
    session_registry.record_event(
        event="failed",
        session_id="s1",
        cli="codex",
        launch_source="launch-worktree-cli",
        worktree_path=str(worktree),
        branch="feature/one",
        pr_number=123,
        feature_pipeline=True,
        path=registry,
    )
    monkeypatch.setenv("AGENTIC_PR_DASH_SESSION_REGISTRY", str(registry))
    monkeypatch.setattr(
        orchestrator.agents,
        "discover_primary_feature_pipeline_agents",
        lambda paths, **kw: {str(worktree): [AgentProcess(pid=999, cli_name="codex", label="Codex")]},
    )

    queued_state = maintenance.build_maintenance_state(
        pr_number=123,
        branch="feature/one",
        worktree_path=str(worktree),
        blockers=["review_comments"],
        state=MaintenanceStatus.QUEUED,
        review_comment_ids=[55],
    )
    maintenance.save_state(queued_state)

    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: [
            {
                "number": 123,
                "title": "Fix comments",
                "headRefName": "feature/one",
                "baseRefName": "main",
                "url": "https://example.com/pr/123",
                "isDraft": False,
                "labels": [],
            }
        ],
    )
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda branch, root=None: str(worktree))
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(github_api, "get_latest_commit", lambda pr_number, cwd=None: ("sha", "2026-06-11T12:00:00Z"))
    monkeypatch.setattr(github_api, "get_ci_checks", lambda pr_number, cwd=None: [])
    monkeypatch.setattr(github_api, "scan_review_threads", lambda pr_number, latest_commit_date, cwd=None: ([_comment()], []))
    _claim_current_pr(
        PRData(
            number=123,
            title="Fix comments",
            branch="feature/one",
            url="https://example.com/pr/123",
            worktree_path=str(worktree),
            review_comments=[_comment()],
        )
    )

    created_tasks = []

    def fake_create_task(coro):
        created_tasks.append(coro)
        coro.close()

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    orch = orchestrator.Orchestrator(repo_cwd=None)

    asyncio.run(orch.refresh_prs())

    assert created_tasks == []


def test_refresh_reads_session_registry_from_target_worktree_config(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "feature-one"
    worktree.mkdir()
    registry = tmp_path / "custom-sessions.jsonl"
    (worktree / "agentic-pr-dash.toml").write_text(
        f'session_registry_path = "{registry}"\n',
        encoding="utf-8",
    )
    queued_state = maintenance.build_maintenance_state(
        pr_number=123,
        branch="feature/one",
        worktree_path=str(worktree),
        blockers=["review_comments"],
        state=MaintenanceStatus.QUEUED,
        review_comment_ids=[55],
    )
    maintenance.save_state(queued_state)
    session_registry.record_event(
        event="started",
        session_id="s1",
        cli="codex",
        launch_source="launch-worktree-cli",
        pid=123,
        worktree_path=str(worktree),
        branch="feature/one",
        pr_number=123,
        feature_pipeline=True,
        path=registry,
    )
    session_registry.record_event(
        event="failed",
        session_id="s1",
        cli="codex",
        launch_source="launch-worktree-cli",
        worktree_path=str(worktree),
        branch="feature/one",
        pr_number=123,
        feature_pipeline=True,
        path=registry,
    )

    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: [
            {
                "number": 123,
                "title": "Fix comments",
                "headRefName": "feature/one",
                "baseRefName": "main",
                "url": "https://example.com/pr/123",
                "isDraft": False,
                "labels": [],
            }
        ],
    )
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda branch, root=None: str(worktree))
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(github_api, "get_latest_commit", lambda pr_number, cwd=None: ("sha", "2026-06-11T12:00:00Z"))
    monkeypatch.setattr(github_api, "get_ci_checks", lambda pr_number, cwd=None: [])
    monkeypatch.setattr(github_api, "scan_review_threads", lambda pr_number, latest_commit_date, cwd=None: ([_comment()], []))

    created_tasks = []

    def fake_create_task(coro):
        created_tasks.append(coro)
        coro.close()

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    orch = orchestrator.Orchestrator(repo_cwd=None)

    asyncio.run(orch.refresh_prs())

    assert len(created_tasks) == 1


def test_build_cards_terminal_session_does_not_leave_clean_pr_working(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "feature-one"
    worktree.mkdir()
    runtime_session = session_registry.RuntimeSessionState(
        session_id="s1",
        event="completed",
        timestamp="2026-06-11T12:00:00Z",
        cli="codex",
        launch_source="launch-worktree-cli",
        pid=123,
        worktree_path=str(worktree),
        branch="feature/one",
        pr_number=123,
        is_feature_pipeline=True,
    )
    monkeypatch.setattr(
        dashboard_app,
        "discover_worktrees",
        lambda: [{"path": str(worktree), "branch": "feature/one", "environment_name": "feature-one"}],
    )
    monkeypatch.setattr(
        dashboard_app,
        "discover_active_agents",
        lambda paths: {str(worktree): [AgentProcess(pid=123, cli_name="codex", label="Codex")]},
    )
    monkeypatch.setattr(
        dashboard_app.session_registry,
        "summarize_sessions",
        lambda path=None: session_registry.SessionSummary(
            sessions={"s1": runtime_session},
            by_worktree={str(worktree): runtime_session},
        ),
    )
    monkeypatch.setattr(dashboard_app, "_load_babysit_activity", lambda: ({}, {}))

    orch = orchestrator.Orchestrator(repo_cwd=None)
    orch.prs = {
        123: PRData(
            number=123,
            title="Ready",
            branch="feature/one",
            url="https://example.com/pr/123",
            worktree_path=str(worktree),
            status=PRStatus.CLEAN,
        )
    }
    monkeypatch.setattr(dashboard_app, "orchestrator", orch)

    card = dashboard_app.build_worktree_cards()[0][0]

    assert card.status == PRStatus.CLEAN
    assert card.activity_message == "Codex watching"


def test_build_cards_keeps_live_agent_when_different_session_is_terminal(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "feature-one"
    worktree.mkdir()
    runtime_session = session_registry.RuntimeSessionState(
        session_id="s1",
        event="completed",
        timestamp="2026-06-11T12:00:00Z",
        cli="codex",
        launch_source="launch-worktree-cli",
        pid=123,
        worktree_path=str(worktree),
        branch="feature/one",
        pr_number=123,
        is_feature_pipeline=True,
    )
    monkeypatch.setattr(
        dashboard_app,
        "discover_worktrees",
        lambda: [{"path": str(worktree), "branch": "feature/one", "environment_name": "feature-one"}],
    )
    monkeypatch.setattr(
        dashboard_app,
        "discover_active_agents",
        lambda paths: {str(worktree): [AgentProcess(pid=999, cli_name="codex", label="Codex")]},
    )
    monkeypatch.setattr(
        dashboard_app.session_registry,
        "summarize_sessions",
        lambda path=None: session_registry.SessionSummary(
            sessions={"s1": runtime_session},
            by_worktree={str(worktree): runtime_session},
        ),
    )
    monkeypatch.setattr(dashboard_app, "_load_babysit_activity", lambda: ({}, {}))

    orch = orchestrator.Orchestrator(repo_cwd=None)
    orch.prs = {
        123: PRData(
            number=123,
            title="Ready",
            branch="feature/one",
            url="https://example.com/pr/123",
            worktree_path=str(worktree),
            status=PRStatus.CLEAN,
        )
    }
    monkeypatch.setattr(dashboard_app, "orchestrator", orch)

    card = dashboard_app.build_worktree_cards()[0][0]

    assert card.status == PRStatus.AGENT_WORKING
    assert card.activity_message == "Codex working"


def test_build_cards_keeps_activity_only_agent_when_terminal_session_has_no_agent_match(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "feature-one"
    worktree.mkdir()
    state_dir = worktree / ".agentic-pr-dash"
    state_dir.mkdir()
    runtime_session = session_registry.RuntimeSessionState(
        session_id="s1",
        event="completed",
        timestamp="2026-06-11T12:00:00Z",
        cli="codex",
        launch_source="launch-worktree-cli",
        pid=123,
        worktree_path=str(worktree),
        branch="feature/one",
        pr_number=123,
        is_feature_pipeline=True,
    )
    busy_since = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    (state_dir / "agent-activity.json").write_text(
        json.dumps(
            {
                "sessions": {
                    "s2": {
                        "state": "busy",
                        "busy_since": busy_since,
                        "updated": busy_since,
                        "pid": 999,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        dashboard_app,
        "discover_worktrees",
        lambda: [{"path": str(worktree), "branch": "feature/one", "environment_name": "feature-one"}],
    )
    monkeypatch.setattr(dashboard_app, "discover_active_agents", lambda paths: {})
    monkeypatch.setattr(
        dashboard_app.session_registry,
        "summarize_sessions",
        lambda path=None: session_registry.SessionSummary(
            sessions={"s1": runtime_session},
            by_worktree={str(worktree): runtime_session},
        ),
    )
    monkeypatch.setattr(dashboard_app.session_registry, "pid_is_live", lambda pid: pid == 999)
    monkeypatch.setattr(dashboard_app, "_load_babysit_activity", lambda: ({}, {}))

    orch = orchestrator.Orchestrator(repo_cwd=None)
    orch.prs = {
        123: PRData(
            number=123,
            title="Ready",
            branch="feature/one",
            url="https://example.com/pr/123",
            worktree_path=str(worktree),
            status=PRStatus.CLEAN,
        )
    }
    monkeypatch.setattr(dashboard_app, "orchestrator", orch)

    card = dashboard_app.build_worktree_cards()[0][0]

    assert card.status == PRStatus.AGENT_WORKING


def test_build_cards_terminal_hidden_agent_worktree_does_not_leave_clean_pr_working(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "agent-finished"
    worktree.mkdir()
    runtime_session = session_registry.RuntimeSessionState(
        session_id="s1",
        event="completed",
        timestamp="2026-06-11T12:00:00Z",
        cli="codex",
        launch_source="launch-worktree-cli",
        pid=123,
        worktree_path=str(worktree),
        branch="agent-finished",
        pr_number=123,
        is_feature_pipeline=True,
    )
    monkeypatch.setattr(
        dashboard_app,
        "discover_worktrees",
        lambda: [{"path": str(worktree), "branch": "agent-finished", "environment_name": "agent-finished"}],
    )
    monkeypatch.setattr(
        dashboard_app,
        "discover_active_agents",
        lambda paths: {str(worktree): [AgentProcess(pid=123, cli_name="codex", label="Codex")]},
    )
    monkeypatch.setattr(
        dashboard_app.session_registry,
        "summarize_sessions",
        lambda path=None: session_registry.SessionSummary(
            sessions={"s1": runtime_session},
            by_worktree={str(worktree): runtime_session},
        ),
    )
    monkeypatch.setattr(dashboard_app, "_load_babysit_activity", lambda: ({}, {}))

    orch = orchestrator.Orchestrator(repo_cwd=None)
    orch.prs = {
        123: PRData(
            number=123,
            title="Ready",
            branch="agent-finished",
            url="https://example.com/pr/123",
            worktree_path=str(worktree),
            status=PRStatus.CLEAN,
        )
    }
    monkeypatch.setattr(dashboard_app, "orchestrator", orch)

    cards, worktree_count, hidden_count = dashboard_app.build_worktree_cards()
    card = cards[0]

    assert worktree_count == 0
    assert hidden_count == 1
    assert card.worktree_hidden is True
    assert card.status == PRStatus.CLEAN
    assert card.activity_message == "Codex watching"


def test_build_cards_reads_hidden_runtime_session_from_worktree_registry(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "agent-finished"
    worktree.mkdir()
    default_registry = tmp_path / "default-sessions.jsonl"
    custom_registry = tmp_path / "custom-sessions.jsonl"
    runtime_session = session_registry.RuntimeSessionState(
        session_id="s1",
        event="completed",
        timestamp="2026-06-11T12:00:00Z",
        cli="codex",
        launch_source="launch-worktree-cli",
        pid=123,
        worktree_path=str(worktree),
        branch="agent-finished",
        pr_number=123,
        is_feature_pipeline=True,
    )
    monkeypatch.setattr(
        dashboard_app,
        "discover_worktrees",
        lambda: [{"path": str(worktree), "branch": "agent-finished", "environment_name": "agent-finished"}],
    )
    monkeypatch.setattr(
        dashboard_app,
        "discover_active_agents",
        lambda paths: {str(worktree): [AgentProcess(pid=123, cli_name="codex", label="Codex")]},
    )
    monkeypatch.setattr(
        dashboard_app.session_registry,
        "registry_path",
        lambda cwd=None: custom_registry if cwd == str(worktree) else default_registry,
    )

    def summarize_sessions(path=None):
        if path == custom_registry:
            return session_registry.SessionSummary(
                sessions={"s1": runtime_session},
                by_worktree={str(worktree): runtime_session},
            )
        return session_registry.SessionSummary()

    monkeypatch.setattr(dashboard_app.session_registry, "summarize_sessions", summarize_sessions)
    monkeypatch.setattr(dashboard_app, "_load_babysit_activity", lambda: ({}, {}))

    orch = orchestrator.Orchestrator(repo_cwd=None)
    orch.prs = {
        123: PRData(
            number=123,
            title="Ready",
            branch="agent-finished",
            url="https://example.com/pr/123",
            worktree_path=str(worktree),
            status=PRStatus.CLEAN,
        )
    }
    monkeypatch.setattr(dashboard_app, "orchestrator", orch)

    cards, worktree_count, hidden_count = dashboard_app.build_worktree_cards()
    card = cards[0]

    assert worktree_count == 0
    assert hidden_count == 1
    assert card.worktree_hidden is True
    assert card.status == PRStatus.CLEAN
    assert card.activity_message == "Codex watching"
