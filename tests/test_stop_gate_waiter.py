"""Tests for the stop-gate waiter enforcement (BOU-1632).

When the stop-gate finds NO pending work but the session owns at least one
open non-draft PR and no live waiter exists, it exits 2 with a spawn prompt.

- No waiter + owned open PR → exit 2, stderr contains the await command
- Live waiter with matching session → exit 0
- --no-waiter flag → exit 0 (suppress the waiter branch)
- 3 consecutive "need-waiter" stops (same fingerprint) → 3rd exits 0 (loop-break)
- Pending work present → exit 2 via the existing pending-work path (unchanged)
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentic_pr_dash import config, maintenance_check as mc


SID = "sess-waiter-test"


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    """Disable stop-interval rate-limiting so tests run without waiting."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _make_armed_worktree(tmp_path: Path, session_id: str, pr_number: int) -> Path:
    """Create a worktree dir with an armed marker for the given PR."""
    wt = tmp_path / "worktree"
    wt.mkdir(exist_ok=True)
    mc._write_arm_marker(str(wt), session_id, os.getpid(), pr_number)
    return wt


def test_stop_gate_blocks_with_spawn_prompt_when_owned_pr_and_no_waiter(
    tmp_path, monkeypatch, capsys
):
    """No pending work + owned open PR + no live waiter → exit 2 with spawn command."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(mc, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(mc, "_detached_pr_records", lambda sid, cwd: [])
    # PR 42 is open, non-draft
    monkeypatch.setattr(mc, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(mc, "_await_alive", lambda cwd, sid: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2
    assert "waiter" in err.lower() or "await" in err.lower()
    assert "42" in err


def test_stop_gate_clean_exit_when_waiter_alive(tmp_path, monkeypatch, capsys):
    """No pending work + owned open PR + live waiter → exit 0."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(mc, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(mc, "_detached_pr_records", lambda sid, cwd: [])
    monkeypatch.setattr(mc, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(mc, "_await_alive", lambda cwd, sid: True)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert rc == 0


def test_stop_gate_no_waiter_flag_suppresses(tmp_path, monkeypatch, capsys):
    """--no-waiter suppresses the waiter-enforcement branch → exit 0."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(mc, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(mc, "_detached_pr_records", lambda sid, cwd: [])
    monkeypatch.setattr(mc, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(mc, "_await_alive", lambda cwd, sid: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID, "--no-waiter"])
    assert rc == 0


def test_stop_gate_need_waiter_loop_break(tmp_path, monkeypatch, capsys):
    """After 3 consecutive 'need-waiter' stops with the same fingerprint, exit 0."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(mc, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(mc, "_detached_pr_records", lambda sid, cwd: [])
    monkeypatch.setattr(mc, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(mc, "_await_alive", lambda cwd, sid: False)

    # First two calls → exit 2
    r1 = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    r2 = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert r1 == 2
    assert r2 == 2
    # Third call → loop-break releases the gate (exit 0)
    r3 = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert r3 == 0


def test_stop_gate_pending_work_still_wins(tmp_path, monkeypatch, capsys):
    """Pending work path is unchanged: exit 2 with the work block, no spawn prompt."""
    wt = _make_armed_worktree(tmp_path, SID, 99)

    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(
        mc, "_check_worktree",
        lambda path, sid, *, claim=True: (10, "needs review\nPR_NUMBER=99")
    )
    monkeypatch.setattr(mc, "_detached_pr_records", lambda sid, cwd: [])

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2
    assert "99" in err
    # The spawn prompt should NOT appear in the pending-work path
    assert "waiter" not in err.lower() or "address it" in err.lower()


def test_stop_gate_no_open_prs_exits_cleanly(tmp_path, monkeypatch, capsys):
    """No pending work and no owned open PRs → exit 0 (no waiter needed)."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(mc, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(mc, "_detached_pr_records", lambda sid, cwd: [])
    # No open PRs
    monkeypatch.setattr(mc, "_owned_open_pr_numbers", lambda owned: set())

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert rc == 0


def test_stop_gate_await_command_rendered_from_config(tmp_path, monkeypatch, capsys):
    """The spawn command in stderr uses the config await_command template."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    # Custom await_command in config
    monkeypatch.setenv("AGENTIC_PR_DASH_AWAIT_COMMAND",
                       "my-custom-waiter --cwd {cwd} --session-id {session_id}")
    config.load.cache_clear()

    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(mc, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(mc, "_detached_pr_records", lambda sid, cwd: [])
    monkeypatch.setattr(mc, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(mc, "_await_alive", lambda cwd, sid: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2
    # Custom template should be rendered with real cwd and session-id
    assert "my-custom-waiter" in err
    assert str(tmp_path) in err
    assert SID in err
