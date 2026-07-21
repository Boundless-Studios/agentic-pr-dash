"""BOU-2223 Stage 2: the stop gate's ownership reads flip onto the claim
store, with the marker as fallback whenever the two disagree.

Stage 1 (``tests/test_ownership_parity.py``) proved the claim-derived view
reproduces the marker-derived one. This file proves the FLIP itself:
``ownership_resolution.resolve_owned`` answers the stop gate's three
ownership questions (which worktrees, which PR, armed-or-adopted) claim-first
with a marker fallback, unions rather than intersects the two owned sets,
never resurrects a stale claim's deleted worktree, and every divergence is
logged. The last two tests wire this all the way through
``maintenance_check.main(["stop-gate", ...])`` to re-prove the BOU-2221
"adopted PRs don't block" guarantee now holds via the claim-derived path, not
just the marker-mocked one ``tests/test_adopted_pr_provenance.py`` already
pins.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path

import pytest

from agentic_pr_dash import config, ownership, ownership_parity
from agentic_pr_dash._maintenance import ownership_resolution
from agentic_pr_dash._maintenance.markers import _write_arm_marker

SID = "sess-claim-reads"
LIVE_PID = os.getpid()
REPO = "Boundless-Studios/agentic-pr-dash"


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Hermetic coordinator store + state dir + repo slug, so these tests never
    touch the real shared ``~/.agent-coordinator/claims.jsonl``."""
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(tmp_path / "claims.jsonl"))
    monkeypatch.setenv("AGENTIC_PR_DASH_STATE_DIR", str(tmp_path / ".gaia"))
    monkeypatch.setattr(config, "_detect_repo", lambda path: REPO)
    return tmp_path


def _mk(tmp_path: Path, name: str) -> str:
    wt = tmp_path / name
    wt.mkdir()
    return str(wt)


# ── 1. Agreement ─────────────────────────────────────────────────────────────


def test_claim_and_marker_agree_source_both_no_divergence(isolated_store):
    wt = _mk(isolated_store, "wt")
    assert _write_arm_marker(wt, SID, LIVE_PID, 100)

    result = ownership_resolution.resolve_owned(SID, wt, [wt])

    abspath = os.path.abspath(wt)
    assert result.worktrees == [abspath]
    assert result.source_for[abspath] == "both"
    assert result.pr_for[abspath] == 100
    assert result.provenance_for[abspath] == "armed"
    assert result.divergences == []


# ── 2. Claim-only worktree (union, fail-closed) ─────────────────────────────


def test_claim_only_worktree_is_still_owned_with_divergence_logged(isolated_store, monkeypatch):
    wt = _mk(isolated_store, "claim-only")
    abspath = os.path.abspath(wt)
    # A live claim exists here (dual-write path), but NO marker was ever
    # written — e.g. the marker write failed after the claim succeeded, or was
    # pruned independently. `_valid_claim_worktrees` normally requires `git
    # worktree list` to still enumerate the path; stub it so this test is
    # hermetic to the git-listing mechanics it is not exercising here (that is
    # test 5's job).
    monkeypatch.setattr(ownership_resolution, "_valid_claim_worktrees", lambda anchor: {abspath})
    outcome = ownership.record_ownership(
        repo=REPO, pr_number=200, session_id=SID, pid=LIVE_PID, worktree_path=wt,
    )
    assert outcome.ok

    result = ownership_resolution.resolve_owned(SID, wt, [])

    assert result.worktrees == [abspath]
    assert result.source_for[abspath] == "claim"
    assert result.pr_for[abspath] == 200
    assert any(d["reason"] == "claim_only_worktree" for d in result.divergences)
    logged = ownership_parity.read_divergences(wt)
    assert any(d.get("reason") == "claim_only_worktree" for d in logged)


# ── 3. Marker-only worktree (union, fail-closed) ────────────────────────────


def test_marker_only_worktree_is_still_owned_with_divergence_logged(
    isolated_store, monkeypatch, legacy_marker_writes
):
    wt = _mk(isolated_store, "marker-only")
    abspath = os.path.abspath(wt)
    # Disable dual-write for this one arm so a marker exists with NO mirrored
    # claim — the "claim never learned about this PR" case. `legacy_marker_writes`
    # is still needed so the marker write itself actually happens (Stage 4 turned
    # that off independently of dual-write).
    monkeypatch.setenv("AGENTIC_PR_DASH_OWNERSHIP_DUAL_WRITE", "0")
    assert _write_arm_marker(wt, SID, LIVE_PID, 300)
    monkeypatch.delenv("AGENTIC_PR_DASH_OWNERSHIP_DUAL_WRITE")

    result = ownership_resolution.resolve_owned(SID, wt, [wt])

    assert result.worktrees == [abspath]
    assert result.source_for[abspath] == "marker"
    # No claim contributed an answer — the caller must fall back to its own
    # marker read, not to a value pre-filled here.
    assert abspath not in result.pr_for
    assert abspath not in result.provenance_for
    reasons = {d["reason"] for d in result.divergences}
    assert "marker_only_worktree" in reasons


# ── 4. Unreadable store: fall back entirely to markers ──────────────────────


def test_unreadable_store_falls_back_entirely_to_marker_set(isolated_store):
    wt1 = _mk(isolated_store, "wt1")
    wt2 = _mk(isolated_store, "wt2")
    assert _write_arm_marker(wt1, SID, LIVE_PID, 1)
    assert _write_arm_marker(wt2, SID, LIVE_PID, 2)
    unknown_snap = ownership.OwnershipSnapshot({}, now=datetime.now(timezone.utc), ok=False)

    result = ownership_resolution.resolve_owned(SID, wt1, [wt1, wt2], snap=unknown_snap)

    abs1, abs2 = os.path.abspath(wt1), os.path.abspath(wt2)
    assert sorted(result.worktrees) == sorted([abs1, abs2])
    assert result.pr_for == {}
    assert result.provenance_for == {}
    assert result.source_for == {abs1: "marker", abs2: "marker"}
    assert len(result.divergences) == 1
    assert result.divergences[0]["reason"] == "claim_store_unreadable"


# ── 5. A stale claim must not resurrect a deleted worktree ──────────────────


def test_claim_naming_a_deleted_worktree_is_not_resurrected(isolated_store, monkeypatch):
    deleted = os.path.join(str(isolated_store), "long-gone")  # never created on disk
    ownership.record_ownership(
        repo=REPO, pr_number=400, session_id=SID, pid=LIVE_PID, worktree_path=deleted,
    )
    # Even if `git worktree list` still (staleley) enumerated it...
    monkeypatch.setattr(
        ownership_resolution, "_valid_claim_worktrees", lambda anchor: {os.path.abspath(deleted)}
    )

    result = ownership_resolution.resolve_owned(SID, str(isolated_store), [])

    # ...the directory not existing on disk must still keep it out entirely —
    # not owned, not divergent, not anywhere in the result.
    assert result.worktrees == []
    assert result.pr_for == {}
    assert not any(d.get("worktree") == os.path.abspath(deleted) for d in result.divergences)


def test_claim_naming_a_worktree_git_no_longer_lists_is_not_resurrected(isolated_store, monkeypatch):
    """The directory can still exist on disk (not yet `git worktree prune`d
    away) — it must be excluded anyway once `git worktree list` stops naming
    it, the other half of the resurrection guard."""
    wt = _mk(isolated_store, "orphaned-worktree")
    ownership.record_ownership(
        repo=REPO, pr_number=401, session_id=SID, pid=LIVE_PID, worktree_path=wt,
    )
    monkeypatch.setattr(ownership_resolution, "_valid_claim_worktrees", lambda anchor: set())

    result = ownership_resolution.resolve_owned(SID, wt, [])

    assert result.worktrees == []


# ── 6. Provenance disagreement resolves toward "armed" ──────────────────────


def test_provenance_disagreement_resolves_to_armed_and_logs_divergence(
    isolated_store, legacy_marker_writes
):
    wt = _mk(isolated_store, "wt")
    assert _write_arm_marker(wt, SID, LIVE_PID, 500, provenance="armed")
    # Diverge the marker's provenance in place, independent of the claim,
    # simulating a lagging/failed dual-write update.
    from agentic_pr_dash._maintenance.markers import _marker_path

    fields = {}
    with open(_marker_path(wt), encoding="utf-8") as fh:
        for line in fh:
            k, _sep, v = line.partition("=")
            fields[k.strip()] = v.strip()
    fields["provenance"] = "adopted"
    with open(_marker_path(wt), "w", encoding="utf-8") as fh:
        fh.write("".join(f"{k}={v}\n" for k, v in fields.items()))

    result = ownership_resolution.resolve_owned(SID, wt, [wt])

    abspath = os.path.abspath(wt)
    # Fail-closed: the claim's "armed" wins over the marker's "adopted" — a
    # false "adopted" would un-block a gate that must keep blocking.
    assert result.provenance_for[abspath] == "armed"
    divergence = next(d for d in result.divergences if d["reason"] == "different_provenance")
    assert divergence["marker_provenance"] == "adopted"
    assert divergence["claim_provenance"] == "armed"


# ── 7. Kill switch ───────────────────────────────────────────────────────────


def test_kill_switch_off_is_byte_identical_to_pure_marker_behaviour(isolated_store, monkeypatch):
    wt = _mk(isolated_store, "wt")
    assert _write_arm_marker(wt, SID, LIVE_PID, 600)
    monkeypatch.setenv("AGENTIC_PR_DASH_OWNERSHIP_CLAIM_READS", "0")

    result = ownership_resolution.resolve_owned(SID, wt, [wt])

    abspath = os.path.abspath(wt)
    assert result.worktrees == [abspath]
    assert result.source_for == {abspath: "marker"}
    assert result.pr_for == {}
    assert result.provenance_for == {}
    assert result.divergences == []


@pytest.mark.parametrize("off_value", ["0", "false", "No", "OFF"])
def test_kill_switch_parsing_mirrors_dual_write_enabled(monkeypatch, off_value):
    monkeypatch.setenv("AGENTIC_PR_DASH_OWNERSHIP_CLAIM_READS", off_value)
    assert ownership_resolution.claim_reads_enabled() is False


def test_kill_switch_defaults_on(monkeypatch):
    monkeypatch.delenv("AGENTIC_PR_DASH_OWNERSHIP_CLAIM_READS", raising=False)
    assert ownership_resolution.claim_reads_enabled() is True


# ── 8. BOU-2221 regression, via the claim-derived path ──────────────────────


def _stub_stop_gate_worktrees(monkeypatch, owned: list[str]) -> None:
    from agentic_pr_dash._maintenance import worktrees as _worktrees_mod

    monkeypatch.setattr(
        _worktrees_mod, "_owned_worktrees_across_roots", lambda session_id, anchor: list(owned)
    )
    monkeypatch.setattr(
        _worktrees_mod,
        "_reconcile_owned_across_roots",
        lambda session_id, anchor, pid, deadline=None: (list(owned), []),
    )
    monkeypatch.setattr(
        _worktrees_mod, "_detached_records_across_roots", lambda session_id, anchor: []
    )


def test_bou2221_adopted_pr_does_not_block_via_claim_derived_provenance(
    isolated_store, monkeypatch
):
    """The BOU-2221 guarantee, re-proven through REAL claim data (not a
    monkeypatched ``_marker_provenance``): an auto-adopted sibling's blocked PR
    must not hold the session's stop, and the claim-derived provenance is what
    tells the gate that."""
    from agentic_pr_dash import maintenance_check
    from agentic_pr_dash._maintenance import worktree_check as _worktree_check_mod

    mine = _mk(isolated_store, "mine")
    sibling = _mk(isolated_store, "sibling")
    assert _write_arm_marker(mine, SID, LIVE_PID, 111, provenance="armed")
    assert _write_arm_marker(sibling, SID, LIVE_PID, 222, provenance="adopted")

    _stub_stop_gate_worktrees(monkeypatch, [mine, sibling])
    monkeypatch.setattr(
        _worktree_check_mod,
        "_check_worktree",
        lambda path, session_id, *, claim=True: (
            (10, "blocker\nPR_NUMBER=222") if str(path) == str(sibling) else (0, "clean")
        ),
    )
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    config.load.cache_clear()

    # Sanity: the claim really does carry "adopted" for the sibling, so the
    # gate's rc==0 below is provably reading it, not coincidentally passing.
    view = ownership.snapshot().owner_for(REPO, 222)
    assert view is not None and view.provenance == "adopted"

    rc = maintenance_check.main(
        ["stop-gate", "--cwd", str(isolated_store), "--session-id", SID, "--no-waiter"]
    )

    assert rc == 0, "an auto-adopted sibling PR must not block the stop gate"


def test_bou2221_armed_pr_still_blocks_via_claim_derived_provenance(isolated_store, monkeypatch):
    """The other direction: a PR this session actually armed must still block,
    even though the decision now flows through the claim-derived provenance."""
    from agentic_pr_dash import maintenance_check
    from agentic_pr_dash._maintenance import worktree_check as _worktree_check_mod

    mine = _mk(isolated_store, "mine")
    sibling = _mk(isolated_store, "sibling")
    assert _write_arm_marker(mine, SID, LIVE_PID, 111, provenance="armed")
    assert _write_arm_marker(sibling, SID, LIVE_PID, 222, provenance="adopted")

    _stub_stop_gate_worktrees(monkeypatch, [mine, sibling])
    monkeypatch.setattr(
        _worktree_check_mod,
        "_check_worktree",
        lambda path, session_id, *, claim=True: (
            (10, "blocker\nPR_NUMBER=111") if str(path) == str(mine) else (0, "clean")
        ),
    )
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    config.load.cache_clear()

    rc = maintenance_check.main(
        ["stop-gate", "--cwd", str(isolated_store), "--session-id", SID, "--no-waiter"]
    )

    assert rc == 2, "a PR this session actually armed must still block the stop gate"
