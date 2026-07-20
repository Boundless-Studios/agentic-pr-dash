"""Marker-vs-claim ownership parity (BOU-2223 Stage 1).

``agentic-pr-dash`` runs two ownership systems: fenced ``agent-coordinator`` claims
and a hand-rolled ``pr-watch.armed`` marker scheme. Stage 1 dual-writes a claim from
every marker write; these tests assert the claim-derived view reproduces the
marker-derived one across the behaviours the rest of the suite already encodes.

Markers remain authoritative here — nothing below asserts a *behaviour* change. The
point is that when Stage 2 flips the stop gate onto claims, it reads the same
answers, including the awkward ones (a live owner with a long-stale heartbeat; the
same PR number in two repos; a released claim after a merged PR).
"""

from datetime import datetime, timedelta, timezone
import os

import pytest

from agentic_pr_dash import ownership, ownership_parity
from agentic_pr_dash._maintenance.markers import (
    _prune_stale_marker,
    _read_marker,
    _touch_owner_heartbeat,
    _write_arm_marker,
    marker_owner_view,
)

SID = "sess-owner"
OTHER_SID = "sess-other"
LIVE_PID = os.getpid()
DEAD_PID = 999999
REPO = "Boundless-Studios/agentic-pr-dash"
OTHER_REPO = "Boundless-Studios/gaia-free"


@pytest.fixture
def worktree(tmp_path, monkeypatch):
    """A worktree whose state dir and repo slug are hermetic.

    ``_repo_slug`` resolves through ``config._detect_repo``; patching there covers
    both call sites (the marker dual-write and the parity comparison) without
    either module having to expose a seam.
    """
    from agentic_pr_dash import config

    monkeypatch.setenv("AGENTIC_PR_DASH_STATE_DIR", str(tmp_path / ".gaia"))
    monkeypatch.setattr(config, "_detect_repo", lambda path: REPO)
    wt = tmp_path / "wt"
    wt.mkdir()
    return str(wt)


def _now():
    return datetime.now(timezone.utc)


def _set_marker_fields(worktree_path, **updates):
    """Rewrite marker fields in place, mimicking an aged/foreign marker on disk."""
    from agentic_pr_dash._maintenance.markers import _marker_path

    fields = _read_marker(worktree_path) or {}
    fields.update({k: str(v) for k, v in updates.items()})
    with open(_marker_path(worktree_path), "w", encoding="utf-8") as fh:
        fh.write("".join(f"{k}={v}\n" for k, v in fields.items()))


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── The dual-write itself ────────────────────────────────────────────────────


def test_arm_records_a_claim_with_armed_provenance(worktree):
    """An explicit arm mirrors into a fenced claim owned by the same session."""
    assert _write_arm_marker(worktree, SID, LIVE_PID, 111)

    view = ownership.snapshot().owner_for(REPO, 111)
    assert view is not None, "arming a PR must record an ownership claim"
    assert view.session_id == SID
    assert view.pid == LIVE_PID
    assert view.provenance == ownership.PROVENANCE_ARMED
    assert view.live is True


def test_adoption_provenance_is_intrinsic_to_the_claim(worktree):
    """BOU-2221's bespoke ``provenance=`` marker field is subsumed by claim metadata.

    The stop gate must keep being able to tell a PR this session opened from one it
    inherited — after the migration that distinction lives in ``OwnerIdentity``.
    """
    assert _write_arm_marker(worktree, SID, LIVE_PID, 222, provenance="adopted")

    view = ownership.snapshot().owner_for(REPO, 222)
    assert view is not None
    assert view.provenance == ownership.PROVENANCE_ADOPTED
    assert view.metadata["provenance"] == "adopted"


def test_claim_task_id_is_repo_qualified(worktree, monkeypatch):
    """The same PR number in two repos must not collide.

    Owned worktrees can span several ``maintenance_repo_roots``; the marker scheme
    needs a strict repo match in the ledger to keep them apart, and the claim
    identity has to carry the same distinction.
    """
    from agentic_pr_dash import config

    assert _write_arm_marker(worktree, SID, LIVE_PID, 500)
    monkeypatch.setattr(config, "_detect_repo", lambda path: OTHER_REPO)
    assert _write_arm_marker(worktree, OTHER_SID, LIVE_PID, 500)

    snap = ownership.snapshot()
    assert snap.owner_for(REPO, 500).session_id == SID
    assert snap.owner_for(OTHER_REPO, 500).session_id == OTHER_SID


def test_dual_write_can_be_disabled(worktree, monkeypatch):
    """The bake-period kill switch takes the claim store out of the write path."""
    monkeypatch.setenv("AGENTIC_PR_DASH_OWNERSHIP_DUAL_WRITE", "0")

    assert _write_arm_marker(worktree, SID, LIVE_PID, 123)

    assert _read_marker(worktree) is not None, "the marker write must be unaffected"
    assert ownership.snapshot().owner_for(REPO, 123) is None


# ── Liveness parity: the three marker tiers ──────────────────────────────────


def test_fresh_heartbeat_is_live_on_both_sides(worktree):
    assert _write_arm_marker(worktree, SID, LIVE_PID, 300)
    _touch_owner_heartbeat(worktree, SID, work_found=False)

    result = ownership_parity.compare_worktree(worktree)
    assert result.marker_live and result.claim_live
    assert result.agrees, result.reason


def test_stale_heartbeat_with_live_pid_stays_owned_on_both_sides(worktree):
    """The tier that ``TaskCoordinator.status()`` alone would get wrong.

    A session that owns a PR but runs no waiter stops heartbeating while remaining
    very much alive. The marker scheme keeps its ownership via the owner-pid
    fallback; a bare ``status()`` would report EXPIRED ⇒ reclaimable and let the
    machine-wide loop claim the PR out from under a live session.
    """
    assert _write_arm_marker(worktree, SID, LIVE_PID, 301)
    stale = _iso(_now() - timedelta(hours=6))
    _set_marker_fields(worktree, heartbeat=stale, last_heartbeat=stale)

    marker = marker_owner_view(worktree)
    assert marker["live"] is True, "marker keeps ownership for a live owner pid"

    # Age the claim past its lease the same way.
    future = _now() + timedelta(hours=6)
    view = ownership.snapshot(now=future).owner_for(REPO, 301)
    assert view.state == "owner_pid_live"
    assert view.live is True, (
        "an expired lease with a live owner pid must stay owned, matching the "
        "marker's third liveness tier"
    )
    assert ownership_parity.compare_worktree(worktree, now=future).agrees


def test_stale_heartbeat_with_dead_pid_is_reclaimable_on_both_sides(worktree):
    assert _write_arm_marker(worktree, SID, DEAD_PID, 302)
    stale = _iso(_now() - timedelta(hours=6))
    _set_marker_fields(worktree, heartbeat=stale, last_heartbeat=stale)

    assert marker_owner_view(worktree)["live"] is False

    future = _now() + timedelta(hours=6)
    view = ownership.snapshot(now=future).owner_for(REPO, 302)
    assert view.state == "expired"
    assert view.live is False
    assert ownership_parity.compare_worktree(worktree, now=future).agrees


def test_fresh_heartbeat_with_dead_pid_stays_owned_on_both_sides(worktree):
    """A fresh heartbeat grants ownership even when the recorded pid is dead.

    ``test_dead_pid_with_fresh_heartbeat_still_defers`` in test_foreign_owner.py is
    the specification: the marker's ``pid`` is stamped once at arm time and never
    rewritten, while a detached waiter or the maintenance loop keeps heartbeating
    under the same session id — so a dead recorded pid does NOT mean the owner is
    gone. ``TaskCoordinator.status()`` would call this OWNER_DEAD; the façade must
    not.
    """
    assert _write_arm_marker(worktree, SID, DEAD_PID, 303)
    _touch_owner_heartbeat(worktree, SID, work_found=False)

    assert marker_owner_view(worktree)["live"] is True

    view = ownership.snapshot().owner_for(REPO, 303)
    assert view.state == "active"
    assert view.live is True, (
        "an unexpired lease must not be overridden by a dead recorded pid — the "
        "marker's heartbeat tier never consults it"
    )
    assert ownership_parity.compare_worktree(worktree).agrees


def test_a_bare_arm_rests_on_the_pid_tier_on_both_sides(worktree):
    """``_write_arm_marker`` writes no heartbeat and no fix-lease.

    Such a marker satisfies neither time-based liveness tier, so its ownership
    rests entirely on the owner pid — and the mirrored claim must start in the same
    state. Giving the arm a full heartbeat-TTL lease instead would make a session
    that arms and then dies before its first heartbeat look owned on the claim side
    while the marker reports it reclaimable.
    """
    assert _write_arm_marker(worktree, SID, DEAD_PID, 308)

    assert marker_owner_view(worktree)["live"] is False
    view = ownership.snapshot().owner_for(REPO, 308)
    assert view.live is False, "a bare arm with a dead pid must not look owned"
    assert ownership_parity.compare_worktree(worktree).agrees

    # ...and the same arm with a LIVE pid is owned on both sides, via that tier.
    assert _write_arm_marker(worktree, SID, LIVE_PID, 309)
    assert marker_owner_view(worktree)["live"] is True
    live_view = ownership.snapshot().owner_for(REPO, 309)
    assert live_view.state == "owner_pid_live"
    assert live_view.live is True
    assert ownership_parity.compare_worktree(worktree).agrees


def test_corrupt_fix_lease_does_not_grant_ownership_forever(worktree):
    """Mirrors ``test_corrupt_fix_lease_without_fresh_heartbeat_does_not_defer_forever``.

    ``_fix_lease_active`` treats an unparseable lease as ACTIVE — correct where it
    guards a dispatch race, wrong for ownership, where ``_live_foreign_owner`` falls
    through to the pid tier instead. The parity harness must mirror the ownership
    reader, or it would report agreement on a marker the real reader disowns.
    """
    assert _write_arm_marker(worktree, SID, DEAD_PID, 306)
    _set_marker_fields(
        worktree,
        heartbeat=_iso(_now() - timedelta(hours=6)),
        last_heartbeat=_iso(_now() - timedelta(hours=6)),
        fix_lease_until="not-a-timestamp",
    )

    assert marker_owner_view(worktree)["live"] is False, (
        "a corrupt fix-lease must not confer ownership on a dead owner"
    )


def test_oversized_marker_pid_is_rejected_not_smuggled_into_the_claim(worktree):
    """A corrupt marker pid must not reach ``OwnerIdentity``.

    ``_pid_alive`` rejects >10-digit pids explicitly (they overflow ``os.kill``'s
    pid_t); the ownership view applies the same guard rather than relying on
    ``os.kill`` to fail incidentally.
    """
    assert _write_arm_marker(worktree, SID, LIVE_PID, 307)
    _set_marker_fields(worktree, pid="1" * 40)

    assert marker_owner_view(worktree)["pid"] is None


def test_fix_lease_extends_the_claim_lease(worktree):
    """``work_found=True`` writes the long fix-lease on both sides, so ownership
    survives well past the short heartbeat TTL."""
    assert _write_arm_marker(worktree, SID, LIVE_PID, 304)
    _touch_owner_heartbeat(worktree, SID, work_found=True)

    marker = _read_marker(worktree)
    assert "fix_lease_until" in marker

    from agentic_pr_dash._maintenance._common import _fix_lease_seconds
    from agentic_pr_dash._maintenance.markers import _heartbeat_ttl_seconds

    assert _fix_lease_seconds() > _heartbeat_ttl_seconds(worktree), (
        "this test is only meaningful while the fix lease outlasts the heartbeat TTL"
    )
    just_past_heartbeat_ttl = _now() + timedelta(
        seconds=_heartbeat_ttl_seconds(worktree) + 30
    )
    view = ownership.snapshot(now=just_past_heartbeat_ttl).owner_for(REPO, 304)
    assert view.state == "active", "the fix lease must still be running"
    assert view.live is True


def test_heartbeat_from_a_foreign_session_does_not_move_the_claim(worktree):
    """``_touch_owner_heartbeat`` is an owner-only write; the claim must match."""
    assert _write_arm_marker(worktree, SID, LIVE_PID, 305)
    _touch_owner_heartbeat(worktree, OTHER_SID, work_found=True)

    view = ownership.snapshot().owner_for(REPO, 305)
    assert view.session_id == SID
    assert "fix_lease_until" not in (_read_marker(worktree) or {})


# ── Conflict + release ───────────────────────────────────────────────────────


def test_foreign_active_claim_is_a_divergence_not_a_marker_failure(worktree):
    """Markers are authoritative in Stage 1.

    When a foreign session already holds the claim, the marker write must still
    succeed — but the disagreement has to be recorded, because it is exactly the
    signal that decides whether Stage 2 is safe.
    """
    ownership.record_ownership(
        repo=REPO,
        pr_number=400,
        session_id=OTHER_SID,
        pid=LIVE_PID,
        worktree_path=worktree,
    )

    assert _write_arm_marker(worktree, SID, LIVE_PID, 400), (
        "a claim conflict must never fail the authoritative marker write"
    )
    assert (_read_marker(worktree) or {}).get("session_id") == SID

    divergences = ownership_parity.read_divergences(worktree)
    assert any(d.get("kind") == "dual_write_failed" for d in divergences)
    assert any(d.get("conflict_session_id") == OTHER_SID for d in divergences)

    result = ownership_parity.compare_worktree(worktree)
    assert not result.agrees
    assert result.reason == "different owning session"


def test_pruning_a_merged_pr_releases_the_claim(worktree, monkeypatch):
    """A merged PR's marker is removed; the mirrored claim must not outlive it."""
    assert _write_arm_marker(worktree, SID, LIVE_PID, 500)
    assert ownership.snapshot().live_owner_for(REPO, 500) is not None

    # _prune_stale_marker imports _pr_open_state lazily from pr_state, so that is
    # the module to patch.
    monkeypatch.setattr(
        "agentic_pr_dash._maintenance.pr_state._pr_open_state",
        lambda pr, cwd: ("merged", None),
    )
    _prune_stale_marker(worktree, {"pr": "500"}, SID)

    assert _read_marker(worktree) is None
    assert ownership.snapshot().live_owner_for(REPO, 500) is None, (
        "a released claim must stop reporting a live owner"
    )
    assert ownership_parity.compare_worktree(worktree).agrees


def test_release_by_a_foreign_session_is_refused(worktree):
    assert _write_arm_marker(worktree, SID, LIVE_PID, 501)

    outcome = ownership.release_ownership(
        repo=REPO, pr_number=501, session_id=OTHER_SID
    )
    assert not outcome.ok
    assert outcome.conflict_session_id == SID
    assert ownership.snapshot().live_owner_for(REPO, 501) is not None


# ── Fail-closed reads ────────────────────────────────────────────────────────


def test_unreadable_store_reports_unknown_not_unowned(worktree, monkeypatch):
    """The stop gate is on a hard-deadline hook and must fail closed.

    A snapshot whose backing read failed answers ``None`` for every PR — which is
    indistinguishable from "unowned" unless it also says so. ``known()`` is that
    distinction, and every Stage-2 reader is required to consult it.
    """
    assert _write_arm_marker(worktree, SID, LIVE_PID, 600)

    def _boom():
        raise OSError("claim store unavailable")

    monkeypatch.setattr(
        "agent_coordinator.service.TaskCoordinator._claims_by_id",
        lambda self, events=None: _boom(),
    )

    snap = ownership.snapshot()
    assert snap.known() is False
    assert snap.owner_for(REPO, 600) is None
    assert ownership_parity.compare_worktree(worktree, snap=snap).reason == (
        "claim_store_unreadable"
    )


def test_snapshot_answers_many_prs_from_one_store_read(worktree, monkeypatch):
    """The claim store is read in full under a lock, so per-PR ``status()`` calls
    do not fit the Stop hook's deadline. One read must serve every query."""
    for pr in (700, 701, 702):
        assert _write_arm_marker(worktree, SID, LIVE_PID, pr)

    reads = {"n": 0}
    real = ownership._coordinator

    def counting():
        reads["n"] += 1
        return real()

    monkeypatch.setattr(ownership, "_coordinator", counting)
    snap = ownership.snapshot()
    for pr in (700, 701, 702):
        assert snap.owner_for(REPO, pr).session_id == SID

    assert reads["n"] == 1, "resolving three PRs must not re-read the store"


def test_ownership_claims_do_not_collide_with_dispatch_claims(worktree):
    """``coordinator.py``'s dispatch claims share the store but key on the PR's
    blocker fingerprint; ownership must be blind to them."""
    from agent_coordinator.models import OwnerIdentity, TaskIdentity
    from agent_coordinator.service import TaskCoordinator
    from agent_coordinator.store import JsonlClaimStore
    from agentic_pr_dash import coordinator

    assert _write_arm_marker(worktree, SID, LIVE_PID, 800)

    TaskCoordinator(JsonlClaimStore(coordinator.store_path())).claim_task(
        TaskIdentity(
            task_type=coordinator.TASK_TYPE,
            task_id=f"github:{REPO}#800",
            fingerprint="some-blocker-hash",
        ),
        OwnerIdentity(session_id=OTHER_SID, pid=LIVE_PID),
        lease_seconds=600,
    )

    view = ownership.snapshot().owner_for(REPO, 800)
    assert view.session_id == SID, (
        "a dispatch claim on the same PR must not be mistaken for ownership"
    )
