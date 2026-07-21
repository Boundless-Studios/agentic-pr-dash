"""BOU-2223 Stage 3: the remaining ownership readers flip onto the claim store.

Stage 1 (``tests/test_ownership_parity.py``) proved the claim-derived view
reproduces the marker-derived one. Stage 2 (``tests/test_stop_gate_claim_reads.py``)
flipped the stop gate via ``ownership_resolution.resolve_owned``, which answers the
*set* question — "which worktrees do I own".

Stage 3 flips the five readers that ask the transpose about a path they already
hold: ``reconcile.py``, ``waiter.py``, ``worktrees.py``, ``app.py`` and
``codex_hooks/run_post_push_watch.py``. They share
``ownership_resolution.resolve_worktree``, so most of what follows pins that
helper's contract — union rather than intersection, provenance resolving toward
``armed``, an unreadable store meaning "could not look" rather than "no claims",
and the kill switch reverting every one of them — and then proves each reader
actually consults it.

Markers remain the WRITE authority throughout; retiring the writers is Stage 4.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path

import pytest

from agentic_pr_dash import config, ownership, ownership_parity
from agentic_pr_dash._maintenance import ownership_resolution
from agentic_pr_dash._maintenance.markers import _write_arm_marker
from agentic_pr_dash._maintenance.ownership_resolution import resolve_worktree

SID = "sess-stage3"
OTHER_SID = "sess-stage3-other"
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


def _arm_marker_only(monkeypatch, wt: str, session_id: str, pr: int) -> None:
    """Write a marker with NO mirrored claim — the pre-Stage-1 install case that
    dominated the Stage 2 bake."""
    monkeypatch.setenv("AGENTIC_PR_DASH_OWNERSHIP_DUAL_WRITE", "0")
    assert _write_arm_marker(wt, session_id, LIVE_PID, pr)
    monkeypatch.delenv("AGENTIC_PR_DASH_OWNERSHIP_DUAL_WRITE")


def _unknown_snap():
    return ownership.OwnershipSnapshot({}, now=datetime.now(timezone.utc), ok=False)


# ── resolve_worktree: agreement ──────────────────────────────────────────────


def test_claim_and_marker_agree_source_both_no_divergence(isolated_store):
    wt = _mk(isolated_store, "wt")
    assert _write_arm_marker(wt, SID, LIVE_PID, 100)

    owned = resolve_worktree(wt, kind="test_divergence")

    assert owned.source == "both"
    assert owned.pr_number == 100
    assert owned.owned_by(SID)
    assert owned.divergences == []


# ── resolve_worktree: the union rule, from each side alone ───────────────────


def test_marker_only_worktree_is_still_owned(isolated_store, monkeypatch):
    wt = _mk(isolated_store, "marker-only")
    _arm_marker_only(monkeypatch, wt, SID, 300)

    owned = resolve_worktree(wt, kind="test_divergence")

    assert owned.source == "marker"
    assert owned.pr_number == 300
    # Fail closed: the claim side knowing nothing must not un-own this.
    assert owned.owned_by(SID)
    assert {d["reason"] for d in owned.divergences} == {"marker_only_worktree"}
    assert any(
        d.get("reason") == "marker_only_worktree"
        for d in ownership_parity.read_divergences(wt)
    )


def test_claim_only_worktree_is_still_owned(isolated_store):
    wt = _mk(isolated_store, "claim-only")
    assert ownership.record_ownership(
        repo=REPO, pr_number=200, session_id=SID, pid=LIVE_PID, worktree_path=wt,
    ).ok

    owned = resolve_worktree(wt, kind="test_divergence")

    assert owned.source == "claim"
    assert owned.pr_number == 200
    assert owned.owned_by(SID)
    assert {d["reason"] for d in owned.divergences} == {"claim_only_worktree"}


def test_owned_by_is_a_union_not_an_intersection(isolated_store, monkeypatch):
    """A marker owned by one session and a live claim held by another: BOTH own
    it. Intersecting would strand whichever one the gate dropped."""
    wt = _mk(isolated_store, "contested")
    _arm_marker_only(monkeypatch, wt, SID, 400)
    assert ownership.record_ownership(
        repo=REPO, pr_number=400, session_id=OTHER_SID, pid=LIVE_PID, worktree_path=wt,
    ).ok

    owned = resolve_worktree(wt, kind="test_divergence")

    assert owned.owned_by(SID)
    assert owned.owned_by(OTHER_SID)
    assert not owned.owned_by("sess-unrelated")
    assert any(d["reason"] == "different_session_id" for d in owned.divergences)


# ── resolve_worktree: disagreement resolution ────────────────────────────────


def test_provenance_disagreement_resolves_to_armed(isolated_store, monkeypatch):
    """An erroneous "adopted" un-blocks a gate that should still block
    (BOU-2221), so the stricter answer wins."""
    wt = _mk(isolated_store, "prov")
    _arm_marker_only(monkeypatch, wt, SID, 500)  # marker provenance defaults to armed
    assert ownership.record_ownership(
        repo=REPO, pr_number=500, session_id=SID, pid=LIVE_PID, worktree_path=wt,
        provenance=ownership.PROVENANCE_ADOPTED,
    ).ok

    owned = resolve_worktree(wt, kind="test_divergence")

    assert owned.provenance == ownership.PROVENANCE_ARMED
    assert any(d["reason"] == "different_provenance" for d in owned.divergences)


def test_marker_pr_wins_when_the_claim_names_a_superseded_pr(isolated_store):
    """The marker is rewritten when a branch is repointed at a new PR; an
    unreleased claim for the old PR is not."""
    wt = _mk(isolated_store, "superseded")
    assert ownership.record_ownership(
        repo=REPO, pr_number=600, session_id=SID, pid=LIVE_PID, worktree_path=wt,
    ).ok
    assert _write_arm_marker(wt, SID, LIVE_PID, 601)

    assert resolve_worktree(wt, kind="test_divergence").pr_number == 601


# ── resolve_worktree: "could not look" ≠ "no claims" ─────────────────────────


def test_unreadable_store_falls_back_entirely_to_the_marker(isolated_store):
    wt = _mk(isolated_store, "unreadable")
    assert _write_arm_marker(wt, SID, LIVE_PID, 700)

    owned = resolve_worktree(wt, kind="test_divergence", snap=_unknown_snap())

    assert owned.source == "marker"
    assert owned.pr_number == 700
    assert owned.owned_by(SID)
    assert [d["reason"] for d in owned.divergences] == ["claim_store_unreadable"]


def test_unowned_worktree_reports_source_none_without_divergence(isolated_store):
    wt = _mk(isolated_store, "nobody")

    owned = resolve_worktree(wt, kind="test_divergence")

    assert owned.source == "none"
    assert owned.pr_number is None
    assert not owned.owned_by(SID)
    # Nothing disagreed — an unowned worktree is not a divergence.
    assert owned.divergences == []


def test_claim_naming_a_deleted_worktree_is_not_resurrected(isolated_store):
    gone = os.path.join(str(isolated_store), "long-gone")  # never created on disk
    assert ownership.record_ownership(
        repo=REPO, pr_number=800, session_id=SID, pid=LIVE_PID, worktree_path=gone,
    ).ok

    owned = resolve_worktree(gone, kind="test_divergence")

    assert owned.source == "none"
    assert not owned.owned_by(SID)


# ── resolve_worktree: the kill switch ────────────────────────────────────────


@pytest.mark.parametrize("off_value", ["0", "false", "no", "off", "OFF"])
def test_kill_switch_reverts_to_pure_marker_reads(isolated_store, monkeypatch, off_value):
    wt = _mk(isolated_store, "killswitch")
    assert ownership.record_ownership(
        repo=REPO, pr_number=900, session_id=OTHER_SID, pid=LIVE_PID, worktree_path=wt,
    ).ok
    _arm_marker_only(monkeypatch, wt, SID, 901)
    monkeypatch.setenv("AGENTIC_PR_DASH_OWNERSHIP_CLAIM_READS", off_value)

    owned = resolve_worktree(wt, kind="test_divergence")

    assert owned.source == "marker"
    assert owned.pr_number == 901
    assert owned.owned_by(SID)
    # The claim holder is invisible with reads off, and nothing is logged.
    assert not owned.owned_by(OTHER_SID)
    assert owned.divergences == []


# ── Each flipped reader actually consults the claim ──────────────────────────


def test_worktrees_marker_pr_reads_a_claim_only_worktree(isolated_store):
    from agentic_pr_dash._maintenance import worktrees

    wt = _mk(isolated_store, "wt-pr")
    assert ownership.record_ownership(
        repo=REPO, pr_number=1100, session_id=SID, pid=LIVE_PID, worktree_path=wt,
    ).ok

    assert worktrees._marker_pr(wt) == "1100"


def test_worktree_is_for_entry_matches_via_claim(isolated_store):
    from agentic_pr_dash._maintenance.worktrees import _worktree_is_for_entry

    wt = _mk(isolated_store, "entry")
    assert ownership.record_ownership(
        repo=REPO, pr_number=1200, session_id=SID, pid=LIVE_PID, worktree_path=wt,
    ).ok
    entry = type("E", (), {"pr": 1200, "branch": "bou-x", "worktree": wt})()

    assert _worktree_is_for_entry(wt, entry)


def test_waiter_coverage_request_accepts_a_claim_only_worktree(isolated_store, monkeypatch):
    """``_request_waiter_coverage`` used to demand a marker naming this session;
    a claim this session holds is now equally sufficient."""
    from agentic_pr_dash._maintenance import waiter

    wt = _mk(isolated_store, "waiter-wt")
    assert ownership.record_ownership(
        repo=REPO, pr_number=1300, session_id=SID, pid=LIVE_PID, worktree_path=wt,
    ).ok
    seen: dict = {}
    monkeypatch.setattr(waiter, "_await_pidfile", lambda cwd, sid: str(Path(isolated_store) / "await.json"))
    Path(isolated_store, "await.json").write_text('{"pid": 1, "requested_roots": []}')
    monkeypatch.setattr(waiter, "_write_await_pidfile",
                        lambda cwd, data, sid: seen.update(data))

    assert waiter._request_waiter_coverage(wt, SID) is True
    assert os.path.abspath(wt) in seen["requested_roots"]


def test_waiter_coverage_request_rejects_a_foreign_worktree(isolated_store):
    from agentic_pr_dash._maintenance import waiter

    wt = _mk(isolated_store, "waiter-foreign")
    assert ownership.record_ownership(
        repo=REPO, pr_number=1301, session_id=OTHER_SID, pid=LIVE_PID, worktree_path=wt,
    ).ok

    assert waiter._request_waiter_coverage(wt, SID) is False


def test_ownership_card_prefers_the_claim_owner(isolated_store):
    from agentic_pr_dash.app import _ownership_for_card

    wt = _mk(isolated_store, "card")
    assert ownership.record_ownership(
        repo=REPO, pr_number=1400, session_id=SID, pid=LIVE_PID, worktree_path=wt,
    ).ok

    result = _ownership_for_card(wt, 1400, str(isolated_store))

    assert result["owner_session_id"] == SID
    assert result["owner_pid"] == LIVE_PID
    assert result["owner_pid_alive"] is True


def test_adoptions_scan_does_not_read_the_store_once_per_worktree(isolated_store, monkeypatch):
    """The store read must be batched per root, never per worktree.

    ``_marker_pr`` became claim-aware in Stage 3, and it is called once per
    worktree in ``before`` and again per worktree in ``root_owned``. Left
    unbatched that is a full ``claims.jsonl`` replay — an append-only log with
    no compaction (BOU-2239) — for every worktree, on the Stop hook's ~108s
    fail-closed budget (BOU-1953).
    """
    from agentic_pr_dash._maintenance import worktrees

    wts = [_mk(isolated_store, f"wt{i}") for i in range(5)]
    for i, wt in enumerate(wts):
        assert _write_arm_marker(wt, SID, LIVE_PID, 2000 + i)

    calls = {"n": 0}
    real_snapshot = ownership.snapshot

    def _counting_snapshot(*a, **kw):
        calls["n"] += 1
        return real_snapshot(*a, **kw)

    monkeypatch.setattr(ownership, "snapshot", _counting_snapshot)
    monkeypatch.setattr(worktrees, "_maint_roots_for", lambda cwd: [str(isolated_store)])
    monkeypatch.setattr(worktrees, "_collect_stop_gate_worktrees", lambda sid, root: list(wts))
    monkeypatch.setattr(worktrees, "_collect_owned_worktrees",
                        lambda sid, root, pid, deadline=None, **kw: list(wts))

    worktrees._reconcile_owned_across_roots(SID, str(isolated_store), LIVE_PID)

    # Two per root (a before and an after snapshot, which must stay distinct so
    # a mid-call ownership rewrite is still detected) — NOT two per worktree.
    assert calls["n"] <= 2, f"{calls['n']} store reads for {len(wts)} worktrees"


def test_ownership_card_never_raises_on_an_unreadable_store(isolated_store, monkeypatch):
    from agentic_pr_dash.app import _ownership_for_card

    wt = _mk(isolated_store, "card-broken")

    def _boom(*a, **kw):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(ownership, "snapshot", _boom)

    # Display-only: a claim-store problem must degrade, never propagate.
    assert isinstance(_ownership_for_card(wt, 1401, str(isolated_store)), dict)
