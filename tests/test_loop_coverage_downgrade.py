"""Tests for loop-coverage downgrade when executor streak >= threshold (BOU-1789 Task 6).

_loop_covers_pr(cwd, pr) is True only when:
  - the detached loop is alive, AND
  - the executor-failure streak for that PR < threshold

When streak >= threshold, the loop no longer covers the PR, so the stop-gate
must force a waiter (exit 2).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentic_pr_dash import config, loop, maintenance_check as mc
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import stop_gate as _stop_gate_mod
from agentic_pr_dash._maintenance import waiter as _waiter_mod
from agentic_pr_dash._maintenance import worktree_check as _worktree_check_mod

SID = "sess-coverage-downgrade"


@pytest.fixture(autouse=True)
def _isolation(monkeypatch, tmp_path):
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(tmp_path / "daemons"))
    monkeypatch.setenv("GAIA_MAINTENANCE_LOOP_MACHINE_WIDE", "true")
    monkeypatch.setenv("AGENTIC_PR_DASH_ESCALATION_THRESHOLD", "3")
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _make_armed_worktree(tmp_path: Path, session_id: str, pr_number: int) -> Path:
    wt = tmp_path / "worktree"
    wt.mkdir(exist_ok=True)
    mc._write_arm_marker(str(wt), session_id, os.getpid(), pr_number)
    return wt


def _write_live_pidfile(tmp_path: Path) -> None:
    """Write a pidfile with our own PID (always alive)."""
    daemon_dir = tmp_path / "daemons"
    daemon_dir.mkdir(parents=True, exist_ok=True)
    (daemon_dir / "pr-maintenance-loop.pid").write_text(str(os.getpid()), encoding="utf-8")


def test_loop_covers_pr_true_when_alive_and_no_streak(tmp_path, monkeypatch):
    """_loop_covers_pr returns True when loop is alive and streak == 0."""
    cwd = str(tmp_path)
    _write_live_pidfile(tmp_path)
    # No streak recorded
    assert loop._loop_covers_pr(cwd, 42) is True


def test_loop_covers_pr_false_when_streak_at_threshold(tmp_path, monkeypatch):
    """_loop_covers_pr returns False when streak >= threshold."""
    cwd = str(tmp_path)
    _write_live_pidfile(tmp_path)
    # Simulate streak at threshold (3)
    monkeypatch.setattr(loop, "executor_failure_streak", lambda cwd, pr: 3)
    assert loop._loop_covers_pr(cwd, 42) is False


def test_loop_covers_pr_false_when_loop_dead(tmp_path, monkeypatch):
    """_loop_covers_pr returns False when loop is dead (regardless of streak)."""
    cwd = str(tmp_path)
    # No pidfile = loop dead
    monkeypatch.setattr(loop, "executor_failure_streak", lambda cwd, pr: 0)
    assert loop._loop_covers_pr(cwd, 42) is False


def test_stop_gate_loop_alive_streak_at_threshold_forces_waiter(tmp_path, monkeypatch, capsys):
    """Loop alive but streak >= threshold → loop does not cover PR → stop-gate returns 2."""
    wt = _make_armed_worktree(tmp_path, SID, 42)
    _write_live_pidfile(tmp_path)

    monkeypatch.setattr(_worktree_check_mod, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records",
                        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)
    # Loop is alive but streak at threshold → not covering
    monkeypatch.setattr(loop, "executor_failure_streak", lambda cwd, pr: 3)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2, f"Expected 2 (loop not covering due to streak), got {rc}; stderr={err!r}"


def test_stop_gate_loop_alive_zero_streak_returns_0(tmp_path, monkeypatch, capsys):
    """Loop alive and streak == 0 → loop covers PR → stop-gate returns 0."""
    wt = _make_armed_worktree(tmp_path, SID, 42)
    _write_live_pidfile(tmp_path)

    monkeypatch.setattr(_worktree_check_mod, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records",
                        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)
    # Loop covers PR (streak == 0)
    monkeypatch.setattr(loop, "executor_failure_streak", lambda cwd, pr: 0)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert rc == 0, f"Expected 0 (loop covers PR), got {rc}"
