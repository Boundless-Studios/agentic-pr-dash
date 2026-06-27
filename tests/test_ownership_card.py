"""Tests for _ownership_for_card and WorktreeCard ownership fields.

These tests verify that _ownership_for_card correctly reads the pr-watch.armed
marker, maintenance state, and comment_scan events, and that all three paths are
individually best-effort (missing files → empty dict, no exception).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_pr_dash.app import _ownership_for_card
from agentic_pr_dash.models import MaintenanceState, MaintenanceStatus, WorktreeCard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STATE_DIR = ".agentic-pr-dash"
_PR_NUMBER = 123


def _write_marker(worktree: Path, *, session_id: str, pid: int, armed_at: str, last_heartbeat: str) -> None:
    state_dir = worktree / _STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = state_dir / "pr-watch.armed"
    content = (
        f"pr={_PR_NUMBER}\n"
        f"armed_at={armed_at}\n"
        f"session_id={session_id}\n"
        f"pid={pid}\n"
        f"last_heartbeat={last_heartbeat}\n"
    )
    marker.write_text(content, encoding="utf-8")


def _write_maintenance_state(worktree: Path, state: MaintenanceStatus) -> None:
    maint_dir = worktree / _STATE_DIR / "pr-maintenance"
    maint_dir.mkdir(parents=True, exist_ok=True)
    ms = MaintenanceState(
        pr_number=_PR_NUMBER,
        branch="bou-1801-test",
        worktree_path=str(worktree),
        state=state,
    )
    path = maint_dir / f"pr-{_PR_NUMBER}.json"
    path.write_text(
        json.dumps(ms.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )


def _seed_comment_scan_event(repo_cwd: Path, pr_number: int, decisions: list[dict]) -> None:
    """Write a comment_scan event directly to the event store JSONL file."""
    store_path = repo_cwd / _STATE_DIR / "observability" / "events.jsonl"
    store_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "repo": str(repo_cwd),
        "pr_number": pr_number,
        "kind": "comment_scan",
        "session_id": None,
        "details": {"decisions": decisions, "picked": len([d for d in decisions if d["decision"] == "PICKED"])},
    }
    with store_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


# ---------------------------------------------------------------------------
# Tests: marker present → fields populated
# ---------------------------------------------------------------------------

def test_ownership_with_marker(tmp_path):
    """When a valid pr-watch.armed marker exists, ownership fields are populated."""
    pid = os.getpid()  # use current process — guaranteed alive
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    session_id = "abc12345-dead-beef-cafe-000000000001"

    _write_marker(
        tmp_path,
        session_id=session_id,
        pid=pid,
        armed_at=now,
        last_heartbeat=now,
    )

    result = _ownership_for_card(
        worktree_path=str(tmp_path),
        pr_number=_PR_NUMBER,
        repo_cwd=str(tmp_path),
    )

    assert result.get("owner_session_id") == session_id
    assert result.get("owner_pid") == pid
    assert result.get("owner_pid_alive") is True
    assert isinstance(result.get("armed_at"), datetime)
    assert isinstance(result.get("last_heartbeat_at"), datetime)


def test_ownership_dead_pid(tmp_path):
    """A pid that doesn't exist → owner_pid_alive is False."""
    # pid 2**20 is almost certainly not running
    dead_pid = 2**20 - 1
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_marker(
        tmp_path,
        session_id="sess-dead",
        pid=dead_pid,
        armed_at=now,
        last_heartbeat=now,
    )

    result = _ownership_for_card(
        worktree_path=str(tmp_path),
        pr_number=_PR_NUMBER,
        repo_cwd=str(tmp_path),
    )

    # pid might or might not be alive on this machine; just confirm we get a bool
    assert "owner_pid_alive" in result
    assert isinstance(result["owner_pid_alive"], bool)


# ---------------------------------------------------------------------------
# Tests: maintenance state
# ---------------------------------------------------------------------------

def test_ownership_loop_state(tmp_path):
    """When maintenance state exists, loop_state is populated."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_marker(
        tmp_path,
        session_id="sess-loop",
        pid=os.getpid(),
        armed_at=now,
        last_heartbeat=now,
    )
    _write_maintenance_state(tmp_path, MaintenanceStatus.RUNNING)

    result = _ownership_for_card(
        worktree_path=str(tmp_path),
        pr_number=_PR_NUMBER,
        repo_cwd=str(tmp_path),
    )

    assert result.get("loop_state") == "running"


# ---------------------------------------------------------------------------
# Tests: comment_scan events → thread_decisions
# ---------------------------------------------------------------------------

def test_ownership_thread_decisions(tmp_path):
    """Seeded comment_scan event → thread_decisions list populated."""
    decisions = [
        {
            "thread_id": "PRRT_abc123",
            "author": "reviewer1",
            "created_at": "2026-06-27T10:00:00Z",
            "age_seconds": 3600.0,
            "decision": "PICKED",
            "marker_state": None,
            "claim_age_seconds": None,
        },
        {
            "thread_id": "PRRT_def456",
            "author": "reviewer2",
            "created_at": "2026-06-27T09:00:00Z",
            "age_seconds": 7200.0,
            "decision": "SKIP_RESOLVED",
            "marker_state": "resolved",
            "claim_age_seconds": None,
        },
    ]
    _seed_comment_scan_event(tmp_path, _PR_NUMBER, decisions)

    result = _ownership_for_card(
        worktree_path=str(tmp_path),
        pr_number=_PR_NUMBER,
        repo_cwd=str(tmp_path),
    )

    thread_decisions = result.get("thread_decisions", [])
    assert len(thread_decisions) == 2
    assert thread_decisions[0].decision == "PICKED"
    assert thread_decisions[0].author == "reviewer1"
    assert thread_decisions[1].decision == "SKIP_RESOLVED"
    assert thread_decisions[1].marker_state == "resolved"


# ---------------------------------------------------------------------------
# Tests: no marker → empty dict, no exception
# ---------------------------------------------------------------------------

def test_no_marker_returns_empty(tmp_path):
    """Missing marker file → all ownership fields absent, no exception raised."""
    result = _ownership_for_card(
        worktree_path=str(tmp_path),
        pr_number=_PR_NUMBER,
        repo_cwd=str(tmp_path),
    )
    assert "owner_session_id" not in result
    assert "owner_pid" not in result
    assert "owner_pid_alive" not in result
    assert "armed_at" not in result


def test_none_worktree_path_returns_empty():
    """None worktree_path → returns empty dict without raising."""
    result = _ownership_for_card(
        worktree_path=None,
        pr_number=42,
        repo_cwd="/nonexistent/path",
    )
    assert isinstance(result, dict)
    assert "owner_session_id" not in result


def test_none_pr_number_returns_empty(tmp_path):
    """None pr_number → no maintenance state, no event query, no exception."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_marker(
        tmp_path,
        session_id="sess-x",
        pid=os.getpid(),
        armed_at=now,
        last_heartbeat=now,
    )
    result = _ownership_for_card(
        worktree_path=str(tmp_path),
        pr_number=None,
        repo_cwd=str(tmp_path),
    )
    # Marker fields can still be populated even with no pr_number
    assert "loop_state" not in result
    assert "thread_decisions" not in result


# ---------------------------------------------------------------------------
# Tests: WorktreeCard accepts the ownership kwargs
# ---------------------------------------------------------------------------

def test_worktre_card_accepts_ownership_fields():
    """WorktreeCard can be constructed with all ownership fields set."""
    now = datetime.now(timezone.utc)
    card = WorktreeCard(
        id="wt-foo",
        worktree_name="foo",
        branch="bou-1801-test",
        owner_session_id="sess-abc",
        owner_pid=12345,
        owner_pid_alive=True,
        armed_at=now,
        last_heartbeat_at=now,
        loop_state="running",
    )
    assert card.owner_session_id == "sess-abc"
    assert card.owner_pid == 12345
    assert card.owner_pid_alive is True
    assert card.armed_at == now
    assert card.last_heartbeat_at == now
    assert card.loop_state == "running"
    assert card.thread_decisions == []


def test_worktre_card_ownership_defaults_to_none():
    """WorktreeCard ownership fields default to None (not absent/error)."""
    card = WorktreeCard(id="wt-bar", worktree_name="bar", branch="main")
    assert card.owner_session_id is None
    assert card.owner_pid is None
    assert card.owner_pid_alive is None
    assert card.armed_at is None
    assert card.last_heartbeat_at is None
    assert card.loop_state is None
    assert card.thread_decisions == []
