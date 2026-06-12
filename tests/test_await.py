"""Tests for the `await` subcommand (BOU-1632).

The await subcommand is a background feedback waiter that:
- Polls owned worktrees for pending work every --interval seconds
- Stamps the ownership heartbeat each tick (keeps detached loop deferring)
- Exits 10 (printing the stop-block) when pending work arrives
- Exits 0 when the owner pid is dead, no owned PRs, or max-wait expires
- Exits 3 when a live same-session pidfile already exists
- Writes/removes pr-watch.await.pid on start/exit
"""
from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest

from agentic_pr_dash import config, maintenance_check as mc


SID = "sess-await-test"


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    """Disable stop-interval rate-limiting so tests run without waiting."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", "/tmp/test-ledger-await-UNUSED")
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _await_pidfile_path(cwd: str, session_id: str = SID) -> Path:
    return Path(mc._await_pidfile(cwd, session_id))


def _write_pidfile(cwd: str, pid: int, session_id: str) -> None:
    path = _await_pidfile_path(cwd, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "session_id": session_id}), encoding="utf-8")


def test_await_exits_0_when_owner_pid_dead(tmp_path, monkeypatch, capsys):
    """When --owner-pid is a dead pid, await exits 0 immediately."""
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [])

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "99999",
        "--max-wait", "1",
    ])
    assert rc == 0
    # pidfile should be removed on exit
    assert not _await_pidfile_path(str(tmp_path)).exists()


def test_await_exits_10_with_prompt_when_work_found(tmp_path, monkeypatch, capsys):
    """When check finds pending work (code 10), await prints the stop-block and exits 10."""
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        mc, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(tmp_path / "worktree")]
    )
    monkeypatch.setattr(mc, "_detached_pr_records", lambda sid, cwd: [])

    wt = tmp_path / "worktree"
    wt.mkdir()

    def _fake_check(path, session_id, *, claim=True):
        return 10, f"pending work\nPR_NUMBER=5"

    monkeypatch.setattr(mc, "_check_worktree", _fake_check)

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "12345",
        "--max-wait", "1",
        "--interval", "1",
    ])
    out = capsys.readouterr().out
    assert rc == 10
    assert "PR_NUMBER=5" in out or "pending work" in out
    assert "pr-watch" in out.lower() or "PR" in out
    # pidfile removed on exit
    assert not _await_pidfile_path(str(tmp_path)).exists()


def test_await_exits_0_when_no_owned_open_prs(tmp_path, monkeypatch, capsys):
    """When there are no owned worktrees and no detached PR records, exits 0."""
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [])
    monkeypatch.setattr(mc, "_detached_pr_records", lambda sid, cwd: [])

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "12345",
        "--max-wait", "1",
    ])
    assert rc == 0
    assert not _await_pidfile_path(str(tmp_path)).exists()


def test_await_single_instance_exit_3(tmp_path, monkeypatch, capsys):
    """When a live pidfile with the same session_id exists, exits 3 without touching it."""
    live_pid = os.getpid()  # our own pid is definitely alive
    _write_pidfile(str(tmp_path), live_pid, SID)

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: pid == str(live_pid) or pid == live_pid)

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "12345",
        "--max-wait", "1",
    ])
    assert rc == 3
    # Original pidfile should still exist (we didn't overwrite it)
    assert _await_pidfile_path(str(tmp_path)).exists()
    data = json.loads(_await_pidfile_path(str(tmp_path)).read_text())
    assert data["pid"] == live_pid
    assert data["session_id"] == SID


def test_await_stale_pidfile_not_exit_3(tmp_path, monkeypatch, capsys):
    """A stale pidfile (dead pid) does NOT trigger exit 3; we proceed normally."""
    dead_pid = 99999
    _write_pidfile(str(tmp_path), dead_pid, SID)

    calls = []
    def _fake_pid_alive(pid):
        calls.append(pid)
        # owner-pid check: 12345 is the owner, declared alive
        # stale pidfile check: 99999 is dead
        if str(pid) == str(dead_pid) or pid == dead_pid:
            return False
        return True

    monkeypatch.setattr(mc, "_pid_alive", _fake_pid_alive)
    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [])
    monkeypatch.setattr(mc, "_detached_pr_records", lambda sid, cwd: [])

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "12345",
        "--max-wait", "1",
    ])
    # Should NOT be 3 (stale pidfile is ignored)
    assert rc == 0


def test_await_max_wait_expiry_exit_0(tmp_path, monkeypatch, capsys):
    """When --max-wait 0 and nothing pending, exits 0."""
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(tmp_path)])
    monkeypatch.setattr(mc, "_detached_pr_records", lambda sid, cwd: [])

    def _clean_check(path, session_id, *, claim=True):
        return 0, "nothing pending"

    monkeypatch.setattr(mc, "_check_worktree", _clean_check)
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda cwd, sid, work: None)

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "12345",
        "--max-wait", "0",
        "--interval", "1",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    # Should print a note about max-wait
    assert "max-wait" in out.lower() or "re-arm" in out.lower()


def test_await_stamps_heartbeat_each_tick(tmp_path, monkeypatch):
    """Each tick stamps the ownership heartbeat for all owned worktrees."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    mc._write_arm_marker(str(wt), SID, os.getpid(), 42)

    heartbeat_calls = []

    def _fake_heartbeat(cwd, sid, work):
        heartbeat_calls.append((cwd, sid, work))

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        mc, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(wt)]
    )
    monkeypatch.setattr(mc, "_detached_pr_records", lambda sid, cwd: [])
    monkeypatch.setattr(mc, "_check_worktree", lambda path, sid, *, claim=True: (0, "clean"))
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", _fake_heartbeat)

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "12345",
        "--max-wait", "0",
        "--interval", "1",
    ])
    assert rc == 0
    # Heartbeat should have been stamped for our worktree
    assert any(str(wt) in str(c[0]) for c in heartbeat_calls)


def test_await_pidfile_written_and_removed(tmp_path, monkeypatch):
    """Pidfile is written on entry and removed on clean exit."""
    pidfile_path = _await_pidfile_path(str(tmp_path))
    written_pids = []

    original_collect = mc._collect_stop_gate_worktrees

    def _collect_and_check_pidfile(sid, cwd):
        # At this point the pidfile should exist
        if pidfile_path.exists():
            written_pids.append(json.loads(pidfile_path.read_text())["pid"])
        return []

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", _collect_and_check_pidfile)
    monkeypatch.setattr(mc, "_detached_pr_records", lambda sid, cwd: [])

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "12345",
        "--max-wait", "0",
        "--interval", "1",
    ])
    assert rc == 0
    assert written_pids  # pidfile was present during the tick
    assert not pidfile_path.exists()  # cleaned up on exit
