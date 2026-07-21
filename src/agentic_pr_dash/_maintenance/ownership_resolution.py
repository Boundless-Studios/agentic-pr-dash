"""Stop-gate ownership reads, flipped onto claims (BOU-2223 Stage 2).

Stage 1 (see ``docs/plans/bou-2223-stage1-ownership-adapter.md``) dual-wrote a
fenced ``agent-coordinator`` claim from every ``pr-watch.armed`` marker write,
with markers staying authoritative. This module is the Stage-2 flip: the stop
gate's three ownership questions —

1. which worktrees do I own,
2. what PR does each own, and
3. is it armed or adopted

— are now answered claim-first, with the marker as a fallback whenever the two
disagree. Markers are still the *write* authority; nothing here writes.

The claim store is keyed by ``(repo_slug, pr_number)`` but the stop gate
enumerates worktrees, so a claim cannot be looked up from a worktree path
without first knowing its PR — that would just be reading the marker again.
:meth:`agentic_pr_dash.ownership.OwnershipSnapshot.live_claims_for_session`
solves this the way Stage 1 designed it to: every claim carries the
``worktree_path`` it was dual-written with, so the reverse index falls out of
one full-store scan.

Design choices worth being explicit about (all "not negotiable" per the epic):

* **Union, never intersection.** The stop gate fails CLOSED (BOU-1953): losing
  a worktree from the owned set means a session stops while it still owns
  unaddressed PR work. A worktree owned by EITHER source is owned by the
  result, full stop.
* **A stale claim cannot resurrect a deleted worktree.** A claim's
  ``worktree_path`` is exactly whatever ``cwd`` was passed to
  ``record_ownership`` at dual-write time; nothing prunes it when the worktree
  is later removed. Before trusting a claim-only path, we require it to (a)
  still exist as a directory and (b) still appear in a live ``git worktree
  list`` from one of the stop gate's maintenance roots — restricting to the
  "candidate roots" the prompt calls for. A handful of extra ``git worktree
  list`` calls (one per maintenance root, not per worktree) is an acceptable
  cost next to the store read that already dominates this path.
* **Provenance disagreement resolves toward "armed", never toward "adopted".**
  Provenance decides whether the gate blocks (``armed``) or merely reports
  (``adopted``, BOU-2221). A false "adopted" would silently let the gate
  release ownership of a PR the session actually armed — a fail-OPEN outcome
  the epic's fail-closed posture forbids. So when the two sources disagree,
  the stricter answer wins; only when BOTH agree it was "adopted" does the
  result relax the gate. This is the same fail-closed instinct as the
  union rule above, just applied to a smaller pair of enum values.
* **An unusable snapshot means "fall back to markers entirely", not "no
  claims".** ``snap.known() is False`` (a lock timeout or unreadable store) is
  reported as ONE divergence explaining why, and every worktree/PR/provenance
  answer below comes straight from ``marker_owned`` — the kill-switch-off
  shape, but observed rather than configured.

One store read for the whole call. The stop gate has a hard ~108s deadline
(BOU-1953); :func:`resolve_owned` takes (or is given) exactly one
``ownership.snapshot()`` and answers every worktree from it — never one
snapshot per worktree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os


def claim_reads_enabled() -> bool:
    """Kill switch for the Stage-2 flip.

    On by default. ``AGENTIC_PR_DASH_OWNERSHIP_CLAIM_READS=0`` reverts the stop
    gate to pure marker reads (Stage 1 behaviour) without a package rollback —
    parsing mirrors :func:`agentic_pr_dash.ownership.dual_write_enabled`
    exactly.
    """
    raw = os.environ.get("AGENTIC_PR_DASH_OWNERSHIP_CLAIM_READS", "").strip().lower()
    return raw not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class OwnedResolution:
    """The stop gate's three ownership questions, answered in one pass.

    ``worktrees`` is the UNION of marker-owned and live-claim-owned paths.
    ``pr_for``/``provenance_for`` carry an entry ONLY for a worktree where a
    live CLAIM contributes an answer (source ``"claim"`` or ``"both"``) — a
    marker-only worktree is deliberately left out of both maps rather than
    populated from a marker read taken once for the whole resolution. The
    caller's own per-worktree marker read (taken fresh, at the point of use)
    is the real fallback for those, exactly as the "prefer the claim, fall
    back to the marker" contract promises; pre-filling it here would silently
    shadow that fallback with stale, unmockable data.
    """

    worktrees: list[str]
    pr_for: dict[str, int]
    provenance_for: dict[str, str]
    #: "claim" | "marker" | "both", per worktree.
    source_for: dict[str, str]
    divergences: list[dict] = field(default_factory=list)


def _resolve_provenance(marker_prov: str | None, claim_prov: str | None) -> str:
    """Fail-closed provenance merge: "armed" from EITHER side wins.

    See the module docstring for why — an erroneous "adopted" is the
    dangerous direction (it un-blocks a gate that should still be blocking).
    """
    if marker_prov == "armed" or claim_prov == "armed":
        return "armed"
    return claim_prov or marker_prov or "armed"


def _marker_pr_and_provenance(worktree: str) -> tuple[int | None, str | None]:
    from .markers import _marker_provenance, _read_marker  # noqa: PLC0415

    fields = _read_marker(worktree)
    if fields is None:
        return None, None
    pr_raw = fields.get("pr", "")
    pr = int(pr_raw) if str(pr_raw).isdigit() else None
    return pr, _marker_provenance(worktree)


def _valid_claim_worktrees(anchor_cwd: str) -> set[str]:
    """Worktree paths a claim is allowed to resurrect: still a directory AND
    still enumerated by ``git worktree list`` from one of the stop gate's
    maintenance roots. Bounds the "stale claim, deleted worktree" case."""
    from .worktrees import _iter_worktree_paths, _maint_roots_for  # noqa: PLC0415

    listed: set[str] = set()
    for root in _maint_roots_for(anchor_cwd):
        for path in _iter_worktree_paths(root):
            listed.add(os.path.abspath(path))
    return listed


def resolve_owned(
    session_id: str,
    anchor_cwd: str,
    marker_owned: list[str],
    *,
    snap=None,
) -> OwnedResolution:
    """Resolve the stop gate's owned-worktree set, claim-first with a marker
    fallback, logging every divergence (BOU-2223 Stage 2).

    ``marker_owned`` is the existing marker-derived list (unchanged input from
    ``worktrees._collect_stop_gate_worktrees``/``_owned_worktrees_across_roots``).
    ``snap`` lets a caller resolving several things from one Stop tick reuse a
    single :func:`agentic_pr_dash.ownership.snapshot` read; when omitted, this
    function takes exactly one.
    """
    marker_owned_norm = list(dict.fromkeys(os.path.abspath(p) for p in marker_owned))
    marker_set = set(marker_owned_norm)

    if not claim_reads_enabled():
        return OwnedResolution(
            worktrees=list(marker_owned_norm),
            pr_for={},
            provenance_for={},
            source_for={wt: "marker" for wt in marker_owned_norm},
        )

    from agentic_pr_dash import ownership, ownership_parity  # noqa: PLC0415

    snapshot = snap if snap is not None else ownership.snapshot()

    def _log(worktree: str, payload: dict) -> dict:
        """Log one divergence and return the wrapped record, so the returned
        ``OwnedResolution.divergences`` and the on-disk log agree byte-for-byte
        on shape."""
        record = {"kind": "stop_gate_divergence", **payload}
        ownership_parity.log_divergence(worktree, record)
        return record

    if not snapshot.known():
        # The store could not be read at all (unreadable file or a lock
        # timeout) — this is NOT "no claims", it is "we could not look". Fall
        # back entirely to the marker set, exactly as if the kill switch were
        # off, and log one divergence explaining why on the anchor (there is
        # no single worktree this failure is "about").
        record = _log(
            anchor_cwd,
            {
                "reason": "claim_store_unreadable",
                "session_id": session_id,
                "marker_owned_count": len(marker_owned_norm),
            },
        )
        return OwnedResolution(
            worktrees=list(marker_owned_norm),
            pr_for={},
            provenance_for={},
            source_for={wt: "marker" for wt in marker_owned_norm},
            divergences=[record],
        )

    # Reverse index: worktree_path -> live claim view. The "still exists AND
    # still part of this repo pool's worktree list" guard is required ONLY for
    # a worktree the claim would otherwise ADD to the owned set (claim-only,
    # not already in marker_owned) — that is the literal "resurrect a deleted
    # worktree" hazard the prompt calls out. A path already in marker_owned is
    # already trusted (whatever enumerated it already established it is real),
    # so gating it on a SECOND, independent `git worktree list` probe would
    # only produce false "marker_only" divergences wherever that probe can't
    # run (e.g. an anchor that isn't a real git checkout in a test harness)
    # without changing the owned set either way.
    valid_paths = _valid_claim_worktrees(anchor_cwd)
    by_path: dict[str, list] = {}
    for view in snapshot.live_claims_for_session(session_id):
        if not view.worktree_path:
            continue
        path = os.path.abspath(view.worktree_path)
        if path not in marker_set and (path not in valid_paths or not os.path.isdir(path)):
            continue
        by_path.setdefault(path, []).append(view)

    claim_views: dict = {}
    for path, views in by_path.items():
        if len(views) == 1:
            claim_views[path] = views[0]
            continue
        # More than one live claim names the SAME worktree_path — possible
        # when a worktree's PR was superseded (branch reassigned to a new open
        # PR) without the OLD PR's claim ever being released; dual-write only
        # releases on merge/close (``_prune_stale_marker``), not on rewrite.
        # Prefer whichever claim matches the marker's own current PR (the
        # marker is still the freshest signal of "which PR is this worktree
        # for right now"); failing that, the highest lease epoch is the best
        # available proxy for "most recently (re)claimed".
        marker_pr, _ = _marker_pr_and_provenance(path) if path in marker_set else (None, None)
        match = next((v for v in views if v.pr_number == marker_pr), None) if marker_pr is not None else None
        claim_views[path] = match or max(views, key=lambda v: v.lease_epoch)

    all_paths = list(dict.fromkeys([*marker_owned_norm, *claim_views.keys()]))

    worktrees: list[str] = []
    pr_for: dict[str, int] = {}
    provenance_for: dict[str, str] = {}
    source_for: dict[str, str] = {}
    divergences: list[dict] = []

    for wt in all_paths:
        in_marker = wt in marker_set
        claim_view = claim_views.get(wt)
        in_claim = claim_view is not None

        source_for[wt] = "both" if (in_marker and in_claim) else ("claim" if in_claim else "marker")
        worktrees.append(wt)

        marker_pr, marker_prov = _marker_pr_and_provenance(wt) if in_marker else (None, None)

        # Only populate pr_for/provenance_for when a CLAIM actually contributes
        # an answer. A marker-only worktree has nothing for the claim side to
        # "prefer" — leaving it absent here means the caller's own (fresh,
        # per-worktree) marker read is the real fallback, exactly as promised,
        # rather than a second, independently-timed read taken this whole
        # resolution ago silently shadowing it every time.
        if in_claim:
            resolved_pr = claim_view.pr_number if claim_view.pr_number is not None else marker_pr
            if resolved_pr is not None:
                pr_for[wt] = resolved_pr
            provenance_for[wt] = _resolve_provenance(marker_prov, claim_view.provenance)

        if in_marker and not in_claim:
            divergences.append(_log(wt, {
                "reason": "marker_only_worktree",
                "worktree": wt,
                "session_id": session_id,
                "marker_pr": marker_pr,
                "marker_provenance": marker_prov,
            }))
        elif in_claim and not in_marker:
            divergences.append(_log(wt, {
                "reason": "claim_only_worktree",
                "worktree": wt,
                "session_id": session_id,
                "claim_pr": claim_view.pr_number,
                "claim_provenance": claim_view.provenance,
            }))
        else:
            if marker_pr is not None and claim_view.pr_number is not None and marker_pr != claim_view.pr_number:
                divergences.append(_log(wt, {
                    "reason": "different_pr_number",
                    "worktree": wt,
                    "session_id": session_id,
                    "marker_pr": marker_pr,
                    "claim_pr": claim_view.pr_number,
                }))
            if marker_prov and claim_view.provenance and marker_prov != claim_view.provenance:
                divergences.append(_log(wt, {
                    "reason": "different_provenance",
                    "worktree": wt,
                    "session_id": session_id,
                    "marker_provenance": marker_prov,
                    "claim_provenance": claim_view.provenance,
                }))

    return OwnedResolution(
        worktrees=worktrees,
        pr_for=pr_for,
        provenance_for=provenance_for,
        source_for=source_for,
        divergences=divergences,
    )
