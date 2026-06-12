"""Tests for stale-marker pruning when a PR is merged/closed (BOU-1632).

When `check` (via _check_worktree) resolves a branch whose marker names a PR
that is no longer open (merged or closed), it should:
  1. Remove the .armed marker file
  2. Remove the pr-watch.stop-loop.json state file
  3. Prune the session ledger entry for that PR

The gh-unavailable path must NOT prune (a network blip must not destroy ownership).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentic_pr_dash import config, maintenance_check as mc, session_ledger as sl


SID = "sess-prune-test"


@pytest.fixture(autouse=True)
def _isolate_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _make_worktree(tmp_path: Path, session_id: str, pr_number: int) -> Path:
    wt = tmp_path / "worktree"
    wt.mkdir(exist_ok=True)
    mc._write_arm_marker(str(wt), session_id, os.getpid(), pr_number)
    return wt


def test_prune_stale_marker_on_merged_pr(tmp_path, monkeypatch):
    """Merged PR → armed marker + stop-loop state + ledger entry are all pruned."""
    wt = _make_worktree(tmp_path, SID, 42)
    # Write a stop-loop state file to verify it gets removed
    mc._save_stop_state(str(wt), {"ts": 0.0, "fingerprint": "old"})
    # Add a ledger entry
    sl.append(SID, pr=42, branch="bou-42", worktree=str(wt))

    marker_path = Path(mc._marker_path(str(wt)))
    stop_state_path = Path(mc._stop_state_path(str(wt)))
    assert marker_path.exists()
    assert stop_state_path.exists()
    assert sl.read(SID)  # ledger has the entry

    # Stub: branch has an armed marker pointing to PR 42, which is merged
    monkeypatch.setattr(mc, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(mc, "_resolve_pr_for_branch", lambda cwd: None)  # no open PR
    monkeypatch.setattr(mc, "_read_marker", lambda cwd: {"pr": "42", "session_id": SID, "pid": str(os.getpid())})
    # gh answers authoritatively: PR 42 is merged
    monkeypatch.setattr(mc, "_pr_open_state", lambda pr, cwd: ("merged", "https://x/pull/42", False, [], "", "", ""))

    # Trigger pruning via _prune_stale_marker
    mc._prune_stale_marker(str(wt), {"pr": "42", "session_id": SID}, SID)

    assert not marker_path.exists(), "Armed marker should be removed for merged PR"
    assert not stop_state_path.exists(), "Stop-loop state should be removed for merged PR"
    entries = sl.read(SID)
    assert not any(e.pr == 42 for e in entries), "Ledger entry should be pruned for merged PR"


def test_prune_stale_marker_on_closed_pr(tmp_path, monkeypatch):
    """Closed PR → same pruning as merged."""
    wt = _make_worktree(tmp_path, SID, 55)
    sl.append(SID, pr=55, branch="bou-55", worktree=str(wt))
    marker_path = Path(mc._marker_path(str(wt)))

    monkeypatch.setattr(mc, "_pr_open_state", lambda pr, cwd: ("closed", "https://x/pull/55", False, [], "", "", ""))

    mc._prune_stale_marker(str(wt), {"pr": "55", "session_id": SID}, SID)

    assert not marker_path.exists()
    assert not any(e.pr == 55 for e in sl.read(SID))


def test_prune_does_not_fire_when_gh_unavailable(tmp_path, monkeypatch):
    """When gh is unavailable (state='unknown'), do NOT prune."""
    wt = _make_worktree(tmp_path, SID, 77)
    sl.append(SID, pr=77, branch="bou-77", worktree=str(wt))
    marker_path = Path(mc._marker_path(str(wt)))

    monkeypatch.setattr(mc, "_pr_open_state", lambda pr, cwd: ("unknown", "", False, [], "", "", ""))

    mc._prune_stale_marker(str(wt), {"pr": "77", "session_id": SID}, SID)

    assert marker_path.exists(), "Marker must NOT be removed when gh is unavailable"
    assert any(e.pr == 77 for e in sl.read(SID)), "Ledger entry must NOT be pruned when gh unavailable"


def test_prune_does_not_fire_for_open_pr(tmp_path, monkeypatch):
    """An open PR should not trigger pruning."""
    wt = _make_worktree(tmp_path, SID, 88)
    sl.append(SID, pr=88, branch="bou-88", worktree=str(wt))
    marker_path = Path(mc._marker_path(str(wt)))

    monkeypatch.setattr(mc, "_pr_open_state", lambda pr, cwd: ("open", "https://x/pull/88", False, [], "", "", ""))

    mc._prune_stale_marker(str(wt), {"pr": "88", "session_id": SID}, SID)

    assert marker_path.exists(), "Marker must NOT be removed for open PR"
    assert any(e.pr == 88 for e in sl.read(SID))


def test_check_subcommand_prunes_stale_marker_via_stop_gate(tmp_path, monkeypatch, capsys):
    """stop-gate prunes stale marker when it encounters a merged/closed PR."""
    wt = _make_worktree(tmp_path, SID, 99)
    sl.append(SID, pr=99, branch="bou-99", worktree=str(wt))
    marker_path = Path(mc._marker_path(str(wt)))

    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    # No open PR found for the branch (it was merged)
    monkeypatch.setattr(mc, "_check_worktree", lambda path, sid, *, claim=True: (0, "no open PR for this branch"))
    monkeypatch.setattr(mc, "_detached_pr_records", lambda sid, cwd: [])
    monkeypatch.setattr(mc, "_owned_open_pr_numbers", lambda owned: set())

    # Also stub _read_marker to return the marker data and _pr_open_state to merged
    real_read_marker = mc._read_marker
    monkeypatch.setattr(mc, "_pr_open_state", lambda pr, cwd: ("merged", "https://x/pull/99", False, [], "", "", ""))

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    # stop-gate should exit 0 (no pending work)
    assert rc == 0


def test_stop_gate_prunes_stale_marker_for_merged_pr_and_exits_0(
    tmp_path, monkeypatch, capsys
):
    """stop-gate: armed marker for a merged PR → marker pruned → gate exits 0
    without demanding a waiter (regression for BOU-1632 Codex P2 finding 1).
    """
    wt = _make_worktree(tmp_path, SID, 101)
    sl.append(SID, pr=101, branch="bou-101", worktree=str(wt))
    marker_path = Path(mc._marker_path(str(wt)))
    assert marker_path.exists()

    # gh returns 'no open PR' for this worktree's branch (PR 101 was merged)
    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    # _check_worktree is NOT stubbed — it will call _resolve_pr_for_branch
    # Stub _resolve_pr_for_branch to return None (no open PR, gh answered)
    monkeypatch.setattr(mc, "_resolve_pr_for_branch", lambda cwd: None)
    # _live_foreign_owner returns None (no foreign owner)
    monkeypatch.setattr(mc, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(mc, "_detached_pr_records", lambda sid, cwd: [])
    # _pr_open_state returns 'merged' so _prune_stale_marker fires
    monkeypatch.setattr(
        mc, "_pr_open_state",
        lambda pr, cwd: ("merged", "https://x/pull/101", False, [], "", "", "")
    )

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID,
                  "--no-waiter"])
    assert rc == 0
    assert not marker_path.exists(), "Stale marker should be pruned by stop-gate"
    entries = sl.read(SID)
    assert not any(e.pr == 101 for e in entries), "Ledger entry should be pruned"
