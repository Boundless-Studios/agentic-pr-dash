import asyncio
from pathlib import Path

from agentic_pr_dash import app as dashboard_app
from agentic_pr_dash import github_api, maintenance, orchestrator, session_registry
from agentic_pr_dash.models import AgentProcess, MaintenanceStatus, PRData, PRStatus, ReviewComment


def _comment(comment_id: int = 55) -> ReviewComment:
    return ReviewComment(
        id=comment_id,
        author="reviewer",
        body="still needs work",
        path="file.py",
        line=10,
        created_at="2026-06-11T12:00:00Z",
    )


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
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda branch: str(worktree))
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(github_api, "get_latest_commit", lambda pr_number, cwd=None: ("sha", "2026-06-11T12:00:00Z"))
    monkeypatch.setattr(github_api, "get_ci_checks", lambda pr_number, cwd=None: [])
    monkeypatch.setattr(github_api, "get_unaddressed_comments", lambda pr_number, latest_commit_date, cwd=None: [_comment()])

    created_tasks = []

    def fake_create_task(coro):
        created_tasks.append(coro)
        coro.close()

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    orch = orchestrator.Orchestrator(repo_cwd=None)

    asyncio.run(orch.refresh_prs())

    assert len(created_tasks) == 1
    assert orch.prs[123].status == PRStatus.HAS_COMMENTS


def test_refresh_keeps_fresh_queued_state_after_stale_owner_retry(monkeypatch, tmp_path: Path):
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
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda branch: str(worktree))
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(github_api, "get_latest_commit", lambda pr_number, cwd=None: ("sha", "2026-06-11T12:00:00Z"))
    monkeypatch.setattr(github_api, "get_ci_checks", lambda pr_number, cwd=None: [])
    monkeypatch.setattr(github_api, "get_unaddressed_comments", lambda pr_number, latest_commit_date, cwd=None: [_comment()])

    created_tasks = []

    def fake_create_task(coro):
        created_tasks.append(coro)
        coro.close()

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    orch = orchestrator.Orchestrator(repo_cwd=None)

    asyncio.run(orch.refresh_prs())

    assert created_tasks == []


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
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda branch: str(worktree))
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(github_api, "get_latest_commit", lambda pr_number, cwd=None: ("sha", "2026-06-11T12:00:00Z"))
    monkeypatch.setattr(github_api, "get_ci_checks", lambda pr_number, cwd=None: [])
    monkeypatch.setattr(github_api, "get_unaddressed_comments", lambda pr_number, latest_commit_date, cwd=None: [_comment()])

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
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda branch: str(worktree))
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(github_api, "get_latest_commit", lambda pr_number, cwd=None: ("sha", "2026-06-11T12:00:00Z"))
    monkeypatch.setattr(github_api, "get_ci_checks", lambda pr_number, cwd=None: [])
    monkeypatch.setattr(github_api, "get_unaddressed_comments", lambda pr_number, latest_commit_date, cwd=None: [_comment()])

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
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda branch: str(worktree))
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(github_api, "get_latest_commit", lambda pr_number, cwd=None: ("sha", "2026-06-11T12:00:00Z"))
    monkeypatch.setattr(github_api, "get_ci_checks", lambda pr_number, cwd=None: [])
    monkeypatch.setattr(github_api, "get_unaddressed_comments", lambda pr_number, latest_commit_date, cwd=None: [_comment()])

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
        lambda: session_registry.SessionSummary(
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
        lambda: session_registry.SessionSummary(
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
