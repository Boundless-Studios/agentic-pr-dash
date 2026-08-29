"""BOU-2365 — the board must separate active work from idle/waiting liveness.

Before this change a live session was ``Agent Working`` on process/heartbeat
liveness alone: ``draining`` counted as work, quiescent sessions counted as
work, and a bare CPU-discovered process counted as work. A merged PR whose chat
process lingered therefore stayed parked in the ``Agent Working`` column.

These tests pin the five precedence scenarios from the ticket plus the guard
that a genuinely working agent on a reclaimable worktree still reads as working.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentic_pr_dash import app, session_registry
from agentic_pr_dash.models import AgentProcess, PRData, PRStatus, WorktreeCard


def _session(**overrides: object) -> session_registry.RuntimeSessionState:
    values: dict[str, object] = {
        "session_id": "conversation-2365",
        "event": "harness_status",
        "timestamp": "2026-07-24T10:00:00Z",
        "cli": "codex",
        "agent_name": "fleet-mantis",
        "worktree_path": "/tmp/wt-2365",
        "supervisor_state": "running",
        "quiescence": "idle",
        "active_turns": 0,
        "active_tools": 0,
        "active_subagents": 0,
        "active_critical_sections": 0,
        "harness_reported_at": datetime.now(timezone.utc).isoformat(),
    }
    values.update(overrides)
    return session_registry.RuntimeSessionState(**values)


def _pr(**overrides: object) -> PRData:
    values: dict[str, object] = {
        "number": 2780,
        "title": "Add Linear MCP to local agent",
        "branch": "bou-2362-local-agent-needs-linear",
        "url": "https://github.com/Boundless-Studios/gaia-free/pull/2780",
        "worktree_path": "/tmp/wt-2365",
        "status": PRStatus.CLEAN,
        "created_at": "2026-07-24T09:00:00Z",
    }
    values.update(overrides)
    return PRData(**values)


@pytest.fixture(autouse=True)
def _isolate_card_builder(monkeypatch):
    """Keep the card builders off the network/filesystem probes."""
    monkeypatch.setattr(app, "get_main_repo_root", lambda: "/tmp/main")
    monkeypatch.setattr(app.orchestrator, "observed_roots", {"/tmp/main"})
    monkeypatch.setattr(app, "_ownership_for_card", lambda **kwargs: {})
    monkeypatch.setattr(app, "_legacy_agent_activity_state", lambda path: "none")
    monkeypatch.setattr(
        app,
        "_selected_worktree_cleanup_reason",
        lambda worktree, agents, **kwargs: (
            False,
            "selected worktree is not stale enough",
        ),
    )


def _reclaimable(monkeypatch, reason: str = "merged PR branch") -> None:
    monkeypatch.setattr(
        app,
        "_selected_worktree_cleanup_reason",
        lambda worktree, agents, **kwargs: (not agents, reason),
    )


def _card(pr, active_agents, runtime_session):
    return app._build_card_for_worktree(
        {"path": "/tmp/wt-2365", "branch": "bou-2362-local-agent-needs-linear"},
        pr,
        active_agents,
        runtime_session,
    )


# ---------------------------------------------------------------------------
# The activity signal itself is three-valued
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("supervisor_state", "quiescence", "active_turns", "expected"),
    [
        # Real work.
        ("running", "busy", 1, "working"),
        ("warning", "busy", 0, "working"),
        # Rotation machinery mid-flight is short-lived genuine work.
        ("checkpointing", "idle", 0, "working"),
        ("fenced", "idle", 0, "working"),
        ("launching", "idle", 0, "working"),
        ("awaiting_ack", "idle", 0, "working"),
        # Wind-down and blocked phases are NOT work (the BOU-2365 regression).
        ("draining", "idle", 0, "waiting"),
        ("stopping", "idle", 0, "waiting"),
        ("stopped", "idle", 0, "waiting"),
        ("blocked", "idle", 0, "waiting"),
        # A quiescent session is waiting, not working.
        ("running", "idle", 0, "waiting"),
        # No usable signal at all.
        ("running", "unknown", 0, "none"),
    ],
)
def test_harness_activity_is_three_valued(
    supervisor_state: str, quiescence: str, active_turns: int, expected: str
):
    runtime_session = _session(
        supervisor_state=supervisor_state,
        quiescence=quiescence,
        active_turns=active_turns,
    )

    assert app._harness_activity_state(runtime_session) == expected


def test_bare_live_process_is_waiting_not_working():
    """AC #1 — liveness alone must never resolve to 'working'."""
    assert app._resolve_agent_activity("/tmp/wt-2365", True, None) == "waiting"
    assert app._resolve_agent_activity("/tmp/wt-2365", False, None) == "none"


# ---------------------------------------------------------------------------
# Scenario (a) — active session + active implementation
# ---------------------------------------------------------------------------

def test_active_session_with_active_implementation_is_agent_working():
    card = _card(
        _pr(),
        [AgentProcess(pid=4242, cli_name="codex", label="Codex")],
        _session(supervisor_state="running", quiescence="busy", active_turns=1),
    )

    assert card.status == PRStatus.AGENT_WORKING
    assert card.agent_state == "working"
    assert card.agent_state_label == "Agent Working"
    assert card.waiting_reason is None


def test_dispatched_maintenance_run_is_agent_working_without_a_harness_signal():
    """The loop's executor has no activity hook — maintenance state carries it."""
    from agentic_pr_dash.models import MaintenanceState, MaintenanceStatus

    card = _card(
        _pr(
            maintenance=MaintenanceState(
                pr_number=2780,
                branch="bou-2362-local-agent-needs-linear",
                worktree_path="/tmp/wt-2365",
                state=MaintenanceStatus.RUNNING,
            )
        ),
        [AgentProcess(pid=4242, cli_name="codex", label="Codex")],
        None,
    )

    assert card.status == PRStatus.AGENT_WORKING
    assert card.agent_state == "working"


def test_dashboard_dispatch_inflight_is_agent_working(monkeypatch):
    """An executor the dashboard just dispatched is work, not mere liveness."""
    monkeypatch.setattr(app.orchestrator, "_inflight_prs", {2780}, raising=False)

    card = _card(_pr(), [], None)

    assert card.status == PRStatus.AGENT_WORKING
    assert card.agent_state == "working"


# ---------------------------------------------------------------------------
# Scenario (b) — live session waiting for user input
# ---------------------------------------------------------------------------

def test_live_session_awaiting_user_is_waiting():
    card = _card(
        _pr(),
        [AgentProcess(pid=4242, cli_name="codex", label="Codex")],
        _session(supervisor_state="running", quiescence="idle"),
    )

    assert card.session_activity == "waiting"
    assert card.agent_state == "waiting"
    assert card.agent_state_label == "Waiting"
    assert card.waiting_reason == "user input"
    assert card.state_chip_label == "Waiting · user input"
    assert card.activity_message == "Codex watching"
    # The column still answers "what does this PR need", not "who is sitting
    # here" — an idle session must not pull a clean PR out of the Clean column.
    assert card.status == PRStatus.CLEAN


def test_worktree_without_a_pr_and_an_idle_session_lands_in_waiting():
    """The only card routed to the Waiting column by activity alone."""
    card = _card(
        None,
        [AgentProcess(pid=4242, cli_name="codex", label="Codex")],
        _session(supervisor_state="running", quiescence="idle"),
    )

    assert card.status == PRStatus.AGENT_WAITING
    assert card.agent_state == "waiting"


# ---------------------------------------------------------------------------
# Scenario (c) — live session waiting for CI / review
# ---------------------------------------------------------------------------

def test_live_session_awaiting_external_checks_is_waiting():
    card = _card(
        _pr(status=PRStatus.CI_PENDING, ci_watch_pending=True),
        [AgentProcess(pid=4242, cli_name="codex", label="Codex")],
        _session(supervisor_state="running", quiescence="idle"),
    )

    # Waiting outranks ci_pending on the chip; the column stays CI Pending.
    assert card.agent_state == "waiting"
    assert card.waiting_reason == "external checks"
    assert card.state_chip_label == "Waiting · external checks"
    assert card.status == PRStatus.CI_PENDING


def test_waiting_session_does_not_mask_an_actionable_pr():
    """A red PR stays in Needs Attention — waiting must not hide real work."""
    card = _card(
        _pr(status=PRStatus.CI_FAILING, failing_checks=["unit"]),
        [AgentProcess(pid=4242, cli_name="codex", label="Codex")],
        _session(supervisor_state="running", quiescence="idle"),
    )

    assert card.status == PRStatus.CI_FAILING
    assert card.agent_state == "ci_failing"
    assert card.session_activity == "waiting"


# ---------------------------------------------------------------------------
# Scenario (d) — merged PR + draining session
# ---------------------------------------------------------------------------

def test_merged_pr_with_draining_session_is_ready_for_cleanup(monkeypatch):
    """The exact BOU-2365 repro: PR #2780 merged, harness Draining, chat alive."""
    _reclaimable(monkeypatch)

    card = _card(None, [], _session(supervisor_state="draining", quiescence="idle"))

    assert card.status == PRStatus.READY_CLEANUP
    assert card.agent_state == "ready_cleanup"
    assert card.agent_state_label == "Ready / Cleanup"
    assert card.cleanup_candidate is True


@pytest.mark.parametrize("status", list(PRStatus))
def test_tracked_pr_never_triggers_card_level_cleanup_probe(monkeypatch, status):
    observed: list[bool] = []

    def cleanup_reason(worktree, agents, *, check_remote_pr=True):
        observed.append(check_remote_pr)
        return (not check_remote_pr, "stale orphan")

    monkeypatch.setattr(app, "_selected_worktree_cleanup_reason", cleanup_reason)

    card = _card(_pr(status=status), [], None)

    assert observed == []
    assert card.status == status


def test_untracked_worktree_uses_local_cleanup_probe(monkeypatch):
    observed: list[bool] = []

    def cleanup_reason(worktree, agents, *, check_remote_pr=True):
        observed.append(check_remote_pr)
        return (True, "branch merged into main")

    monkeypatch.setattr(app, "_selected_worktree_cleanup_reason", cleanup_reason)

    card = _card(None, [], None)

    assert observed == [False]
    assert card.status == PRStatus.READY_CLEANUP


# ---------------------------------------------------------------------------
# Scenario (e) — completed worktree + lingering live process
# ---------------------------------------------------------------------------

def test_completed_worktree_with_lingering_process_is_ready_for_cleanup(monkeypatch):
    _reclaimable(monkeypatch)

    card = _card(None, [AgentProcess(pid=4242, cli_name="codex", label="Codex")], None)

    assert card.status == PRStatus.READY_CLEANUP
    assert card.agent_state == "ready_cleanup"
    # The lingering process still blocks the destructive cleanup action even
    # though it no longer hides the terminal display state.
    assert card.cleanup_candidate is False


def test_working_agent_on_a_reclaimable_worktree_still_reads_as_working(monkeypatch):
    _reclaimable(monkeypatch)

    card = _card(
        None,
        [AgentProcess(pid=4242, cli_name="codex", label="Codex")],
        _session(supervisor_state="running", quiescence="busy", active_turns=1),
    )

    assert card.status == PRStatus.AGENT_WORKING
    assert card.agent_state == "working"


# ---------------------------------------------------------------------------
# Board wiring
# ---------------------------------------------------------------------------

def test_every_status_is_claimed_exactly_once():
    """No status renders twice, and none renders nowhere.

    BOU-2431 moved NO_PR off the board onto the Worktrees tab, so a column is
    no longer the only way to be claimed — but being claimed exactly once is
    still the invariant that keeps a card from duplicating or vanishing.
    """
    claimed: dict[PRStatus, int] = {}
    for column in app.KANBAN_COLUMNS:
        for status in column["statuses"]:
            claimed[status] = claimed.get(status, 0) + 1
    for status in app.NO_PR_TAB_STATUSES:
        claimed[status] = claimed.get(status, 0) + 1

    assert set(claimed) == set(PRStatus), (
        f"Unclaimed statuses: {set(PRStatus) - set(claimed)}"
    )
    assert all(count == 1 for count in claimed.values()), claimed


def test_waiting_and_working_stay_distinct_states_inside_one_column():
    """BOU-2431 folded the "Waiting" COLUMN away; the distinction remains.

    Sharing a column is a layout decision. What BOU-2365 protects is that an
    agent idling on a poll is not indistinguishable from one doing work — that
    now lives on the card's state chip rather than in the column header.
    """
    titles = {column["id"]: column["title"] for column in app.KANBAN_COLUMNS}
    assert titles["ready_cleanup"] == "Ready / Cleanup"
    assert titles["in_progress"] == "Agent Working"

    working = WorktreeCard(id="w", worktree_name="w", branch="w", status=PRStatus.AGENT_WORKING)
    waiting = WorktreeCard(id="x", worktree_name="x", branch="x", status=PRStatus.AGENT_WAITING)

    column_ids = {
        card.status: column["id"]
        for column in app.build_columns([working, waiting])
        for card in column["cards"]
    }
    assert column_ids[PRStatus.AGENT_WORKING] == "in_progress"
    assert column_ids[PRStatus.AGENT_WAITING] == "in_progress"
    assert working.agent_state != waiting.agent_state
