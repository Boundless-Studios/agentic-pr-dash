from datetime import datetime, timezone
import os

from agent_coordinator.models import OwnerIdentity
from agent_coordinator.service import TaskCoordinator
from agent_coordinator.store import JsonlClaimStore
from agentic_pr_dash import coordinator
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
