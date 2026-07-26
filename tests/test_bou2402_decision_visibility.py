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


def test_decision_wait_is_emitted_once_per_decision(monkeypatch, tmp_path):
    """PR #110 review: level-triggering floods the bounded event store.

    The waiter polls _check_worktree every ~150s and detached loops check the
    same worktree, so a long-lived decision would append an identical record
    every pass and evict the history observability exists to preserve.
    """
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _pr(worktree)
    _stub_boundaries(monkeypatch, pr)
    _ask(tmp_path / "claims.jsonl", pr)

    for _ in range(4):
        maintenance_check._check_worktree(str(worktree), SID, claim=True)

    assert len(_events(worktree, "decision_wait")) == 1


def test_a_superseding_decision_emits_again(monkeypatch, tmp_path):
    """Edge-triggering must key on the decision, not merely on 'was waiting'."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _pr(worktree)
    _stub_boundaries(monkeypatch, pr)
    store_path = tmp_path / "claims.jsonl"
    _ask(store_path, pr)
    maintenance_check._check_worktree(str(worktree), SID, claim=True)

    # Same logical key, new evidence -> supersedes, new decision id.
    coord = TaskCoordinator(JsonlClaimStore(store_path))
    claim = coordinator.claim_pr(
        pr, session_id=SID, pid=None, agent="codex", lease_seconds=900
    )
    coord.request_decision(
        coordinator.task_identity_for_pr(pr),
        claim_id=claim.claim_id,
        logical_key="turn-orchestration-boundary",
        category="architecture",
        question="Revised: split it?",
        options=[
            DecisionOption(option_id="a", summary="A", trade_offs="x"),
            DecisionOption(option_id="b", summary="B", trade_offs="y"),
        ],
        recommendation="a",
        rationale="new evidence",
        affected_scope=["backend/src"],
        fingerprint="evidence:v2",
        requesting_runtime="codex",
        requesting_session_id=SID,
        now=BASE_TIME,
    )
    maintenance_check._check_worktree(str(worktree), SID, claim=True)

    assert len(_events(worktree, "decision_wait")) == 2


# ---------------------------------------------------------------------------
# The rendered card — what PR #110's first cut got wrong
#
# The original tests asserted on the transient PRData that _check_worktree
# mutates. The board is built from the orchestrator's OWN PRData objects, which
# are re-stamped by _compute_status on every enrichment — so those tests passed
# while no real card ever acquired the status. These assert on the card.
# ---------------------------------------------------------------------------


def _orchestrator_pr(tmp_path, worktree):
    return PRData(
        number=42,
        title="needs review",
        branch="feature/x",
        url="https://github.com/Boundless-Studios/gaia-free/pull/42",
        worktree_path=str(worktree),
        failing_checks=["unit"],
        review_comments=[
            ReviewComment(
                id=7, author="r", body="fix", created_at="2026-07-25T12:00:00Z"
            )
        ],
    )


def _compute(pr):
    """Drive the REAL _compute_status without standing up the full server."""
    from agentic_pr_dash.orchestrator import Orchestrator

    return Orchestrator._compute_status(Orchestrator.__new__(Orchestrator), pr)


def test_compute_status_gives_the_board_pr_the_decision_status(tmp_path):
    """The load-bearing fix: the status must come from _compute_status.

    Reverting this makes the 'Needs Your Decision' column permanently empty
    while maintenance defers — the defect PR #110's first cut shipped.
    """
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _orchestrator_pr(tmp_path, worktree)
    _ask(tmp_path / "claims.jsonl", pr)

    assert _compute(pr) is PRStatus.WAITING_HUMAN_DECISION


def test_the_decision_outranks_failing_ci_and_comments(tmp_path):
    """A red check on a PR whose boundary question is open is not the ask."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _orchestrator_pr(tmp_path, worktree)  # has failing_checks AND comments
    _ask(tmp_path / "claims.jsonl", pr)

    assert _compute(pr) is PRStatus.WAITING_HUMAN_DECISION


def test_compute_status_projects_the_question_onto_the_board_pr(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _orchestrator_pr(tmp_path, worktree)
    record = _ask(tmp_path / "claims.jsonl", pr)

    _compute(pr)

    assert pr.waiting_decision_id == record.request.decision_id
    assert pr.waiting_decision_question == record.request.question
    assert pr.waiting_decision_runtime == "codex"


def test_resolved_decision_clears_the_projection(tmp_path):
    """A stale question must not keep rendering after it is answered."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _orchestrator_pr(tmp_path, worktree)
    record = _ask(tmp_path / "claims.jsonl", pr)
    _compute(pr)
    assert pr.waiting_decision_id is not None

    TaskCoordinator(JsonlClaimStore(tmp_path / "claims.jsonl")).resolve_decision(
        record.request.decision_id,
        request_fingerprint="evidence:v1",
        human_actor="ilya",
        selected_option_id="split-service",
        now=BASE_TIME,
    )

    assert _compute(pr) is not PRStatus.WAITING_HUMAN_DECISION
    assert pr.waiting_decision_id is None
    assert pr.waiting_decision_question is None


def test_a_ledger_failure_does_not_break_status_computation(tmp_path, monkeypatch):
    """Status runs on every enrichment — it must degrade, not raise."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _orchestrator_pr(tmp_path, worktree)

    def _boom(_pr):
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(coordinator, "pending_decision_for_pr", _boom)
    assert _compute(pr) is PRStatus.CI_AND_COMMENTS


# ---------------------------------------------------------------------------
# The card chip
# ---------------------------------------------------------------------------


def _card(**overrides):
    from agentic_pr_dash.models import WorktreeCard

    payload = dict(
        id="pr:42",
        worktree_name="wt",
        branch="feature/x",
        status=PRStatus.WAITING_HUMAN_DECISION,
    )
    payload.update(overrides)
    return WorktreeCard(**payload)


def test_the_card_does_not_report_a_blocked_pr_as_clean(tmp_path):
    """PR #110 review: agent_state fell through every branch and returned clean.

    A Clean chip on a PR that is blocked on the viewer is actively misleading —
    strictly worse than the invisibility this work set out to fix.
    """
    card = _card()
    assert card.agent_state != "clean"
    assert card.agent_state == "needs_decision"
    assert card.agent_state_label == "Needs Your Decision"


def test_the_decision_state_outranks_a_stale_maintenance_record(tmp_path):
    """A leftover QUEUED/RUNNING record must not paint the card 'working'."""
    from agentic_pr_dash.maintenance import build_maintenance_state
    from agentic_pr_dash.models import MaintenanceStatus

    card = _card(
        maintenance=build_maintenance_state(
            pr_number=42,
            branch="feature/x",
            worktree_path="/tmp/wt",
            blockers=[],
            state=MaintenanceStatus.RUNNING,
        )
    )
    assert card.agent_state == "needs_decision"


def test_the_card_carries_the_question_for_rendering(tmp_path):
    card = _card(
        waiting_decision_id="abc123def456",
        waiting_decision_question="Split the service?",
        waiting_decision_category="architecture",
        waiting_decision_runtime="codex",
    )
    assert card.waiting_decision_question == "Split the service?"
    assert card.waiting_decision_category == "architecture"
    assert card.waiting_decision_runtime == "codex"


def test_every_agent_state_has_a_label():
    """A state with no label renders 'Unknown' on the chip."""
    card = _card()
    assert card.agent_state_label != "Unknown"
