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
import types
from pathlib import Path

import pytest

from agentic_pr_dash import config, github_api, maintenance_check as mc
from agentic_pr_dash._maintenance import (
    ownership_resolution as _ownership_resolution_mod,
)
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import stop_gate as _stop_gate_mod
from agentic_pr_dash._maintenance import waiter as _waiter_mod
from agentic_pr_dash._maintenance import worktree_check as _worktree_check_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod


SID = "sess-waiter-test"


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch, tmp_path):
    """Disable stop-interval rate-limiting so tests run without waiting."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    # Isolate from any real detached pr-maintenance-loop daemon on this machine:
    # point the daemon dir at an empty path so _detached_loop_alive resolves a
    # missing pidfile (False) unless a test opts in explicitly.
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(tmp_path / "empty-daemons"))
    config.load.cache_clear()
    yield
    config.load.cache_clear()


_DEAD_PID = 2147480000  # unused high pid — the owning session is gone


def _make_armed_worktree(tmp_path: Path, session_id: str, pr_number: int, pid: int | None = None) -> Path:
    """Create a worktree dir with an armed marker for the given PR.

    ``pid`` defaults to this live test process (an ACTIVELY-owned worktree). Pass
    ``_DEAD_PID`` to model a worktree whose owning session has gone away — the
    case where the detached loop legitimately provides coverage.
    """
    wt = tmp_path / "worktree"
    wt.mkdir(exist_ok=True)
    mc._write_arm_marker(str(wt), session_id, os.getpid() if pid is None else pid, pr_number)
    return wt


def test_stop_gate_blocks_with_spawn_prompt_when_owned_pr_and_no_waiter(
    tmp_path, monkeypatch, capsys
):
    """No pending work + owned open PR + no live waiter → exit 2 with spawn command."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    # PR 42 is open, non-draft
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2
    assert "waiter" in err.lower() or "await" in err.lower()
    assert "42" in err


def test_stop_gate_clean_exit_when_waiter_alive(tmp_path, monkeypatch, capsys):
    """No pending work + owned open PR + live waiter → exit 0."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: True)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert rc == 0


def test_stop_gate_does_not_demand_waiter_for_claim_owned_draft(
    tmp_path,
    monkeypatch,
    capsys,
):
    """Claim-derived ownership must retain `_check_worktree`'s draft verdict."""
    wt = tmp_path / "worktree"
    wt.mkdir()

    monkeypatch.setattr(
        _worktrees_mod,
        "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(wt)],
    )
    monkeypatch.setattr(
        _ownership_resolution_mod,
        "resolve_owned",
        lambda session_id, cwd, marker_owned, *, snap=None: types.SimpleNamespace(
            worktrees=[str(wt)],
            pr_for={str(wt): 2704},
            provenance_for={str(wt): "armed"},
        ),
    )
    monkeypatch.setattr(
        _ownership_resolution_mod,
        "resolve_worktree",
        lambda worktree, *, kind, snap=None: types.SimpleNamespace(pr_number=None),
    )
    monkeypatch.setattr(
        _worktree_check_mod,
        "_check_worktree",
        lambda path, sid, *, claim=True: (0, "PR is a draft; nothing pending"),
    )
    monkeypatch.setattr(
        _reconcile_mod,
        "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: set())
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err

    assert rc == 0
    assert "await" not in err.lower()
    assert "waiter" not in err.lower()


def test_stop_gate_does_not_demand_waiter_for_marker_owned_draft(
    tmp_path,
    monkeypatch,
    capsys,
):
    """Marker-derived ownership must retain `_check_worktree`'s draft verdict."""
    wt = _make_armed_worktree(tmp_path, SID, 2704)

    monkeypatch.setattr(
        _worktrees_mod,
        "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(wt)],
    )
    monkeypatch.setattr(
        _worktree_check_mod,
        "_check_worktree",
        lambda path, sid, *, claim=True: (0, "PR is a draft; nothing pending"),
    )
    monkeypatch.setattr(
        _reconcile_mod,
        "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {2704})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err

    assert rc == 0
    assert "await" not in err.lower()
    assert "waiter" not in err.lower()


def test_stop_gate_keeps_same_number_non_draft_in_another_repo(
    tmp_path,
    monkeypatch,
    capsys,
):
    """A draft PR number must not hide a non-draft PR in another repository."""
    draft_wt = tmp_path / "draft-worktree"
    ready_wt = tmp_path / "ready-worktree"
    draft_wt.mkdir()
    ready_wt.mkdir()
    worktrees = [str(draft_wt), str(ready_wt)]

    monkeypatch.setattr(
        _worktrees_mod,
        "_collect_stop_gate_worktrees",
        lambda sid, cwd: worktrees,
    )
    monkeypatch.setattr(
        _ownership_resolution_mod,
        "resolve_owned",
        lambda session_id, cwd, marker_owned, *, snap=None: types.SimpleNamespace(
            worktrees=worktrees,
            pr_for={str(draft_wt): 2704, str(ready_wt): 2704},
            provenance_for={wt: "armed" for wt in worktrees},
        ),
    )
    monkeypatch.setattr(
        _ownership_resolution_mod,
        "resolve_worktree",
        lambda worktree, *, kind, snap=None: types.SimpleNamespace(pr_number=None),
    )
    monkeypatch.setattr(
        _worktree_check_mod,
        "_check_worktree",
        lambda path, sid, *, claim=True: (
            (0, "PR is a draft; nothing pending")
            if path == str(draft_wt)
            else (0, "nothing pending")
        ),
    )
    monkeypatch.setattr(
        _reconcile_mod,
        "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {2704})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err

    assert rc == 2
    assert "2704" in err


def test_stop_gate_no_waiter_flag_suppresses(tmp_path, monkeypatch, capsys):
    """--no-waiter suppresses the waiter-enforcement branch → exit 0."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID, "--no-waiter"])
    assert rc == 0


def test_stop_gate_live_detached_loop_suppresses_waiter(tmp_path, monkeypatch, capsys):
    """A live detached loop is sufficient idle coverage ONLY once the owning
    session is gone (BOU-1653, refined by session-precedence).

    With no pending work and an owned open PR whose marker pid is DEAD (session
    gone), the stop-gate must NOT prompt for a per-session waiter when the
    detached loop daemon is alive → exit 0. (A live in-session owner keeps its
    own waiter — see test_live_in_session_owner_keeps_waiter_despite_live_loop.)
    """
    wt = _make_armed_worktree(tmp_path, SID, 42, pid=_DEAD_PID)

    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)
    # The autouse fixture forces the loop dead; flip it live for this case.
    monkeypatch.setattr(_waiter_mod, "_detached_loop_alive", lambda cwd: True)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 0
    assert "await" not in err.lower()


def test_live_in_session_owner_keeps_waiter_despite_live_loop(tmp_path, monkeypatch, capsys):
    """Session precedence: a LIVE in-session owner (pid-alive marker) keeps its
    OWN waiter even when the machine-wide detached loop is alive — the loop
    defers to the session, so it is NOT coverage for that PR → exit 2 (spawn a
    per-session waiter)."""
    wt = _make_armed_worktree(tmp_path, SID, 42)  # live pid (os.getpid())

    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)
    # Loop is alive — but the live in-session owner takes precedence.
    monkeypatch.setattr(_waiter_mod, "_detached_loop_alive", lambda cwd: True)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2
    assert "await" in err.lower()
    assert "#42" in err


def test_detached_ledger_pr_still_gets_waiter_when_loop_live(tmp_path, monkeypatch, capsys):
    """The detached loop only services worktree-backed PRs. A detached-ledger PR
    (worktree torn down, no blockers yet) must still get a waiter even when a
    machine-wide loop is live (codex PR #21 review).

    The worktree-backed PR #42 here uses a DEAD marker pid (owning session gone)
    so the live loop legitimately covers it — isolating the detached-ledger
    behavior. A live in-session owner is covered separately."""
    wt = _make_armed_worktree(tmp_path, SID, 42, pid=_DEAD_PID)

    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(
        _reconcile_mod,
        "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [{"pr": 99, "state": "open", "p1": False, "unresolved_threads": 0}],
    )
    monkeypatch.setattr(_stop_gate_mod, "_record_has_blockers", lambda r, **kw: False)
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)
    monkeypatch.setattr(_waiter_mod, "_detached_loop_alive", lambda cwd: True)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2
    # Use the "#<n>" PR token, not a bare number — the rendered await command
    # contains the cwd path, whose random pytest tmp id can coincidentally contain
    # the digits "42" (e.g. pytest-423).
    assert "#99" in err  # detached PR still demands a waiter
    assert "#42" not in err  # worktree-backed PR is covered by the live loop


def test_detached_loop_alive_reads_pidfile(tmp_path, monkeypatch):
    """_detached_loop_alive: live pid + fresh healthy record → True; a live pid
    ALONE → False (BOU-2086); dead pid → False; missing → False.

    Requires the machine-wide opt-in (a scoped loop is not proof of coverage).
    """
    from agentic_pr_dash import loop

    daemon_dir = tmp_path / "daemons"
    daemon_dir.mkdir()
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(daemon_dir))
    monkeypatch.setenv("GAIA_MAINTENANCE_LOOP_MACHINE_WIDE", "true")
    config.load.cache_clear()

    pidfile = daemon_dir / "pr-maintenance-loop.pid"

    # Missing pidfile → not alive.
    assert mc._detached_loop_alive(str(tmp_path)) is False

    # Live pid (this process) but NO health record → pid-alive alone is NOT
    # proof of maintenance capability (BOU-2086).
    pidfile.write_text(str(os.getpid()), encoding="utf-8")
    assert mc._detached_loop_alive(str(tmp_path)) is False

    # Live pid + fresh, executors-viable health record → alive.
    loop.record_loop_health(str(tmp_path), executors_viable=True, interval=600)
    assert mc._detached_loop_alive(str(tmp_path)) is True

    # Dead pid → not alive even with a healthy record. PID 1 is always live, so
    # use a pid that cannot exist.
    pidfile.write_text("2147483647", encoding="utf-8")
    assert mc._detached_loop_alive(str(tmp_path)) is False


def test_detached_loop_alive_false_without_machine_wide_optin(tmp_path, monkeypatch):
    """A live loop on a shared pidfile does NOT count unless declared machine-wide
    — a session/repo-scoped loop must not suppress another session's waiter."""
    daemon_dir = tmp_path / "daemons"
    daemon_dir.mkdir()
    (daemon_dir / "pr-maintenance-loop.pid").write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(daemon_dir))
    monkeypatch.delenv("GAIA_MAINTENANCE_LOOP_MACHINE_WIDE", raising=False)
    monkeypatch.delenv("AGENTIC_PR_DASH_MAINTENANCE_LOOP_MACHINE_WIDE", raising=False)
    config.load.cache_clear()

    assert mc._detached_loop_alive(str(tmp_path)) is False


def test_stop_gate_need_waiter_loop_break(tmp_path, monkeypatch, capsys):
    """After 3 consecutive 'need-waiter' stops with the same fingerprint, exit 0."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)
    head = {"sha": "head-1"}
    monkeypatch.setattr(_stop_gate_mod, "_local_head_sha", lambda cwd: head["sha"])

    # First two calls → exit 2
    r1 = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    r2 = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert r1 == 2
    assert r2 == 2
    # Third call → loop-break releases the gate (exit 0)
    r3 = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    capsys.readouterr()
    assert r3 == 0

    # The bounded release is durable for this exact observation. A later Stop
    # attempt must not immediately re-arm and inject the same waiter request.
    r4 = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert r4 == 0
    assert capsys.readouterr().err == ""

    # A new immutable PR head is new observation state even though the open PR
    # number and waiter requirement are otherwise identical.
    head["sha"] = "head-2"
    r5 = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert r5 == 2
    assert "42" in capsys.readouterr().err


def test_stop_gate_ci_rerun_invalidates_waiter_release(tmp_path, monkeypatch, capsys):
    """Required CI restarting on the same head is a new waiter observation."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)
    monkeypatch.setattr(_stop_gate_mod, "_local_head_sha", lambda cwd: "head-1")

    observation = {"verified": False, "ci_pending": False}
    monkeypatch.setattr(
        _waiter_mod,
        "_read_clean_exit_keys",
        lambda sid: (
            {_waiter_mod._clean_exit_key("", 42)}
            if observation["verified"]
            else set()
        ),
    )
    monkeypatch.setattr(
        github_api,
        "required_checks_pending",
        lambda number, cwd: observation["ci_pending"],
    )

    assert [
        mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
        for _ in range(3)
    ] == [2, 2, 0]
    capsys.readouterr()
    assert mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID]) == 0

    observation["ci_pending"] = True
    assert mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID]) == 2
    assert "42" in capsys.readouterr().err


def test_stop_gate_ci_identity_swap_invalidates_waiter_release(
    tmp_path, monkeypatch, capsys
):
    """CI moving between owned PRs is new state even while some CI stays pending."""
    wt_a = tmp_path / "worktree-a"
    wt_b = tmp_path / "worktree-b"
    wt_a.mkdir()
    wt_b.mkdir()
    mc._write_arm_marker(str(wt_a), SID, os.getpid(), 41)
    mc._write_arm_marker(str(wt_b), SID, os.getpid(), 42)

    monkeypatch.setattr(
        _worktrees_mod,
        "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(wt_a), str(wt_b)],
    )
    monkeypatch.setattr(
        _worktree_check_mod,
        "_check_worktree",
        lambda path, sid, *, claim=True: (0, "nothing pending"),
    )
    monkeypatch.setattr(
        _reconcile_mod,
        "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    monkeypatch.setattr(
        _stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {41, 42}
    )
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)
    monkeypatch.setattr(_stop_gate_mod, "_local_head_sha", lambda cwd: "head")
    pending = {41}
    monkeypatch.setattr(
        github_api,
        "required_checks_pending",
        lambda number, cwd: number in pending,
    )

    assert [
        mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
        for _ in range(3)
    ] == [2, 2, 0]
    capsys.readouterr()

    pending.clear()
    pending.add(42)
    assert mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID]) == 2
    assert "41" in capsys.readouterr().err


def test_stop_gate_escalation_detail_invalidates_release(tmp_path, monkeypatch, capsys):
    """A changed escalation streak/error must re-arm the same open PR."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)
    escalation = {"count": 3, "last_error": "executor failed"}
    monkeypatch.setattr(
        _stop_gate_mod,
        "_read_escalation_marker",
        lambda cwd: {"42": escalation.copy()},
    )

    assert [
        mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
        for _ in range(3)
    ] == [2, 2, 0]
    capsys.readouterr()
    assert mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID]) == 0

    escalation["count"] = 4
    escalation["last_error"] = "executor failed again"
    assert mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID]) == 2
    assert "executor failed again" in capsys.readouterr().err


def test_stop_gate_pending_work_still_wins(tmp_path, monkeypatch, capsys):
    """Pending work path is unchanged: exit 2 with the work block, no spawn prompt."""
    wt = _make_armed_worktree(tmp_path, SID, 99)

    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(
        _worktree_check_mod, "_check_worktree",
        lambda path, sid, *, claim=True: (
            10,
            "needs review\nSUMMARY=PR #99 (br): 1 unresolved review comment(s), CI green\nPR_NUMBER=99",
        )
    )
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2
    assert "99" in err
    # The spawn prompt should NOT appear in the pending-work path
    assert "waiter" not in err.lower() or "address it" in err.lower()


def test_stop_gate_no_open_prs_exits_cleanly(tmp_path, monkeypatch, capsys):
    """No pending work and no owned open PRs → exit 0 (no waiter needed).

    Deliberately a BARE worktree, not `_make_armed_worktree`: that helper's
    `_write_arm_marker` dual-writes a real ownership CLAIM by default (BOU-2223),
    and the stop gate's waiter-demand branch unions the claim-derived
    `pr_for.values()` on top of the `_owned_open_pr_numbers` stub below — an armed
    worktree would leak PR 42's claim back in through that union even though the
    marker-only stub says "no open PRs", making the "no owned open PRs" premise
    dishonest. A worktree with neither a marker nor a claim is the honest way to
    express it.
    """
    wt = tmp_path / "worktree"
    wt.mkdir()

    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    # No open PRs
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: set())

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert rc == 0


def test_stop_gate_await_command_rendered_from_config(tmp_path, monkeypatch, capsys):
    """The spawn command in stderr uses the config await_command template."""
    wt = _make_armed_worktree(tmp_path, SID, 42)

    # Custom await_command in config
    monkeypatch.setenv("AGENTIC_PR_DASH_AWAIT_COMMAND",
                       "my-custom-waiter --cwd {cwd} --session-id {session_id}")
    config.load.cache_clear()

    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2
    # Custom template should be rendered with real cwd and session-id
    assert "my-custom-waiter" in err
    assert str(tmp_path) in err
    assert SID in err
