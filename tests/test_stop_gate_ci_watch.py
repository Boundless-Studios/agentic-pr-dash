"""Tests for stop-gate coverage of watch-pending PRs (BOU-1789 Task 4).

When a non-draft PR is watch-pending (required CI still running) with no
covering loop and no live waiter, the stop-gate must return 2 (spawn waiter).
Draft PRs are excluded. A clean/passing PR with no other work returns 0.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentic_pr_dash import config, maintenance_check as mc
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import stop_gate as _stop_gate_mod
from agentic_pr_dash._maintenance import waiter as _waiter_mod
from agentic_pr_dash._maintenance import worktree_check as _worktree_check_mod

SID = "sess-sg-ci-watch"


@pytest.fixture(autouse=True)
def _isolation(monkeypatch, tmp_path):
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(tmp_path / "empty-daemons"))
    monkeypatch.setenv("GAIA_MAINTENANCE_LOOP_MACHINE_WIDE", "true")
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _make_armed_worktree(tmp_path: Path, session_id: str, pr_number: int) -> Path:
    wt = tmp_path / "worktree"
    wt.mkdir(exist_ok=True)
    mc._write_arm_marker(str(wt), session_id, os.getpid(), pr_number)
    return wt


def test_stop_gate_watch_pending_pr_no_coverage_returns_2(tmp_path, monkeypatch, capsys):
    """Non-draft watch-pending PR + no loop + no live waiter → stop-gate returns 2."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    # No actionable blockers (nothing to fix)
    monkeypatch.setattr(_worktree_check_mod, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records",
                        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    # PR 42 is open and watch-pending
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)
    # No loop alive
    monkeypatch.setattr(_waiter_mod, "_detached_loop_alive", lambda cwd: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2, f"Expected 2, got {rc}; stderr={err!r}"
    assert "42" in err or "waiter" in err.lower() or "await" in err.lower()


def test_stop_gate_watch_pending_with_live_waiter_returns_0(tmp_path, monkeypatch, capsys):
    """Non-draft watch-pending PR with a live waiter → stop-gate returns 0."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    monkeypatch.setattr(_worktree_check_mod, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records",
                        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    # Waiter is alive
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: True)
    monkeypatch.setattr(_waiter_mod, "_detached_loop_alive", lambda cwd: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert rc == 0, f"Expected 0 (waiter alive), got {rc}"


def test_stop_gate_clean_pr_no_watch_pending_returns_0(tmp_path, monkeypatch, capsys):
    """A clean/passing PR with no watch-pending state returns 0."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    monkeypatch.setattr(_worktree_check_mod, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records",
                        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    # No open PRs at all
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: set())
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)
    monkeypatch.setattr(_waiter_mod, "_detached_loop_alive", lambda cwd: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert rc == 0, f"Expected 0 (no open PRs), got {rc}"
