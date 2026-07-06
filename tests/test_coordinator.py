from datetime import datetime, timezone
import os
import subprocess

from agent_coordinator.models import OwnerIdentity
from agent_coordinator.service import TaskCoordinator
from agent_coordinator.store import JsonlClaimStore
from agentic_pr_dash import coordinator, maintenance
from agentic_pr_dash.models import PRData, ReviewComment


BASE_TIME = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


def _comment(comment_id: int) -> ReviewComment:
    return ReviewComment(
        id=comment_id,
        author="reviewer",
        body="please fix",
        created_at="2026-06-11T12:00:00Z",
    )


def _pr(**kwargs) -> PRData:
    base = {
        "number": 123,
        "title": "Fix PR",
        "branch": "feature/fix",
        "url": "https://github.com/Boundless-Studios/gaia-free/pull/123",
        "worktree_path": "/tmp/wt",
        "failing_checks": ["unit", "lint"],
        "review_comments": [_comment(22), _comment(11)],
        "merge_state": "DIRTY",
    }
    base.update(kwargs)
    return PRData(**base)


def _git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo(cwd) -> None:
    cwd.mkdir(parents=True, exist_ok=True)
    _git(cwd, "init")
    _git(cwd, "config", "user.email", "test@example.com")
    _git(cwd, "config", "user.name", "Test User")
    (cwd / "README.md").write_text("initial\n", encoding="utf-8")
    _git(cwd, "add", "README.md")
    _git(cwd, "commit", "-m", "initial")
    _git(cwd, "checkout", "-B", "main")


def test_fingerprint_is_stable_and_changes_with_blocker_details():
    first = coordinator.fingerprint_for_pr(_pr())
    reordered = coordinator.fingerprint_for_pr(
        _pr(
            failing_checks=["lint", "unit"],
            review_comments=[_comment(11), _comment(22)],
        )
    )
    changed_comment = coordinator.fingerprint_for_pr(
        _pr(review_comments=[_comment(11), _comment(33)])
    )

    assert first == reordered
    assert first != changed_comment


def test_active_claim_suppresses_duplicate_dispatch(tmp_path, monkeypatch):
    store = tmp_path / "claims.jsonl"
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(store))
    pr = _pr()
    task = coordinator.task_identity_for_pr(pr)
    TaskCoordinator(JsonlClaimStore(store)).claim_task(
        task,
        OwnerIdentity(session_id="owner-1", pid=os.getpid(), worktree_path=pr.worktree_path),
        lease_seconds=300,
        now=BASE_TIME,
    )

    decision = coordinator.dispatch_decision_for_pr(pr, now=BASE_TIME)

    assert decision.should_dispatch is False
    assert decision.state == "active"


def test_released_claim_requeues_current_blockers(tmp_path, monkeypatch):
    store = tmp_path / "claims.jsonl"
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(store))
    pr = _pr()
    task = coordinator.task_identity_for_pr(pr)
    coord = TaskCoordinator(JsonlClaimStore(store))
    claim = coord.claim_task(
        task,
        OwnerIdentity(session_id="owner-1", pid=999, worktree_path=pr.worktree_path),
        lease_seconds=300,
        now=BASE_TIME,
    )
    coord.release_claim(claim.claim_id, owner_session_id="owner-1", reason="failed", now=BASE_TIME)

    decision = coordinator.dispatch_decision_for_pr(pr, now=BASE_TIME)

    assert decision.should_dispatch is True
    assert decision.state == "released"


def test_changed_fingerprint_is_new_work_even_with_active_old_claim(tmp_path, monkeypatch):
    store = tmp_path / "claims.jsonl"
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(store))
    original = _pr(review_comments=[_comment(11)])
    changed = _pr(review_comments=[_comment(11), _comment(22)])
    TaskCoordinator(JsonlClaimStore(store)).claim_task(
        coordinator.task_identity_for_pr(original),
        OwnerIdentity(session_id="owner-1", pid=999, worktree_path=original.worktree_path),
        lease_seconds=300,
        now=BASE_TIME,
    )

    decision = coordinator.dispatch_decision_for_pr(changed, now=BASE_TIME)

    assert decision.should_dispatch is True
    assert decision.state == "no_claim"


def test_dirty_released_owner_blocks_unsafe_takeover(tmp_path, monkeypatch):
    store = tmp_path / "claims.jsonl"
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(store))
    pr = _pr(worktree_path=str(tmp_path / "current"))
    owner_worktree = str(tmp_path / "owner")
    coord = TaskCoordinator(JsonlClaimStore(store))
    claim = coord.claim_task(
        coordinator.task_identity_for_pr(pr),
        OwnerIdentity(session_id="owner-1", pid=999, worktree_path=owner_worktree),
        lease_seconds=300,
        now=BASE_TIME,
    )
    coord.release_claim(claim.claim_id, owner_session_id="owner-1", reason="failed", now=BASE_TIME)
    monkeypatch.setattr(coordinator, "worktree_has_dirty_or_unpushed_changes", lambda path: path == owner_worktree)

    decision = coordinator.dispatch_decision_for_pr(pr, now=BASE_TIME)

    assert decision.should_dispatch is False
    assert decision.state == "manual_intervention"
    assert "PR #123 has 2 unaddressed review comments" in decision.reason
    assert owner_worktree in decision.reason


def test_handoff_file_does_not_make_worktree_dirty(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = repo / maintenance.HANDOFF_FILENAME
    handoff.write_text("old handoff\n", encoding="utf-8")
    _git(repo, "add", maintenance.HANDOFF_FILENAME)
    _git(repo, "commit", "-m", "track handoff")

    handoff.write_text("new handoff\n", encoding="utf-8")

    assert coordinator.worktree_has_dirty_or_unpushed_changes(str(repo)) is False

    (repo / "README.md").write_text("changed\n", encoding="utf-8")

    assert coordinator.worktree_has_dirty_or_unpushed_changes(str(repo)) is True


def test_unpushed_check_prefers_remote_branch_over_main_upstream(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "checkout", "-b", "feature/fix", "origin/main")
    (repo / "fix.txt").write_text("fix\n", encoding="utf-8")
    _git(repo, "add", "fix.txt")
    _git(repo, "commit", "-m", "fix")
    _git(repo, "push", "origin", "HEAD")

    assert _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") == "origin/main"
    assert coordinator.worktree_has_dirty_or_unpushed_changes(str(repo)) is False

    (repo / "more.txt").write_text("more\n", encoding="utf-8")
    _git(repo, "add", "more.txt")
    _git(repo, "commit", "-m", "more")

    assert coordinator.worktree_has_dirty_or_unpushed_changes(str(repo)) is True
