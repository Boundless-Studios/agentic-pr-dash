"""Tests for await waiter staying alive past max-wait when CI is watch-pending (BOU-1789 Task 3)."""
from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

from agentic_pr_dash import config, maintenance_check as mc
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod

SID = "sess-await-ci-watch"


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", "/tmp/test-ledger-await-ci-UNUSED")
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def test_await_stays_alive_past_max_wait_while_watch_pending(tmp_path, monkeypatch, capsys):
    """When max-wait expires but a non-draft PR is still watch-pending, the waiter
    must NOT return 0 — it must stay alive and poll again.

    Tick 1: check returns (0, nothing pending), _collect_await_watch_pending=True
            deadline has passed → stays alive because watch-pending
    Tick 2: check returns (10, CI failed) → returns 10
    """
    tick_count = [0]
    watch_pending_calls = [0]

    wt = tmp_path / "worktree"
    wt.mkdir()

    def fake_check(path, session_id, *, claim=True):
        tick_count[0] += 1
        if tick_count[0] == 1:
            return 0, "nothing pending"
        return 10, "CI failed\nPR_NUMBER=5"

    def fake_collect_watch_pending(owned, cwd, session_id):
        watch_pending_calls[0] += 1
        # First check: watch-pending (CI still running)
        return watch_pending_calls[0] == 1

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    # Patch _collect_stop_gate_worktrees in the worktrees module — this is what
    # _owned_worktrees_across_roots calls internally, so the await picks it up.
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees",
                        lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records",
                        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(mc, "_check_worktree", fake_check)
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    # Patch _collect_await_watch_pending in the mc namespace — that's where _cmd_await looks
    monkeypatch.setattr(mc, "_collect_await_watch_pending", fake_collect_watch_pending)

    # Use a tiny sleep override so the test is fast
    sleep_calls = [0]
    original_sleep = __import__("time").sleep
    monkeypatch.setattr("time.sleep", lambda s: None)  # no-op sleep

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", str(os.getpid()),
        "--max-wait", "0",  # deadline is immediately now
        "--interval", "0",
    ])
    assert rc == 10, f"Expected 10 (CI failure woke waiter), got {rc}"
    assert tick_count[0] >= 2, "Should have polled at least twice"


def test_await_returns_0_when_max_wait_expires_and_no_watch_pending(tmp_path, monkeypatch, capsys):
    """When max-wait expires and no PR is watch-pending, return 0 as before."""
    wt = tmp_path / "worktree"
    wt.mkdir()

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees",
                        lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records",
                        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(mc, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    # No watch-pending PRs
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)
    monkeypatch.setattr("time.sleep", lambda s: None)

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", str(os.getpid()),
        "--max-wait", "0",
        "--interval", "0",
    ])
    assert rc == 0, f"Expected 0 (max-wait expired, nothing pending), got {rc}"
