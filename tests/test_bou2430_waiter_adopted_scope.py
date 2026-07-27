"""BOU-2430: the waiter's blocking scope must agree with the stop gate's.

The stop gate explicitly excludes auto-adopted worktrees from its blocking
`pending` set (see test_adopted_pr_provenance.py / stop_gate.py's
`adopted_pending` split) and tells the operator so: "These were auto-adopted,
not armed by this session ... NOT blocking your stop." It then prescribes
launching the `await` waiter.

Before this fix, `_run_await_loop` had no equivalent split: every worktree in
`owned` (armed OR adopted) that `_check_worktree` reported as pending (code 10)
went straight into the waiter's blocking `pending` list, so the waiter exited
10 ("Feedback arrived — address it now") on the very PR the gate had just
called non-blocking. That is an unsatisfiable prescription: the gate says
"start a waiter", the waiter immediately reports required action on a PR nobody
asked this session to service (BOU-2430).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentic_pr_dash import config
from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash._maintenance import ownership_resolution as _ownres_mod
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod

SID = "sess-bou2430"


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _fake_ownership(provenance_by_wt: dict[str, str]):
    def _resolve(worktree_path, *, kind, snap=None):
        wt = str(Path(worktree_path))
        prov = provenance_by_wt.get(wt, "armed")
        return _ownres_mod.WorktreeOwnership(
            worktree=wt,
            session_id=SID,
            pr_number=999,
            provenance=prov,
            source="marker",
        )
    return _resolve


def test_waiter_does_not_block_on_an_adopted_worktrees_blockers(tmp_path, monkeypatch):
    """An adopted-only owned worktree with blockers must NOT make the waiter
    exit 10 — the gate already told the caller it is not their responsibility.
    """
    adopted_wt = tmp_path / "adopted"
    adopted_wt.mkdir()

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(adopted_wt)],
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    monkeypatch.setattr(
        _ownres_mod, "resolve_worktree", _fake_ownership({str(adopted_wt): "adopted"})
    )

    def _fake_check(path, session_id, *, claim=True):
        assert Path(path) == adopted_wt
        return 10, (
            "PR #999: needs work\nSUMMARY=PR #999: 1 unresolved review comment(s)\n"
            "PR_NUMBER=999"
        )

    monkeypatch.setattr(mc, "_check_worktree", _fake_check)

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "1",
        "--max-wait", "0",
    ])

    # Must NOT be the "feedback arrived, address it now" exit — that PR isn't
    # this session's to service.
    assert rc != 10, (
        "waiter blocked on an ADOPTED worktree's blockers — the stop gate says "
        "this is not the session's responsibility, so the waiter must not "
        "treat it as actionable feedback either (BOU-2430 scope mismatch)"
    )


def test_waiter_still_blocks_on_an_armed_worktrees_blockers(tmp_path, monkeypatch):
    """Control: a genuinely ARMED (this session's own) worktree's blockers must
    still wake the waiter — only the adopted case is excluded."""
    armed_wt = tmp_path / "armed"
    armed_wt.mkdir()

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(armed_wt)],
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    monkeypatch.setattr(
        _ownres_mod, "resolve_worktree", _fake_ownership({str(armed_wt): "armed"})
    )

    def _fake_check(path, session_id, *, claim=True):
        return 10, (
            "PR #1000: needs work\nSUMMARY=PR #1000: 1 unresolved review comment(s)\n"
            "PR_NUMBER=1000"
        )

    monkeypatch.setattr(mc, "_check_worktree", _fake_check)

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "1",
        "--max-wait", "0",
    ])

    assert rc == 10, "an ARMED worktree's real blockers must still wake the waiter"
