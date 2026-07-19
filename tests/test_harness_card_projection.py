from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_pr_dash import app, session_registry
from agentic_pr_dash.models import PRData, PRStatus


def _session(**overrides: object) -> session_registry.RuntimeSessionState:
    values: dict[str, object] = {
        "session_id": "conversation-1",
        "event": "harness_status",
        "timestamp": "2026-07-19T10:00:00Z",
        "cli": "codex",
        "worktree_path": "/tmp/worktree",
        "chain_id": "chain-1",
        "generation": 2,
        "supervisor_state": "running",
        "context_percent": 67.5,
        "context_tokens": 675_000,
        "window_tokens": 1_000_000,
        "cumulative_tokens": 9_500_000,
        "context_confidence": "confident",
        "quiescence": "busy",
        "active_turns": 1,
        "active_tools": 2,
        "active_subagents": 1,
        "active_critical_sections": 0,
        "checkpoint_fingerprint": "abc123",
        "outbox_depth": 3,
        "harness_reported_at": datetime.now(timezone.utc).isoformat(),
    }
    values.update(overrides)
    return session_registry.RuntimeSessionState(**values)


@pytest.mark.parametrize(
    ("state", "quiescence", "active_turns", "expected"),
    [
        ("running", "busy", 1, "working"),
        ("warning", "busy", 0, "working"),
        ("draining", "idle", 0, "working"),
        ("checkpointing", "idle", 0, "working"),
        ("fenced", "idle", 0, "working"),
        ("launching", "idle", 0, "working"),
        ("awaiting_ack", "idle", 0, "working"),
        ("running", "idle", 0, "idle"),
        ("blocked", "idle", 0, "idle"),
        ("running", "unknown", 0, "none"),
    ],
)
def test_harness_activity_maps_supervisor_and_quiescence(
    state: str,
    quiescence: str,
    active_turns: int,
    expected: str,
):
    runtime_session = _session(
        supervisor_state=state,
        quiescence=quiescence,
        active_turns=active_turns,
        active_tools=0,
        active_subagents=0,
    )

    assert app._harness_activity_state(runtime_session) == expected


def test_fresh_harness_activity_wins_and_stale_status_falls_back(monkeypatch):
    monkeypatch.setattr(app, "_legacy_agent_activity_state", lambda path: "idle")
    fresh = _session(quiescence="busy")
    stale = _session(
        harness_reported_at=(
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat()
    )

    assert app._resolve_agent_working("/tmp/worktree", False, fresh) is True
    assert app._resolve_agent_working("/tmp/worktree", True, stale) is False


def test_runtime_selection_prefers_live_conversation_over_newer_terminal_one():
    active = _session(
        session_id="active",
        timestamp="2026-07-19T10:00:00Z",
    )
    terminal = _session(
        session_id="terminal",
        event="completed",
        timestamp="2026-07-19T10:01:00Z",
    )
    summary = session_registry.SessionSummary(
        sessions={"active": active, "terminal": terminal}
    )
    summary.reindex()

    assert app._runtime_session_for_worktree("/tmp/worktree", summary) is active


def test_runtime_selection_does_not_prefer_abandoned_stale_report_over_terminal():
    stale = _session(
        session_id="stale",
        timestamp="2026-07-19T10:00:00Z",
        harness_reported_at=(
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat(),
    )
    terminal = _session(
        session_id="terminal",
        event="completed",
        timestamp="2026-07-19T10:01:00Z",
    )
    summary = session_registry.SessionSummary(
        sessions={"stale": stale, "terminal": terminal}
    )
    summary.reindex()

    assert app._runtime_session_for_worktree("/tmp/worktree", summary) is terminal


def test_runtime_card_marks_stale_harness_projection():
    stale = _session(
        harness_reported_at=(
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat()
    )

    fields = app._runtime_card_fields(stale)

    assert fields["runtime_status_stale"] is True


def test_worktree_card_projects_full_harness_status(monkeypatch):
    runtime_session = _session()
    pr = PRData(
        number=2195,
        title="Surface harness status",
        branch="bou-2195",
        url="https://github.com/Boundless-Studios/agentic-pr-dash/pull/2195",
        worktree_path="/tmp/worktree",
        status=PRStatus.CLEAN,
        created_at="2026-07-19T09:00:00Z",
    )
    monkeypatch.setattr(app, "_ownership_for_card", lambda **kwargs: {})
    monkeypatch.setattr(app, "_legacy_agent_activity_state", lambda path: "none")

    card = app._build_card_for_worktree(
        {"path": "/tmp/worktree", "branch": "bou-2195"},
        pr,
        [],
        runtime_session,
    )

    assert card.status == PRStatus.AGENT_WORKING
    assert card.runtime_chain_id == "chain-1"
    assert card.runtime_generation == 2
    assert card.supervisor_state == "running"
    assert card.context_percent == 67.5
    assert card.context_tokens == 675_000
    assert card.window_tokens == 1_000_000
    assert card.cumulative_tokens == 9_500_000
    assert card.context_confidence == "confident"
    assert card.runtime_quiescence == "busy"
    assert card.runtime_active_turns == 1
    assert card.runtime_active_tools == 2
    assert card.runtime_active_subagents == 1
    assert card.runtime_active_critical_sections == 0
    assert card.runtime_checkpoint_fingerprint == "abc123"
    assert card.runtime_outbox_depth == 3
