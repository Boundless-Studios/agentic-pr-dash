"""BOU-1789 integration guard — CI terminal guarantee.

Drives the REAL _stop_gate_impl and _cmd_await (stubbing only the gh boundary
`required_checks_pending` / `_check_worktree` resolution and process-liveness
— NOT the decision logic) to assert end-to-end:

(a) stop-gate watch-pending + no loop + no waiter → 2
(b) await stays alive past max-wait while watch-pending and returns 10 on
    required-check failure
(c) loop streak>=threshold → PR not loop-covered → stop-gate forces waiter
    even with loop pid alive
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentic_pr_dash import config, loop, maintenance_check as mc
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import stop_gate as _stop_gate_mod
from agentic_pr_dash._maintenance import waiter as _waiter_mod
from agentic_pr_dash._maintenance import worktree_check as _worktree_check_mod

SID = "sess-bou1789-guard"


@pytest.fixture(autouse=True)
def _isolation(monkeypatch, tmp_path):
    """Isolate from real state, rate-limits, and daemons."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(tmp_path / "empty-daemons"))
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


def _write_live_pidfile(tmp_path: Path) -> Path:
    """Write a pidfile with our own PID (always alive)."""
    daemon_dir = tmp_path / "empty-daemons"
    daemon_dir.mkdir(parents=True, exist_ok=True)
    pidfile = daemon_dir / "pr-maintenance-loop.pid"
    pidfile.write_text(str(os.getpid()), encoding="utf-8")
    return pidfile


# ---------------------------------------------------------------------------
# (a) stop-gate: watch-pending PR + no loop + no waiter → 2
# ---------------------------------------------------------------------------

def test_stop_gate_watch_pending_no_coverage_forces_waiter(tmp_path, monkeypatch, capsys):
    """(a) A non-draft PR that is watch-pending with no loop and no live waiter
    must cause the stop-gate to return 2 (spawn waiter prompt)."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    # _check_worktree: no current blockers → code 0 (nothing to fix yet)
    monkeypatch.setattr(_worktree_check_mod, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records",
                        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])

    # PR 42 is open, no loop, no waiter
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)
    # No detached loop
    monkeypatch.setattr(_waiter_mod, "_detached_loop_alive", lambda cwd: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2, f"Expected 2, got {rc}; stderr={err!r}"
    # The spawn prompt should mention PR 42
    assert "42" in err or "waiter" in err.lower() or "await" in err.lower()


# ---------------------------------------------------------------------------
# (b) await stays alive past max-wait while watch-pending, returns 10 on failure
# ---------------------------------------------------------------------------

def test_await_stays_alive_and_wakes_on_ci_failure(tmp_path, monkeypatch, capsys):
    """(b) await stays alive past max-wait while CI is pending and wakes on failure.

    Stubs only the gh boundary (_collect_await_watch_pending and _check_worktree)
    — the await decision logic (_cmd_await) is run real.
    """
    tick_count = [0]
    watch_pending_calls = [0]

    wt = tmp_path / "worktree"
    wt.mkdir()

    def fake_check(path, session_id, *, claim=True):
        tick_count[0] += 1
        if tick_count[0] == 1:
            return 0, "nothing pending"  # CI still running, no blockers
        return 10, "CI failed\nPR_NUMBER=42"

    def fake_collect_watch_pending(owned, cwd, session_id):
        watch_pending_calls[0] += 1
        return watch_pending_calls[0] == 1  # True on first call, False after

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees",
                        lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records",
                        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(mc, "_check_worktree", fake_check)
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(mc, "_collect_await_watch_pending", fake_collect_watch_pending)
    monkeypatch.setattr("time.sleep", lambda s: None)  # no-op sleep

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", str(os.getpid()),
        "--max-wait", "0",
        "--interval", "0",
    ])
    assert rc == 10, f"Expected 10 (CI failure woke waiter), got {rc}"
    assert tick_count[0] >= 2, "Should have polled at least twice"


# ---------------------------------------------------------------------------
# (c) loop streak>=threshold → stop-gate forces waiter even with loop alive
# ---------------------------------------------------------------------------

def test_stop_gate_loop_streak_at_threshold_forces_waiter(tmp_path, monkeypatch, capsys):
    """(c) When the loop is alive but its executor-failure streak >= threshold,
    the loop no longer covers the PR, so the stop-gate must return 2."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    # Write a live loop pidfile
    _write_live_pidfile(tmp_path)

    monkeypatch.setattr(_worktree_check_mod, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records",
                        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)
    # Streak at threshold — loop does NOT cover PR 42
    monkeypatch.setattr(loop, "executor_failure_streak", lambda cwd, pr: 3)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2, (
        f"Expected 2 (loop not covering due to streak), got {rc}; stderr={err!r}"
    )
