"""BOU-2491: a dashboard claim (advisory, `can_execute=false`) must never
suppress the loop's dispatch decision for a PR with real blockers.

BOU-2490 (`coordinator.claim_pr`, `actor=` param) already stamps
``can_execute`` metadata on every claim, so a dashboard-queued claim is
correctly marked non-executing. But `dispatch_decision_for_pr` — the single
function both the automatic poll path AND `_check_worktree` (the loop's
actual dispatch precursor) consult — never READS that metadata: it only asks
the coordinator whether the claim is "reclaimable" (time/pid-based), so an
advisory claim with a live pid-less owner reads exactly like a real, healthy,
in-progress fix and suppresses dispatch — the dashboard queued work nobody
will ever execute, and the loop defers to it forever (bounded only by lease
expiry). This is "the component that CANNOT run an executor holding a claim
against the component that CAN" from the ticket, reproduced at the
`dispatch_decision_for_pr` level.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

from agent_coordinator.models import OwnerIdentity
from agent_coordinator.service import TaskCoordinator
from agent_coordinator.store import JsonlClaimStore

from agentic_pr_dash import coordinator
from agentic_pr_dash.models import MaintenanceActor, PRData, ReviewComment

BASE_TIME = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)


def _pr(**kwargs) -> PRData:
    base = {
        "number": 2491,
        "title": "Fix PR",
        "branch": "feature/fix",
        "url": "https://github.com/Boundless-Studios/gaia-free/pull/2491",
        "worktree_path": "/tmp/wt-2491",
        "failing_checks": ["unit"],
        "review_comments": [
            ReviewComment(id=1, author="reviewer", body="fix this", created_at="2026-06-11T12:00:00Z"),
        ],
    }
    base.update(kwargs)
    return PRData(**base)


def test_advisory_dashboard_claim_does_not_block_dispatch(tmp_path, monkeypatch):
    store = tmp_path / "claims.jsonl"
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(store))
    pr = _pr()
    task = coordinator.task_identity_for_pr(pr)
    # Mirror orchestrator.dispatch_pr_maintenance's real claim shape: pid=None,
    # actor=DASHBOARD_QUEUE (advisory, can_execute=false).
    TaskCoordinator(JsonlClaimStore(store)).claim_task(
        task,
        OwnerIdentity(
            session_id="agentic-pr-dash-dashboard", pid=None,
            worktree_path=pr.worktree_path,
            metadata={"actor": MaintenanceActor.DASHBOARD_QUEUE.value, "can_execute": "false"},
        ),
        lease_seconds=300,
        now=BASE_TIME,
    )

    decision = coordinator.dispatch_decision_for_pr(
        pr, now=BASE_TIME, caller_can_execute=True
    )

    assert decision.should_dispatch is True, (
        "an advisory (can_execute=false) claim must never suppress dispatch "
        "for a PR with real blockers (BOU-2491) — the dashboard queued work "
        "nobody will execute, and the loop must not defer to it as if it "
        "were a real in-progress fix"
    )


def test_executing_caller_replaces_advisory_claim_before_claiming(tmp_path, monkeypatch):
    store = tmp_path / "claims.jsonl"
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(store))
    pr = _pr()
    TaskCoordinator(JsonlClaimStore(store)).claim_task(
        coordinator.task_identity_for_pr(pr),
        OwnerIdentity(
            session_id="dashboard", pid=None, worktree_path=pr.worktree_path,
            metadata={"actor": MaintenanceActor.DASHBOARD_QUEUE.value, "can_execute": "false"},
        ),
        lease_seconds=300,
        now=BASE_TIME,
    )

    assert coordinator.release_advisory_claim_for_pr(pr, now=BASE_TIME) is True
    handle = coordinator.claim_pr(
        pr, session_id="loop", pid=os.getpid(), agent="loop", lease_seconds=300,
        actor=MaintenanceActor.LOOP_EXECUTOR,
    )
    assert handle is not None


def test_advisory_claim_still_suppresses_a_non_executing_callers_requeue(
    tmp_path, monkeypatch,
):
    """Control: the orchestrator's OWN poll loop asks this same question to
    decide whether to re-queue (NOT whether to execute) — it must keep
    respecting its own advisory claim, or every 60s poll re-queues the same
    handoff forever (the duplicate-dispatch bug this mechanism exists for)."""
    store = tmp_path / "claims.jsonl"
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(store))
    pr = _pr()
    task = coordinator.task_identity_for_pr(pr)
    TaskCoordinator(JsonlClaimStore(store)).claim_task(
        task,
        OwnerIdentity(
            session_id="agentic-pr-dash-dashboard", pid=None,
            worktree_path=pr.worktree_path,
            metadata={"actor": MaintenanceActor.DASHBOARD_QUEUE.value, "can_execute": "false"},
        ),
        lease_seconds=300,
        now=BASE_TIME,
    )

    # Default caller_can_execute=False — the orchestrator's own re-queue check.
    decision = coordinator.dispatch_decision_for_pr(pr, now=BASE_TIME)

    assert decision.should_dispatch is False, (
        "a non-executing caller re-asking the SAME question must keep "
        "respecting its own advisory claim, or the dashboard re-queues the "
        "same maintenance handoff on every poll tick"
    )


def test_executing_claim_still_suppresses_dispatch(tmp_path, monkeypatch):
    """Control: a genuinely executing claim (loop-executor) must still block,
    unchanged — only advisory claims lose their blocking power."""
    store = tmp_path / "claims.jsonl"
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(store))
    pr = _pr()
    task = coordinator.task_identity_for_pr(pr)
    TaskCoordinator(JsonlClaimStore(store)).claim_task(
        task,
        OwnerIdentity(
            session_id="loop-session", pid=os.getpid(),
            worktree_path=pr.worktree_path,
            metadata={"actor": MaintenanceActor.LOOP_EXECUTOR.value, "can_execute": "true"},
        ),
        lease_seconds=300,
        now=BASE_TIME,
    )

    decision = coordinator.dispatch_decision_for_pr(pr, now=BASE_TIME)

    assert decision.should_dispatch is False, (
        "a genuinely executing claim must still suppress duplicate dispatch"
    )


def test_advisory_release_refuses_a_claim_the_decision_was_not_made_from(
    tmp_path, monkeypatch
):
    """The release must be fenced to the DISPATCH DECISION's claim identity.

    Blockers change concurrently, so active advisory claims for several
    fingerprints can coexist. `_best_active_claim_for_pr` is fingerprint-
    agnostic, so it can surface a NEWER claim for unrelated blockers. Releasing
    that one is doubly wrong: the decision's own advisory claim survives (so
    `claim_pr` still conflicts and the executor defers anyway) while an
    unrelated dashboard claim loses its duplicate-dispatch guard.
    """
    store = tmp_path / "claims.jsonl"
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(store))
    pr = _pr()
    TaskCoordinator(JsonlClaimStore(store)).claim_task(
        coordinator.task_identity_for_pr(pr),
        OwnerIdentity(
            session_id="dashboard", pid=None, worktree_path=pr.worktree_path,
            metadata={"actor": MaintenanceActor.DASHBOARD_QUEUE.value, "can_execute": "false"},
        ),
        lease_seconds=300,
        now=BASE_TIME,
    )

    assert coordinator.release_advisory_claim_for_pr(
        pr, claim_id="some-other-fingerprints-claim", now=BASE_TIME
    ) is False, (
        "a claim_id that does not match the decision's must refuse the release, "
        "not fall back to releasing whichever claim looks 'best'"
    )

    # ...and the real claim is untouched, so the guard it provides survives.
    still_there = coordinator._best_active_claim_for_pr(pr, now=BASE_TIME)
    assert still_there is not None and still_there.status == "active"

    # Control: passing the matching identity still releases.
    assert coordinator.release_advisory_claim_for_pr(
        pr, claim_id=still_there.claim_id, now=BASE_TIME
    ) is True
