"""BOU-2567 x BOU-2556 (PR #122 review, round 4): a deferred thread and a
budget-unknown PR are DIFFERENT facts with OPPOSITE stop-gate policies.

  deferred        — a real, verified finding, judged out of scope, tracked by
                     a ticket. Policy: do not block, do not dispatch.
  budget-unknown  — the stop gate's per-worktree wall-clock budget
                     (BOU-2556) ran out before this worktree was ever
                     examined. Policy: fail CLOSED — block, exactly like an
                     unresolved thread would.

The operator's verdict (round 4 addendum): budget-unknown wins. A deferral is
a fact about specific thread IDs recorded in the PAST; new findings can land
on the SAME PR after that snapshot (the concrete incident: PR #2863 was
deferred-only, then a later review round surfaced live push-guard bypasses).
Trusting a stale "nothing pending (deferred: N)" text for a worktree the gate
never actually re-examined this tick would silently hide exactly that. A
false block costs one tick and is loud/recoverable; a silently-trusted stale
deferral is not.

These tests prove the merged `_stop_gate_impl` (BOU-2567's deferred-count
extraction rebased onto BOU-2556's budget/cache loop) upholds this: a
worktree that never got far enough to be checked THIS tick is BUDGET-UNKNOWN,
never silently folded into "clean, N deferred" -- even when that worktree's
PR is genuinely deferred-only and a prior/cached check would have said so.
"""
from __future__ import annotations

import os
import time
import time as real_time
from pathlib import Path

import pytest

from agentic_pr_dash import config, maintenance_check as mc
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import worktree_check as _worktree_check_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod

SID = "sess-defer-vs-budget"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(tmp_path / "empty-daemons"))
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _make_armed_worktree(tmp_path: Path, name: str, pr_number: int) -> Path:
    wt = tmp_path / name
    wt.mkdir(exist_ok=True)
    mc._write_arm_marker(str(wt), SID, os.getpid(), pr_number)
    return wt


def test_budget_unknown_wins_over_a_never_reached_deferred_only_pr(
    monkeypatch, tmp_path, capsys,
):
    """Two owned worktrees. #1 is genuinely clean-and-deferred (would be
    reported "nothing pending (deferred: 1)" if checked). #2 never gets
    examined this tick because the budget runs out first -- if the gate
    treated "budget ran out" as "assume clean" it would exit 0 and silently
    skip whatever #2 actually needs. It must instead block."""
    monkeypatch.setenv("PR_AGENT_OPS_STOP_GATE_BUDGET", "5")
    config.load.cache_clear()

    wt_deferred = _make_armed_worktree(tmp_path, "wt-deferred", 501)
    wt_unreached = _make_armed_worktree(tmp_path, "wt-unreached", 502)
    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(wt_deferred), str(wt_unreached)],
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )

    # Deterministic fake clock, same technique as BOU-2556's own budget test:
    # the first (real) check "costs" enough of the 5s budget that the SECOND
    # worktree's check never even starts.
    clock = {"t": real_time.monotonic()}

    def _fake_monotonic():
        return clock["t"]

    monkeypatch.setattr(time, "monotonic", _fake_monotonic)

    checked: list[str] = []

    def _fake_check(path, sid, *, claim=True):
        checked.append(path)
        clock["t"] += 6.0  # exceeds the whole 5s budget in one "check"
        if path == str(wt_deferred):
            return 0, "nothing pending (deferred: 1)"
        return 0, "nothing pending"  # never actually reached

    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", _fake_check)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err

    assert rc == 2, (
        "a worktree the budget never reached must block the stop, even "
        "though the OTHER owned worktree is confirmed clean-and-deferred"
    )
    assert checked == [str(wt_deferred)], (
        "the second worktree must never have been checked at all this tick"
    )
    assert "BUDGET-UNKNOWN" in err
    # The two facts must never blur: a budget-unknown tick must not ALSO
    # claim a clean "N deferred, not blocking" verdict for the whole gate --
    # that print only fires on the fully-clean (not pending, not unknown)
    # branch, which this tick never reaches.
    assert "not blocking" not in err, (
        f"the deferred-clean, non-blocking message must not appear on a "
        f"tick that is blocking for an unrelated reason; stderr was: {err!r}"
    )


def test_control_all_worktrees_examined_within_budget_reports_deferred_clean(
    monkeypatch, tmp_path, capsys,
):
    """Control: when the budget is NOT exhausted and every owned worktree IS
    actually examined this tick, a deferred-only result DOES read as clean
    and DOES surface its count -- proving the assertions above are testing
    the budget-unknown distinction specifically, not merely "deferred is
    silenced by this test file's mocks"."""
    monkeypatch.setenv("PR_AGENT_OPS_STOP_GATE_BUDGET", "5")
    config.load.cache_clear()

    wt_deferred = _make_armed_worktree(tmp_path, "wt-deferred-only", 503)
    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(wt_deferred)],
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    monkeypatch.setattr(
        _worktree_check_mod, "_check_worktree",
        lambda path, sid, *, claim=True: (0, "nothing pending (deferred: 2)"),
    )

    # --no-waiter: sidesteps the separate "start a background waiter" demand
    # (unrelated to this assertion — see test_deferred_review_gate.py's own
    # clean-pass test for the same precedent) so a genuinely clean tick
    # actually returns 0 instead of asking for a waiter to be armed.
    rc = mc.main(
        ["stop-gate", "--cwd", str(tmp_path), "--session-id", SID, "--no-waiter"]
    )
    err = capsys.readouterr().err

    assert rc == 0
    assert "BUDGET-UNKNOWN" not in err
    assert "2" in err and "deferred" in err.lower() and "not blocking" in err
