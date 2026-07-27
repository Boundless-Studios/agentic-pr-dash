"""BOU-2450: a merged/closed PR pruned mid-tick must not linger in the stop
gate's waiter-demand set.

``_stop_gate_impl`` resolves ``pr_for`` (claim-derived) ONCE at the top of the
call via ``ownership_resolution.resolve_owned``. The per-worktree loop that
follows may discover — via a fresh ``_check_worktree``/``_pr_open_state``
probe — that a worktree's recorded PR has since merged or closed, and prunes
its marker + claim (``_prune_stale_marker``). But the waiter-demand block
further down reads the ALREADY-CAPTURED ``pr_for`` dict, not a re-resolved
one, so a PR pruned mid-tick still feeds ``claim_open_pr_numbers`` -> `
`owned_pr_numbers`` -> ``open_prs``, and the gate reports "You own open PR(s)
#N" (and may demand a waiter for it) for a PR that is, by this tick's own
finding, no longer open. This is the same defect class as BOU-2450's repro
(a merged PR reported as open) at the mechanism level.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentic_pr_dash import config
from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash._maintenance import ownership_resolution as _ownres_mod
from agentic_pr_dash._maintenance import stop_gate as _stop_gate_mod
from agentic_pr_dash._maintenance import worktree_check as _worktree_check_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod

SID = "sess-bou2450"


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def test_pruned_pr_does_not_linger_in_the_waiter_demand_set(tmp_path, monkeypatch, capsys):
    wt = tmp_path / "mine"
    wt.mkdir()
    wt_str = str(wt)

    resolution = _ownres_mod.OwnedResolution(
        worktrees=[wt_str],
        pr_for={wt_str: 101},
        provenance_for={wt_str: "armed"},
        source_for={wt_str: "both"},
    )
    monkeypatch.setattr(_ownres_mod, "resolve_owned", lambda *a, **k: resolution)

    # This tick's own worktree check finds PR 101 already merged: "nothing
    # pending" (code 0), NOT a blocker.
    monkeypatch.setattr(
        _worktree_check_mod, "_check_worktree",
        lambda path, session_id, *, claim=True: (0, "nothing pending"),
    )
    # The prune-check inside the per-worktree loop resolves the SAME stale PR
    # number and confirms (via a fresh probe) it is merged, so it prunes.
    monkeypatch.setattr(
        _ownres_mod, "resolve_worktree",
        lambda path, *, kind, snap=None: _ownres_mod.WorktreeOwnership(
            worktree=wt_str, session_id=SID, pr_number=101,
            provenance="armed", source="both",
        ),
    )
    pruned_calls = []

    def _fake_prune(cwd, marker, session_id):
        pruned_calls.append(marker)
        return True  # this tick's own probe confirmed PR 101 merged

    monkeypatch.setattr(_stop_gate_mod, "_prune_stale_marker", _fake_prune)
    # After pruning, a FRESH marker read finds nothing — _owned_open_pr_numbers
    # is called AFTER the loop and re-reads state, so it correctly reflects
    # the prune (this is the read that is NOT stale).
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: set())
    monkeypatch.setattr(
        _worktrees_mod, "_detached_records_across_roots",
        lambda sid, cwd: [],
    )
    monkeypatch.setattr(
        _worktrees_mod, "_owned_worktrees_across_roots",
        lambda sid, cwd: [wt_str],
    )
    monkeypatch.setattr(
        _worktrees_mod, "_reconcile_owned_across_roots",
        lambda sid, cwd, pid, deadline: ([wt_str], []),
    )

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])

    out, err = capsys.readouterr()
    assert pruned_calls, "expected the stale marker/claim to be pruned this tick"
    assert "#101" not in err and "#101" not in out, (
        "a PR pruned as merged THIS TICK must not appear in the waiter-demand "
        f"set (BOU-2450). stderr={err!r} stdout={out!r}"
    )
    assert rc == 0, "no genuinely open owned PR remains, so the gate must exit clean"


def test_pruned_number_in_one_repo_does_not_hide_same_number_elsewhere(
    tmp_path, monkeypatch, capsys,
):
    wt_a = tmp_path / "repo-a"; wt_a.mkdir()
    wt_b = tmp_path / "repo-b"; wt_b.mkdir()
    worktrees = [str(wt_a), str(wt_b)]
    resolution = _ownres_mod.OwnedResolution(
        worktrees=worktrees,
        pr_for={str(wt_a): 101, str(wt_b): 101},
        provenance_for={wt: "armed" for wt in worktrees},
        source_for={wt: "both" for wt in worktrees},
    )
    monkeypatch.setattr(_ownres_mod, "resolve_owned", lambda *a, **k: resolution)
    monkeypatch.setattr(
        _worktree_check_mod, "_check_worktree",
        lambda path, session_id, *, claim=True: (0, "nothing pending"),
    )
    monkeypatch.setattr(
        _ownres_mod, "resolve_worktree",
        lambda path, *, kind, snap=None: _ownres_mod.WorktreeOwnership(
            worktree=path, session_id=SID, pr_number=101,
            provenance="armed", source="both",
        ),
    )
    monkeypatch.setattr(
        _stop_gate_mod, "_prune_stale_marker",
        lambda cwd, marker, session_id: cwd == str(wt_a),
    )
    from agentic_pr_dash._maintenance import _common
    monkeypatch.setattr(_common, "_repo_slug", lambda wt: Path(wt).name)
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {101})
    monkeypatch.setattr(_worktrees_mod, "_detached_records_across_roots", lambda *a: [])
    monkeypatch.setattr(
        _worktrees_mod, "_reconcile_owned_across_roots",
        lambda *a, **k: (worktrees, []),
    )

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert rc == 2
    assert "101" in capsys.readouterr().err
