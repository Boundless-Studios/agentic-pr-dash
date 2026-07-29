"""Regression tests for BOU-1632 Codex P2 review findings.

Finding 2: Waiter pidfile collision across sessions sharing a cwd.
Finding 3: Detached PRs excluded from waiter enforcement.
Finding 4: _resolve_owner_pid only recognizes claude ancestors, not codex.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentic_pr_dash import config, maintenance_check as mc, session_ledger as sl
from agentic_pr_dash._maintenance import waiter as _waiter_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    config.load.cache_clear()
    yield
    config.load.cache_clear()


# ---------------------------------------------------------------------------
# Finding 2: per-session pidfile
# ---------------------------------------------------------------------------

def test_two_sessions_get_different_pidfile_paths(tmp_path):
    """Each session-id maps to a distinct pidfile path."""
    path_a = mc._await_pidfile(str(tmp_path), "session-alpha")
    path_b = mc._await_pidfile(str(tmp_path), "session-beta")
    assert path_a != path_b
    assert "session-alpha" in path_a or "alpha" in path_a
    assert "session-beta" in path_b or "beta" in path_b


def test_two_session_pidfiles_coexist(tmp_path):
    """Writing a pidfile for session B does not touch session A's pidfile."""
    sid_a = "sess-coexist-A"
    sid_b = "sess-coexist-B"

    mc._write_await_pidfile(str(tmp_path), {"pid": 1001, "session_id": sid_a}, sid_a)
    mc._write_await_pidfile(str(tmp_path), {"pid": 1002, "session_id": sid_b}, sid_b)

    data_a = mc._read_await_pidfile(str(tmp_path), sid_a)
    data_b = mc._read_await_pidfile(str(tmp_path), sid_b)

    assert data_a["pid"] == 1001
    assert data_b["pid"] == 1002


def test_removing_session_b_pidfile_leaves_session_a_intact(tmp_path):
    """Removing B's pidfile does not affect A's."""
    sid_a = "sess-remove-A"
    sid_b = "sess-remove-B"

    mc._write_await_pidfile(str(tmp_path), {"pid": 2001, "session_id": sid_a}, sid_a)
    mc._write_await_pidfile(str(tmp_path), {"pid": 2002, "session_id": sid_b}, sid_b)

    mc._remove_await_pidfile(str(tmp_path), sid_b)

    # A's file must still exist and contain A's data
    data_a = mc._read_await_pidfile(str(tmp_path), sid_a)
    assert data_a["pid"] == 2001

    # B's file must be gone
    data_b = mc._read_await_pidfile(str(tmp_path), sid_b)
    assert data_b == {}


def test_await_alive_session_a_not_affected_by_session_b_waiter(tmp_path, monkeypatch):
    """stop-gate for session A sees A's waiter alive regardless of B."""
    sid_a = "sess-alive-A"
    sid_b = "sess-alive-B"

    mc._write_await_pidfile(
        str(tmp_path),
        {"pid": 3001, "session_id": sid_a, "process_identity": "start-a"},
        sid_a,
    )
    mc._write_await_pidfile(
        str(tmp_path),
        {"pid": 3002, "session_id": sid_b, "process_identity": "start-b"},
        sid_b,
    )

    # Both pids alive
    monkeypatch.setattr(_waiter_mod, "_pid_alive", lambda p: True)
    monkeypatch.setattr(
        _waiter_mod,
        "_process_identity",
        lambda p: {"3001": "start-a", "3002": "start-b"}[str(p)],
    )

    assert mc._await_alive(str(tmp_path), sid_a) is True
    assert mc._await_alive(str(tmp_path), sid_b) is True

    # Remove B's pidfile (B's waiter exits)
    mc._remove_await_pidfile(str(tmp_path), sid_b)
    assert mc._await_alive(str(tmp_path), sid_a) is True  # A still alive
    assert mc._await_alive(str(tmp_path), sid_b) is False  # B gone


# ---------------------------------------------------------------------------
# Finding 3: Detached PRs must trigger waiter enforcement
# ---------------------------------------------------------------------------

def test_stop_gate_demands_waiter_for_detached_open_pr(tmp_path, monkeypatch, capsys):
    """Session has NO live worktrees but owns an open detached ledger PR.
    stop-gate must demand a waiter (exit 2) rather than exit 0.
    """
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    sl.append("sess-detached-waiter", pr=201, branch="bou-201",
               worktree=str(tmp_path / "gone"))

    # No live worktrees for this session
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [])
    # Detached record: PR 201 is open, no current blockers
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [{
            "pr": 201, "url": "https://x/pull/201", "branch": "bou-201",
            "worktree_present": False, "unresolved_threads": 0,
            "ci_failing": False, "failing_checks": [],
            "changes_requested": False, "merge_conflict": False,
            "review_decision": "", "merge_state": "", "mergeable": "",
            "p1": False, "state": "open",
        }]
    )
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)

    rc = mc.main([
        "stop-gate",
        "--cwd", str(tmp_path),
        "--session-id", "sess-detached-waiter",
    ])
    err = capsys.readouterr().err
    assert rc == 2, "stop-gate must demand a waiter when detached open PR and no waiter"
    assert "waiter" in err.lower() or "await" in err.lower()
    assert "201" in err


def test_await_keeps_ticking_when_owned_empty_but_detached_prs_exist(
    tmp_path, monkeypatch, capsys
):
    """await must NOT exit 0 when owned=[] but the session has open detached PRs
    that still have something to watch. It should continue ticking until
    max-wait or work is found.

    Since BOU-1962 a detached PR that is fully CLEAN (no blockers AND required
    CI terminal) is a legitimate exit-0, so tick 1 marks the record
    watch-pending (``ci_watch_pending``) — the codex-P2 concern this test
    guards is that the empty-`owned` early exit must not drop coverage of a
    detached PR that is still being watched.
    """
    # First tick: no owned worktrees, but a watch-pending detached record
    # → should NOT exit 0
    # Second tick: detached record has a blocker → exits 10

    tick_count = [0]

    def fake_pid_alive(pid):
        return True

    def fake_collect_worktrees(sid, cwd):
        return []  # no live worktrees

    def fake_detached(sid, cwd, include_legacy=True, prune_legacy=True):
        tick_count[0] += 1
        if tick_count[0] == 1:
            # First tick: open, no blockers, required CI still running
            return [{
                "pr": 202, "url": "https://x/pull/202", "branch": "bou-202",
                "worktree_present": False, "unresolved_threads": 0,
                "ci_failing": False, "failing_checks": [],
                "changes_requested": False, "merge_conflict": False,
                "review_decision": "", "merge_state": "", "mergeable": "",
                "p1": False, "state": "open", "ci_watch_pending": True,
            }]
        # Second tick: has a blocker
        return [{
            "pr": 202, "url": "https://x/pull/202", "branch": "bou-202",
            "worktree_present": False, "unresolved_threads": 1,
            "ci_failing": False, "failing_checks": [],
            "changes_requested": False, "merge_conflict": False,
            "review_decision": "", "merge_state": "", "mergeable": "",
            "p1": False, "state": "open",
        }]

    monkeypatch.setattr(mc, "_pid_alive", fake_pid_alive)  # for _cmd_await owner-pid check
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", fake_collect_worktrees)
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", fake_detached)
    # Speed up the loop
    monkeypatch.setattr(mc.time, "sleep", lambda s: None)  # type: ignore[attr-defined]

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", "sess-await-detached",
        "--owner-pid", "99999",
        "--interval", "1",
        "--max-wait", "30",
    ])
    assert rc == 10, "await must exit 10 when detached blocker arrives"
    assert tick_count[0] == 2, "await must have ticked twice (not exited on first empty-owned)"


# ---------------------------------------------------------------------------
# Finding 4: _resolve_owner_pid must recognize codex ancestors
# ---------------------------------------------------------------------------

def test_resolve_owner_pid_recognizes_codex_ancestor(monkeypatch):
    """When a codex process is an ancestor, _resolve_owner_pid returns its pid."""
    # pid=999 is the shell (os.getppid()); its parent is pid=50 (codex)
    # ps -o ppid=,comm= -p 999  → "50 bash"   (pid 999 is shell, parent=50)
    # ps -o ppid=,comm= -p 50   → "1 codex"   (pid 50 is codex, parent=1)  ← match
    ps_responses = {
        999: "50 bash",
        50: "1 codex",
    }

    def fake_run(cmd, **kwargs):
        pid = int(cmd[-1])
        import types
        result = types.SimpleNamespace()
        result.stdout = ps_responses.get(pid, "")
        return result

    monkeypatch.setattr(mc.subprocess, "run", fake_run)
    monkeypatch.setattr(mc.os, "getppid", lambda: 999)

    pid = mc._resolve_owner_pid()
    assert pid == 50, f"Expected codex ancestor pid 50, got {pid}"


def test_resolve_owner_pid_still_recognizes_claude_ancestor(monkeypatch):
    """Existing behavior: claude ancestor still recognized after the fix."""
    # ps -o ppid=,comm= -p 999  → "50 claude"  ← match immediately
    ps_responses = {
        999: "50 claude",
    }

    def fake_run(cmd, **kwargs):
        pid = int(cmd[-1])
        import types
        result = types.SimpleNamespace()
        result.stdout = ps_responses.get(pid, "")
        return result

    monkeypatch.setattr(mc.subprocess, "run", fake_run)
    monkeypatch.setattr(mc.os, "getppid", lambda: 999)

    pid = mc._resolve_owner_pid()
    assert pid == 999


def test_resolve_owner_pid_falls_back_to_getppid_when_no_match(monkeypatch):
    """When no claude/codex ancestor found, falls back to os.getppid()."""
    ps_responses = {
        999: "1 bash",
        1: "",
    }

    def fake_run(cmd, **kwargs):
        pid = int(cmd[-1])
        import types
        result = types.SimpleNamespace()
        result.stdout = ps_responses.get(pid, "")
        return result

    monkeypatch.setattr(mc.subprocess, "run", fake_run)
    monkeypatch.setattr(mc.os, "getppid", lambda: 999)

    pid = mc._resolve_owner_pid()
    assert pid == 999  # falls back to os.getppid()
