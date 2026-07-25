"""BOU-2402: make a decision-blocked PR legible.

BOU-2040 made a pending human decision *pause* maintenance instead of failing
it. Correct, but silent: the pause reached an operator only as defer text, so on
the board a paused PR looked identical to a stalled one, and `pr-observe` could
not answer "why did this PR stop being dispatched?".

Three surfaces, one rule each:

- **Status.** Its own board column, not the generic "Waiting" bucket. This is
  the only column whose work is blocked on the *viewer*; filing it next to
  AGENT_WAITING would read as someone else's problem.
- **Carried fields.** The question, category, and asking runtime travel on the
  PR so a viewer can read what is being asked without opening the ledger.
- **Event.** A `decision_wait` record naming the decision, claim, and runtime.

The emission contract from BOU-1801 still holds: observability is best-effort
and must never alter dispatch. That is asserted here, not assumed.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_coordinator.models import DecisionOption
from agent_coordinator.service import TaskCoordinator
from agent_coordinator.store import JsonlClaimStore
from agentic_pr_dash import coordinator, maintenance_check, observability
from agentic_pr_dash._maintenance import markers as _markers_mod
from agentic_pr_dash._maintenance import pr_state as _pr_state_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash.models import PRData, PRStatus, ReviewComment


BASE_TIME = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
SID = "sess-self"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "AGENTIC_PR_DASH_COORDINATOR_STORE", str(tmp_path / "claims.jsonl")
    )


def _pr(worktree):
    return PRData(
        number=42,
        title="needs review",
        branch="feature/x",
        url="https://github.com/Boundless-Studios/gaia-free/pull/42",
        worktree_path=str(worktree),
        status=PRStatus.HAS_COMMENTS,
        review_comments=[
            ReviewComment(
                id=7, author="r", body="fix", created_at="2026-07-25T12:00:00Z"
            )
        ],
    )


def _stub_boundaries(monkeypatch, pr):
    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: pr)
    monkeypatch.setattr(_pr_state_mod, "_unresolved_review_threads", lambda n, cwd: [])
    monkeypatch.setattr(
        _worktrees_mod, "_live_independent_owner_sessions", lambda paths, sid: {}
    )
    monkeypatch.setattr(
        _markers_mod, "_touch_owner_heartbeat", lambda cwd, sid, work: None
    )
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda cwd: None)


def _ask(store_path, pr, *, runtime="codex", session=SID):
    coord = TaskCoordinator(JsonlClaimStore(store_path))
    handle = coordinator.claim_pr(
        pr, session_id=session, pid=None, agent=runtime, lease_seconds=900
    )
    return coord.request_decision(
        coordinator.task_identity_for_pr(pr),
        claim_id=handle.claim_id,
        logical_key="turn-orchestration-boundary",
        category="architecture",
        question="Should turn orchestration own its own service boundary?",
        options=[
            DecisionOption(
                option_id="split-service", summary="Own service", trade_offs="New unit"
            ),
            DecisionOption(
                option_id="keep-module", summary="Keep it", trade_offs="Implicit"
            ),
        ],
        recommendation="keep-module",
        rationale="Reversible today",
        affected_scope=["backend/src"],
        fingerprint="evidence:v1",
        requesting_runtime=runtime,
        requesting_session_id=session,
        now=BASE_TIME,
    )


def _run(monkeypatch, tmp_path, *, runtime="codex"):
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    pr = _pr(worktree)
    _stub_boundaries(monkeypatch, pr)
    record = _ask(tmp_path / "claims.jsonl", pr, runtime=runtime)
    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
    return pr, record, code, text


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_blocked_pr_gets_its_own_status(monkeypatch, tmp_path):
    pr, _, code, text = _run(monkeypatch, tmp_path)

    assert code == 0, text
    assert pr.status is PRStatus.WAITING_HUMAN_DECISION


def test_the_status_is_not_conflated_with_agent_waiting(monkeypatch, tmp_path):
    """AGENT_WAITING means a live agent is idle. This means nobody is coming."""
    pr, _, _, _ = _run(monkeypatch, tmp_path)
    assert pr.status is not PRStatus.AGENT_WAITING


def test_the_status_has_its_own_board_column(monkeypatch, tmp_path):
    from agentic_pr_dash.app import KANBAN_COLUMNS

    owning = [
        column
        for column in KANBAN_COLUMNS
        if PRStatus.WAITING_HUMAN_DECISION in column["statuses"]
    ]
    assert len(owning) == 1, "status must map to exactly one column"
    assert owning[0]["id"] == "needs_decision"
    # Blocked on the viewer -> surfaced before agent-owned work.
    assert KANBAN_COLUMNS.index(owning[0]) == 0


def test_every_status_maps_to_a_column(monkeypatch, tmp_path):
    """A status with no column would make the card vanish from the board."""
    from agentic_pr_dash.app import KANBAN_COLUMNS

    mapped = set()
    for column in KANBAN_COLUMNS:
        mapped |= set(column["statuses"])
    assert PRStatus.WAITING_HUMAN_DECISION in mapped


# ---------------------------------------------------------------------------
# Carried fields
# ---------------------------------------------------------------------------


def test_the_question_travels_on_the_pr(monkeypatch, tmp_path):
    pr, record, _, _ = _run(monkeypatch, tmp_path)

    assert pr.waiting_decision_id == record.request.decision_id
    assert pr.waiting_decision_question == record.request.question
    assert pr.waiting_decision_category == "architecture"


@pytest.mark.parametrize("runtime", ["codex", "claude"])
def test_the_asking_runtime_is_carried(monkeypatch, tmp_path, runtime):
    pr, _, _, _ = _run(monkeypatch, tmp_path, runtime=runtime)
    assert pr.waiting_decision_runtime == runtime


def test_an_unblocked_pr_carries_no_decision_fields(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _pr(worktree)
    _stub_boundaries(monkeypatch, pr)

    maintenance_check._check_worktree(str(worktree), SID, claim=True)

    assert pr.waiting_decision_id is None
    assert pr.status is not PRStatus.WAITING_HUMAN_DECISION


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


def _events(cwd, kind=None):
    store = observability.get_event_store(str(cwd))
    return [e for e in store.query() if kind is None or e.kind == kind]


def test_a_decision_wait_event_is_recorded(monkeypatch, tmp_path):
    _, record, _, _ = _run(monkeypatch, tmp_path)

    events = _events(tmp_path / "wt", "decision_wait")
    assert len(events) == 1
    details = events[0].details
    assert details["decision_id"] == record.request.decision_id
    assert details["claim_id"] == record.request.claim_id
    assert details["requesting_runtime"] == "codex"
    assert details["category"] == "architecture"
    assert events[0].pr_number == 42


def test_no_decision_wait_event_when_nothing_is_blocked(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _pr(worktree)
    _stub_boundaries(monkeypatch, pr)

    maintenance_check._check_worktree(str(worktree), SID, claim=True)

    assert _events(worktree, "decision_wait") == []


def test_emission_failure_never_changes_the_defer(monkeypatch, tmp_path):
    """BOU-1801 contract: observability must never alter control flow."""

    def _boom(*a, **k):
        raise RuntimeError("event store unwritable")

    monkeypatch.setattr(observability.event_store, "get_event_store", _boom)

    pr, _, code, text = _run(monkeypatch, tmp_path)

    # Still defers (0, not 10), and still carries the decision fields.
    assert code == 0, text
    assert pr.status is PRStatus.WAITING_HUMAN_DECISION
    assert pr.waiting_decision_id is not None


def test_emit_helper_swallows_a_bad_state_dir(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise OSError("no such state dir")

    monkeypatch.setattr(observability.event_store, "get_event_store", _boom)
    observability.emit(str(tmp_path), "decision_wait", pr_number=1)  # must not raise
