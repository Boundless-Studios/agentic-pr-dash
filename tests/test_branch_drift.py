"""Tests for shared session branch-drift detection."""

from __future__ import annotations

import json
from pathlib import Path

from agentic_pr_dash.branch_drift import branch_drift_message


def _write_events(path: Path, events: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def test_drift_detected_when_session_started_on_foreign_branch(tmp_path, monkeypatch):
    registry = tmp_path / "events.jsonl"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_events(
        registry,
        [
            {
                "session_id": "sess-A",
                "event": "started",
                "timestamp": "2026-06-06T07:00:00Z",
                "branch": "fix/original-owner",
                "worktree_path": str(worktree),
            }
        ],
    )
    monkeypatch.setenv("GAIA_SESSION_REGISTRY", str(registry))

    msg = branch_drift_message(["sess-A"], "fix/foreign-sibling", worktree)
    assert msg is not None
    assert "fix/original-owner" in msg
    assert "fix/foreign-sibling" in msg


def test_no_drift_when_started_branch_matches_current(tmp_path, monkeypatch):
    registry = tmp_path / "events.jsonl"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_events(
        registry,
        [
            {
                "session_id": "sess-A",
                "event": "started",
                "timestamp": "2026-06-06T07:00:00Z",
                "branch": "fix/same-branch",
                "worktree_path": str(worktree),
            }
        ],
    )
    monkeypatch.setenv("GAIA_SESSION_REGISTRY", str(registry))

    assert branch_drift_message(["sess-A"], "fix/same-branch", worktree) is None


def test_attributed_continuation_rebinds_branch_lineage(tmp_path, monkeypatch):
    registry = tmp_path / "events.jsonl"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_events(registry, [
        {"session_id": "sess-A", "event": "started", "branch": "fix/first",
         "worktree_path": str(worktree)},
        {"session_id": "sess-A", "event": "branch_transition",
         "from_branch": "fix/first", "branch": "fix/continued",
         "attributed": True, "worktree_path": str(worktree)},
    ])
    monkeypatch.setenv("GAIA_SESSION_REGISTRY", str(registry))

    assert branch_drift_message(["sess-A"], "fix/continued", worktree) is None


def test_unattributed_branch_change_remains_sibling_drift(tmp_path, monkeypatch):
    registry = tmp_path / "events.jsonl"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_events(registry, [
        {"session_id": "sess-A", "event": "started", "branch": "fix/first",
         "worktree_path": str(worktree)},
        {"session_id": "sess-A", "event": "heartbeat", "branch": "fix/sibling",
         "worktree_path": str(worktree)},
    ])
    monkeypatch.setenv("GAIA_SESSION_REGISTRY", str(registry))

    assert branch_drift_message(["sess-A"], "fix/sibling", worktree) is not None


def test_attributed_transition_in_other_worktree_does_not_rebind(tmp_path, monkeypatch):
    registry = tmp_path / "events.jsonl"
    worktree = tmp_path / "wt"
    other = tmp_path / "other"
    worktree.mkdir()
    other.mkdir()
    _write_events(registry, [
        {"session_id": "sess-A", "event": "started", "branch": "fix/first",
         "worktree_path": str(worktree)},
        {"session_id": "sess-A", "event": "branch_transition",
         "from_branch": "fix/first", "branch": "fix/sibling",
         "attributed": True, "worktree_path": str(other)},
    ])
    monkeypatch.setenv("GAIA_SESSION_REGISTRY", str(registry))

    assert branch_drift_message(["sess-A"], "fix/sibling", worktree) is not None


def test_no_drift_when_session_started_on_primary_branch(tmp_path, monkeypatch):
    registry = tmp_path / "events.jsonl"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_events(
        registry,
        [
            {
                "session_id": "sess-A",
                "event": "started",
                "timestamp": "2026-06-06T07:00:00Z",
                "branch": "main",
                "worktree_path": str(worktree),
            }
        ],
    )
    monkeypatch.setenv("GAIA_SESSION_REGISTRY", str(registry))

    assert branch_drift_message(["sess-A"], "fix/real-work", worktree) is None


def test_no_drift_when_started_in_different_worktree(tmp_path, monkeypatch):
    registry = tmp_path / "events.jsonl"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    other = tmp_path / "other-wt"
    other.mkdir()
    _write_events(
        registry,
        [
            {
                "session_id": "sess-A",
                "event": "started",
                "timestamp": "2026-06-06T07:00:00Z",
                "branch": "fix/original-owner",
                "worktree_path": str(other),
            }
        ],
    )
    monkeypatch.setenv("GAIA_SESSION_REGISTRY", str(registry))

    assert branch_drift_message(["sess-A"], "fix/foreign-sibling", worktree) is None


def test_drift_uses_most_recent_started_event_branch(tmp_path, monkeypatch):
    """A later `started` (session-id reuse across relaunches) wins; a heartbeat
    on a foreign branch must NOT be treated as the started branch."""
    registry = tmp_path / "events.jsonl"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_events(
        registry,
        [
            {
                "session_id": "sess-A",
                "event": "started",
                "timestamp": "2026-06-06T07:00:00Z",
                "branch": "fix/original-owner",
                "worktree_path": str(worktree),
            },
            {
                "session_id": "sess-A",
                "event": "heartbeat",
                "timestamp": "2026-06-06T07:05:00Z",
                "branch": "fix/foreign-sibling",
                "worktree_path": str(worktree),
            },
        ],
    )
    monkeypatch.setenv("GAIA_SESSION_REGISTRY", str(registry))

    # Current branch is the heartbeat branch, but the started branch differs.
    msg = branch_drift_message(["sess-A"], "fix/foreign-sibling", worktree)
    assert msg is not None
    assert "fix/original-owner" in msg


def test_drift_detected_when_older_candidate_drifts_under_benign_newer(tmp_path, monkeypatch):
    """With multiple candidate ids, a benign newest `started` must not mask an
    older candidate whose `started` recorded the original non-primary branch."""
    registry = tmp_path / "events.jsonl"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_events(
        registry,
        [
            # Launcher session: started on the original owner branch (drift).
            {
                "session_id": "launcher-session",
                "event": "started",
                "timestamp": "2026-06-06T07:00:00Z",
                "branch": "fix/original-owner",
                "worktree_path": str(worktree),
            },
            # Payload session: started later, already on the current branch (benign).
            {
                "session_id": "payload-session",
                "event": "started",
                "timestamp": "2026-06-06T07:05:00Z",
                "branch": "fix/foreign-sibling",
                "worktree_path": str(worktree),
            },
        ],
    )
    monkeypatch.setenv("GAIA_SESSION_REGISTRY", str(registry))

    msg = branch_drift_message(
        ["payload-session", "launcher-session"], "fix/foreign-sibling", worktree
    )
    assert msg is not None
    assert "fix/original-owner" in msg


def test_no_session_ids_returns_none(tmp_path, monkeypatch):
    registry = tmp_path / "events.jsonl"
    monkeypatch.setenv("GAIA_SESSION_REGISTRY", str(registry))
    assert branch_drift_message([], "fix/foreign-sibling", tmp_path) is None
    assert branch_drift_message(["", "  "], "fix/foreign-sibling", tmp_path) is None


def test_empty_current_branch_returns_none(tmp_path, monkeypatch):
    registry = tmp_path / "events.jsonl"
    monkeypatch.setenv("GAIA_SESSION_REGISTRY", str(registry))
    assert branch_drift_message(["sess-A"], "", tmp_path) is None


def test_no_started_event_for_candidate_returns_none(tmp_path, monkeypatch):
    registry = tmp_path / "events.jsonl"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_events(
        registry,
        [
            {
                "session_id": "sess-OTHER",
                "event": "started",
                "timestamp": "2026-06-06T07:00:00Z",
                "branch": "fix/original-owner",
                "worktree_path": str(worktree),
            }
        ],
    )
    monkeypatch.setenv("GAIA_SESSION_REGISTRY", str(registry))

    assert branch_drift_message(["sess-A"], "fix/foreign-sibling", worktree) is None


def test_multiple_candidate_ids_matches_launcher_session(tmp_path, monkeypatch):
    registry = tmp_path / "events.jsonl"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _write_events(
        registry,
        [
            {
                "session_id": "launcher-session",
                "event": "started",
                "timestamp": "2026-06-06T07:00:00Z",
                "branch": "fix/original-owner",
                "worktree_path": str(worktree),
            }
        ],
    )
    monkeypatch.setenv("GAIA_SESSION_REGISTRY", str(registry))

    # Conversation session id has no event; launcher session id does.
    msg = branch_drift_message(
        ["conversation-session", "launcher-session"], "fix/foreign-sibling", worktree
    )
    assert msg is not None
    assert "fix/original-owner" in msg
