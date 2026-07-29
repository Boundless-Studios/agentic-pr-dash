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
from agentic_pr_dash._maintenance import _common as _common_mod
from agentic_pr_dash._maintenance import waiter as _waiter_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import markers as _markers_mod


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


def _bind_pr(monkeypatch, pr: int = 42) -> None:
    """Make every owned worktree resolve to open PR ``pr``.

    The clean verdict is reachable only when the waiter actually watched a PR
    (BOU-2294), so a test whose subject is NOT the empty watch set has to bind
    one. ``_owned_open_pr_pairs`` is the documented seam for this.
    """
    monkeypatch.setattr(mc, "_owned_open_pr_pairs", lambda owned: [(w, pr) for w in owned])
    monkeypatch.setattr(mc, "_marker_pr_still_current", lambda wt, n: True)


def _write_pidfile(
    cwd: str,
    pid: int,
    session_id: str,
    process_identity: str = "Mon Jul 27 12:34:56 2026",
) -> None:
    path = _await_pidfile_path(cwd, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "session_id": session_id,
                "process_identity": process_identity,
            }
        ),
        encoding="utf-8",
    )


def test_await_exits_0_when_owner_pid_dead(tmp_path, monkeypatch, capsys):
    """When --owner-pid is a dead pid, await exits 0 immediately."""
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [])

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
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(tmp_path / "worktree")]
    )
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])

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


def test_await_exits_unbound_when_no_owned_open_prs(tmp_path, monkeypatch, capsys):
    """No owned worktrees and no detached PR records: the waiter bound to NOTHING.

    That is not a clean bill of health — it used to exit 0 next to a "all watched
    PRs are clean" message, which is a wrong answer that looks like a right one
    (BOU-2294). It must be loud and non-zero instead.
    """
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [])
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "12345",
        "--max-wait", "1",
    ])
    captured = capsys.readouterr()
    assert rc == mc._AWAIT_UNBOUND
    assert rc != 0 and rc != 10
    assert "clean" not in captured.out.lower()
    assert '"outcome":"unbound"' in captured.err
    assert not _await_pidfile_path(str(tmp_path)).exists()


def test_await_single_instance_exit_3(tmp_path, monkeypatch, capsys):
    """When a live pidfile with the same session_id exists, exits 3 without touching it."""
    live_pid = os.getpid()  # our own pid is definitely alive
    _write_pidfile(str(tmp_path), live_pid, SID)

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: pid == str(live_pid) or pid == live_pid)
    monkeypatch.setattr(
        _waiter_mod,
        "_process_identity",
        lambda pid: "Mon Jul 27 12:34:56 2026",
    )

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
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [])
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "12345",
        "--max-wait", "1",
    ])
    # Should NOT be 3 (stale pidfile is ignored). Nothing is owned here, so the
    # run ends unbound rather than clean (BOU-2294).
    assert rc != 3
    assert rc == mc._AWAIT_UNBOUND


def test_await_reclaims_live_pidfile_from_non_waiter_process(
    tmp_path, monkeypatch, capsys
):
    """A recycled pid is not an incumbent waiter (BOU-2705).

    Process existence alone is not liveness for this registration: the pidfile
    can outlive its waiter and the OS can assign that pid to an unrelated
    process. Deferring in that state leaves the session's wake channel owned by
    a ghost.
    """
    live_but_unrelated_pid = os.getpid()
    _write_pidfile(str(tmp_path), live_but_unrelated_pid, SID)

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        _waiter_mod,
        "_process_identity",
        lambda pid: "Mon Jul 27 12:35:01 2026",
    )
    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: []
    )
    monkeypatch.setattr(
        _reconcile_mod,
        "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "12345",
        "--max-wait", "1",
    ])

    assert rc == mc._AWAIT_UNBOUND
    assert '"outcome":"deferred_to_loop"' not in capsys.readouterr().err
    assert not _await_pidfile_path(str(tmp_path)).exists()


def test_await_alive_accepts_custom_waiter_with_unmatched_quote(
    tmp_path, monkeypatch
):
    """Process identity must not parse a configured waiter's command text."""
    monkeypatch.setenv(
        "AGENTIC_PR_DASH_WAITER_DIR",
        str(tmp_path / "waiters"),
    )
    _waiter_mod._write_await_pidfile(
        "",
        {
            "pid": 123,
            "session_id": SID,
            "process_identity": "Mon Jul 27 12:34:56 2026",
        },
        SID,
    )
    monkeypatch.setattr(_waiter_mod, "_pid_alive", lambda pid: True)

    def fake_run(command, **kwargs):
        if "command=" in command:
            return types.SimpleNamespace(
                returncode=0,
                stdout="/opt/my-custom-waiter --cwd /tmp/John's/repo "
                f"--session-id {SID}",
            )
        return types.SimpleNamespace(
            returncode=0,
            stdout="Mon Jul 27 12:34:56 2026",
        )

    monkeypatch.setattr(_waiter_mod.subprocess, "run", fake_run)

    assert _waiter_mod._await_alive(str(tmp_path), SID) is True


def test_await_alive_rejects_recycled_waiter_from_another_session(
    tmp_path, monkeypatch
):
    """A same-shaped command cannot inherit another session's stale pidfile."""
    monkeypatch.setenv(
        "AGENTIC_PR_DASH_WAITER_DIR",
        str(tmp_path / "waiters"),
    )
    _waiter_mod._write_await_pidfile(
        "",
        {
            "pid": 123,
            "session_id": SID,
            "process_identity": "Mon Jul 27 12:34:56 2026",
        },
        SID,
    )
    monkeypatch.setattr(_waiter_mod, "_pid_alive", lambda pid: True)

    def fake_run(command, **kwargs):
        if "command=" in command:
            return types.SimpleNamespace(
                returncode=0,
                stdout="agentic-pr-dash await --session-id another-session",
            )
        return types.SimpleNamespace(
            returncode=0,
            stdout="Mon Jul 27 12:35:01 2026",
        )

    monkeypatch.setattr(_waiter_mod.subprocess, "run", fake_run)

    assert _waiter_mod._await_alive(str(tmp_path), SID) is False


def test_update_await_coverage_persists_process_identity(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AGENTIC_PR_DASH_WAITER_DIR",
        str(tmp_path / "waiters"),
    )
    monkeypatch.setattr(
        _waiter_mod.subprocess,
        "run",
        lambda *args, **kwargs: types.SimpleNamespace(
            returncode=0,
            stdout="Mon Jul 27 12:34:56 2026",
        ),
    )

    _waiter_mod._update_await_coverage(str(tmp_path), SID, [str(tmp_path)])

    data = _waiter_mod._read_await_pidfile("", SID)
    assert data["process_identity"] == "Mon Jul 27 12:34:56 2026"


def test_process_identity_uses_utc_timezone(monkeypatch):
    observed = {}

    def fake_run(command, **kwargs):
        observed.update(kwargs)
        return types.SimpleNamespace(
            returncode=0,
            stdout="Mon Jul 27 19:34:56 2026",
        )

    monkeypatch.setenv("TZ", "America/New_York")
    monkeypatch.setattr(_waiter_mod.subprocess, "run", fake_run)

    assert _waiter_mod._process_identity("123") == "Mon Jul 27 19:34:56 2026"
    assert observed["env"]["TZ"] == "UTC"


def test_await_max_wait_expiry_exit_0(tmp_path, monkeypatch, capsys):
    """When --max-wait 0 and nothing pending, exits 0."""
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(tmp_path)])
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])

    def _clean_check(path, session_id, *, claim=True):
        return 0, "nothing pending"

    monkeypatch.setattr(mc, "_check_worktree", _clean_check)
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    _bind_pr(monkeypatch)

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
    # BOU-1962: a clean owned worktree now exits via the clean-state early
    # exit before the deadline message; either exit reason is a valid 0.
    assert (
        "clean" in out.lower()
        or "max-wait" in out.lower()
        or "re-arm" in out.lower()
    )


def test_await_exits_0_promptly_when_owned_pr_clean_infinite_max_wait(
    tmp_path, monkeypatch, capsys
):
    """BOU-1962: an owned worktree with a CLEAN PR (no pending feedback, CI
    terminal) exits 0 on the first tick — even with ``--max-wait=-1`` (no
    deadline), which previously spun forever and needed a manual kill."""
    wt = tmp_path / "worktree"
    wt.mkdir()

    tick_count = [0]

    def _clean_check(path, session_id, *, claim=True):
        tick_count[0] += 1
        if tick_count[0] > 3:
            raise AssertionError("await did not exit on a clean PR (BOU-1962)")
        return 0, "nothing pending"

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)]
    )
    monkeypatch.setattr(
        _reconcile_mod,
        "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    monkeypatch.setattr(mc, "_check_worktree", _clean_check)
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    # Required CI terminal — nothing watch-pending.
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)
    monkeypatch.setattr("time.sleep", lambda s: None)
    _bind_pr(monkeypatch)

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "12345",
        "--max-wait=-1",
        "--interval", "0",
    ])
    out = capsys.readouterr().out
    assert rc == 0, f"Expected 0 (clean-state early exit), got {rc}"
    assert tick_count[0] == 1, "clean exit should fire on the first tick"
    assert "clean" in out.lower()
    assert not _await_pidfile_path(str(tmp_path)).exists()


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
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(wt)]
    )
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(mc, "_check_worktree", lambda path, sid, *, claim=True: (0, "clean"))
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", _fake_heartbeat)
    _bind_pr(monkeypatch)

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
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", _collect_and_check_pidfile)
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "12345",
        "--max-wait", "0",
        "--interval", "1",
    ])
    # Nothing owned this run, so the verdict is "unbound" (BOU-2294) — the
    # pidfile lifecycle under test is the same on every exit path.
    assert rc == mc._AWAIT_UNBOUND
    assert written_pids  # pidfile was present during the tick
    assert not pidfile_path.exists()  # cleaned up on exit
