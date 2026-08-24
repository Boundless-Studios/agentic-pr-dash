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

import os
import time
from dataclasses import dataclass, field


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


def _marker_session_id(worktree: str) -> str | None:
    from .markers import _marker_session_id as read  # noqa: PLC0415

    return read(worktree) or None


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


@dataclass(frozen=True)
class WorktreeOwnership:
    """Who owns ONE worktree, resolved claim-first (BOU-2223 Stage 3).

    :func:`resolve_owned` answers the stop gate's set question ("which worktrees
    do I own"). Every other reader asks the transpose about a path it already
    holds, so this is the shape they share.

    ``session_id``/``pr_number``/``provenance`` are the merged best answer, for
    display and for "which PR is this". Do NOT open-code an ownership test
    against ``session_id`` — use :meth:`owned_by`, which applies the union rule.
    """

    worktree: str
    session_id: str | None
    pr_number: int | None
    provenance: str | None
    #: "claim" | "marker" | "both" | "none"
    source: str
    owner_pid: int | None = None
    lease_epoch: int | None = None
    state: str | None = None
    #: Session ids that any live claim on this worktree is held by.
    claim_session_ids: tuple[str, ...] = ()
    marker_session_id: str | None = None
    divergences: list[dict] = field(default_factory=list)
    #: True when ownership is genuinely UNRESOLVABLE this call — the claim store
    #: could not be read (a lock timeout or corrupt log) AND no marker exists to
    #: fall back on. Distinct from ``source == "none"``, which means BOTH sources
    #: were actually consulted and agree nobody owns this worktree. Collapsing the
    #: two let a transient claim-store hiccup read as "unowned" and let a sibling
    #: session (or the detached loop) adopt or dispatch against a PR someone else
    #: actually holds. Callers that gate ADOPTION or TAKEOVER must check this
    #: first and fail closed (decline) rather than fall through to a "none" path;
    #: callers that only answer the stop gate's "which worktrees do I own" question
    #: already fail closed via ``resolve_owned``'s marker-only fallback and do not
    #: need this flag.
    unknown: bool = False

    def owned_by(self, session_id: str) -> bool:
        """Union rule: ``session_id`` owns this worktree if EITHER source says so.

        Never an intersection. Both call sites that ask this — the stop gate's
        passive worktree collection and the waiter's coverage request — decide
        whether a session may stop or stay covered, and both fail CLOSED
        (BOU-1953): dropping a worktree means a session walks away from work it
        still owns. A claim the marker hasn't caught up to, or a marker whose
        dual-write predates Stage 1, must each be enough on its own.
        """
        if not session_id:
            return False
        return session_id == self.marker_session_id or session_id in self.claim_session_ids


@dataclass(frozen=True)
class CurrentPRResolution:
    """The current branch/PR answer shared by the stop gate and waiter.

    ``resolved`` means the worktree branch was known and GitHub returned a
    definite open-PR answer (including "no open PR"). ``unknown`` is separate:
    a stale cache or rate-limited lookup must not be mistaken for an unbound
    worktree or a clean stop. ``stale_pr_number`` is retained only for pruning
    and diagnostics; callers must never watch it after the branch moved on.
    """

    worktree: str
    branch: str | None
    pr_number: int | None
    head_sha: str = ""
    is_draft: bool = False
    resolved: bool = False
    unknown: bool = False
    stale_pr_number: int | None = None


def _current_head(worktree_path: str) -> str:
    """Return the checked-out HEAD, or an empty string on local git failure."""
    import subprocess  # noqa: PLC0415

    try:
        result = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def resolve_current_pr(
    worktree_path: str,
    *,
    session_id: str = "",
    kind: str = "pr_watch_divergence",
    owner: WorktreeOwnership | None = None,
    snap=None,
    deadline: float | None = None,
) -> CurrentPRResolution:
    """Resolve one owned worktree to its exact current branch/PR.

    Ownership is established from the session/worktree claim first. Only then
    is the checked-out branch (and local HEAD when available) used to select an
    open PR. This prevents an author-wide PR list from binding a session to a
    sibling worktree and lets the waiter follow a same-worktree PR replacement.
    """
    path = os.path.abspath(worktree_path)
    resolved_owner = owner or resolve_worktree(path, kind=kind, snap=snap)
    owns = getattr(resolved_owner, "owned_by", None)
    if session_id and callable(owns) and not owns(session_id):
        return CurrentPRResolution(path, None, None)

    from ._common import _current_branch  # noqa: PLC0415
    from .pr_state import _GH_UNAVAILABLE, _resolve_pr_entry_for_branch  # noqa: PLC0415

    branch = _current_branch(path)
    stale_pr = resolved_owner.pr_number
    if not branch:
        return CurrentPRResolution(path, None, None, stale_pr_number=stale_pr)

    head_sha = _current_head(path)
    # A cold lookup failure is just as unobservable as a warm failure. In
    # particular, an ambiguous same-branch result must not fall through to the
    # legacy branch resolver, which may choose an arbitrary PR and then prune
    # or watch stale ownership.
    entry = _resolve_pr_entry_for_branch(
        path,
        branch,
        head_oid=head_sha,
        validate_snapshot_state=True,
        deadline=deadline,
    )
    if entry is _GH_UNAVAILABLE:
        return CurrentPRResolution(
            path,
            branch,
            None,
            head_sha=head_sha,
            resolved=True,
            unknown=True,
            stale_pr_number=stale_pr,
        )
    if entry is None:
        return CurrentPRResolution(
            path,
            branch,
            None,
            head_sha=head_sha,
            resolved=True,
            stale_pr_number=stale_pr,
        )

    number = int(entry["number"])
    return CurrentPRResolution(
        path,
        branch,
        number,
        # Keep the local checkout identity separate from the remote PR head.
        # A force-push or unpushed local commit is a checkout change, not a
        # reason to rewrite the identity used by the stop-gate race check.
        head_sha=head_sha,
        is_draft=bool(entry.get("isDraft", False)),
        resolved=True,
        stale_pr_number=(stale_pr if stale_pr is not None and stale_pr != number else None),
    )


def resolve_current_prs(
    worktrees: list[str],
    session_id: str = "",
    *,
    kind: str = "pr_watch_divergence",
    snap=None,
    deadline: float | None = None,
) -> dict[str, CurrentPRResolution]:
    """Resolve all worktrees from one ownership snapshot and current PR pass."""
    out: dict[str, CurrentPRResolution] = {}
    for worktree in dict.fromkeys(os.path.abspath(path) for path in worktrees):
        if deadline is not None and time.monotonic() >= deadline:
            out[worktree] = CurrentPRResolution(
                worktree, None, None, resolved=True, unknown=True
            )
            continue
        owner = resolve_worktree(worktree, kind=kind, snap=snap)
        owns = getattr(owner, "owned_by", None)
        if session_id and callable(owns) and not owns(session_id):
            continue
        out[worktree] = resolve_current_pr(
            worktree,
            session_id=session_id,
            kind=kind,
            owner=owner,
            snap=snap,
            deadline=deadline,
        )
    return out


def resolve_worktree(
    worktree_path: str,
    *,
    kind: str,
    snap=None,
) -> WorktreeOwnership:
    """Resolve one worktree's ownership, claim-first with a marker fallback.

    ``kind`` names the calling reader in the divergence log
    (``reconcile_divergence``, ``waiter_divergence``, ...) so a Stage 4 bake can
    attribute a disagreement to the call path that saw it, rather than to one
    undifferentiated pile.

    ``snap`` MUST be passed by any caller resolving more than one worktree: the
    snapshot is a full store read, and the Stop hook's ~108s deadline is a
    fail-closed budget that one-read-per-worktree would blow.
    """
    path = os.path.abspath(worktree_path)
    marker_pr, marker_prov = _marker_pr_and_provenance(path)
    marker_session = _marker_session_id(path)

    def _marker_only(
        source: str, divergences: list[dict] | None = None, *, unknown: bool = False,
    ) -> WorktreeOwnership:
        return WorktreeOwnership(
            worktree=path,
            session_id=marker_session,
            pr_number=marker_pr,
            provenance=marker_prov,
            source=source,
            marker_session_id=marker_session,
            divergences=divergences or [],
            unknown=unknown,
        )

    if not claim_reads_enabled():
        return _marker_only("marker" if marker_session or marker_pr else "none")

    from agentic_pr_dash import ownership, ownership_parity  # noqa: PLC0415

    snapshot = snap if snap is not None else ownership.snapshot()

    def _log(payload: dict) -> dict:
        record = {"kind": kind, "worktree": path, **payload}
        ownership_parity.log_divergence(path, record)
        return record

    if not snapshot.known():
        # "Could not look", NOT "no claims" — fall back entirely to the marker,
        # exactly as the kill-switch-off shape, and say why once.
        #
        # When a marker DOES exist it is still a trustworthy, store-independent
        # answer, so this stays "marker" exactly as before. When it does NOT,
        # the honest answer is "we do not know" — NOT "none" (genuinely unowned,
        # both sources consulted and agree). Reading an unreadable store as
        # "none" is precisely the P1 adoption-theft defect this fixes: a
        # transient lock timeout on the shared claim store would otherwise look
        # identical to a real absence of ownership, and a sibling
        # session/the detached loop would adopt or dispatch against a PR someone
        # else actually holds. ``unknown=True`` lets adoption/takeover callers
        # fail closed on exactly this case (see the field docstring); callers
        # that don't check it keep today's fallback behavior unchanged.
        no_marker = not marker_session and marker_pr is None
        return _marker_only(
            "marker" if marker_session or marker_pr else "none",
            [_log({"reason": "claim_store_unreadable", "marker_pr": marker_pr})],
            unknown=no_marker,
        )

    views = snapshot.live_claims_for_worktree(path)
    # A claim must never resurrect a worktree that is gone. A path with a marker
    # was established as real by whoever enumerated it; a claim-only path has no
    # such witness, so it has to still be a directory.
    if views and marker_session is None and marker_pr is None and not os.path.isdir(path):
        views = []

    if not views:
        divergences = []
        if marker_session or marker_pr is not None:
            divergences.append(_log({
                "reason": "marker_only_worktree",
                "marker_pr": marker_pr,
                "marker_provenance": marker_prov,
                "marker_session_id": marker_session,
            }))
        return _marker_only("marker" if (marker_session or marker_pr is not None) else "none",
                            divergences)

    # Same tiebreak as resolve_owned: the marker is the freshest signal of which
    # PR this worktree is for right now; failing that, the highest lease epoch is
    # the best proxy for "most recently (re)claimed".
    match = next((v for v in views if v.pr_number == marker_pr), None) if marker_pr is not None else None
    view = match or max(views, key=lambda v: v.lease_epoch)

    divergences = []
    if marker_session is None and marker_pr is None:
        divergences.append(_log({
            "reason": "claim_only_worktree",
            "claim_pr": view.pr_number,
            "claim_provenance": view.provenance,
            "claim_session_id": view.session_id,
        }))
    else:
        if marker_pr is not None and view.pr_number is not None and marker_pr != view.pr_number:
            divergences.append(_log({
                "reason": "different_pr_number",
                "marker_pr": marker_pr,
                "claim_pr": view.pr_number,
            }))
        if marker_session and view.session_id and marker_session != view.session_id:
            divergences.append(_log({
                "reason": "different_session_id",
                "marker_session_id": marker_session,
                "claim_session_id": view.session_id,
            }))
        if marker_prov and view.provenance and marker_prov != view.provenance:
            divergences.append(_log({
                "reason": "different_provenance",
                "marker_provenance": marker_prov,
                "claim_provenance": view.provenance,
            }))

    has_marker = marker_session is not None or marker_pr is not None
    return WorktreeOwnership(
        worktree=path,
        session_id=view.session_id or marker_session,
        pr_number=view.pr_number if view.pr_number is not None else marker_pr,
        provenance=_resolve_provenance(marker_prov, view.provenance),
        source="both" if has_marker else "claim",
        owner_pid=view.pid,
        lease_epoch=view.lease_epoch,
        state=view.state,
        claim_session_ids=tuple(dict.fromkeys(v.session_id for v in views if v.session_id)),
        marker_session_id=marker_session,
        divergences=divergences,
    )


def live_foreign_claim(
    worktree_path: str,
    self_session_id: str,
    *,
    kind: str,
    snap=None,
) -> bool:
    """True when a LIVE claim on ``worktree_path`` is held by another session.

    The claim-side half of the "is someone else working here?" question. Every
    caller unions this with its existing marker check
    (``_marker_live_foreign_pid`` / ``_live_foreign_owner``) rather than replacing
    it: either source reporting a live foreign owner is enough to keep hands off,
    which is the precedent Stage 3 set in
    ``worktrees._collect_owned_worktrees`` (BOU-2223).

    Retiring the marker writer in Stage 4 is what makes this necessary — the
    marker-only checks silently degrade to "nobody owns this" once the file stops
    being written, which would let the detached loop, the headless dashboard and a
    live in-session owner all dispatch against the same PR.

    ``self_session_id`` may be ``""`` for a caller with no session of its own (the
    detached loop); every live claim is then foreign, which is the intent.

    Fail-open on error is deliberate and matches the marker helpers: a claim-store
    problem must not wedge maintenance, and the caller's marker check stays in
    force.
    """
    return live_foreign_claim_owner(
        worktree_path, self_session_id, kind=kind, snap=snap
    ) is not None


def live_foreign_claim_owner(
    worktree_path: str,
    self_session_id: str,
    *,
    kind: str,
    snap=None,
) -> str | None:
    """The session id of a LIVE foreign claim on ``worktree_path``, or ``None``.

    Same question as :func:`live_foreign_claim`, but for the callers that need to
    name the owner rather than just know one exists — ``_live_foreign_owner``'s
    return shape, so a caller can union the two sources with a plain
    ``marker_owner or claim_owner``.

    Deterministic when several claims are live on one worktree: the ids are
    returned in ``claim_session_ids`` order, which
    :func:`resolve_worktree` de-duplicates while preserving first-seen order.
    """
    try:
        owned = resolve_worktree(worktree_path, kind=kind, snap=snap)
    except Exception:  # noqa: BLE001 — never break a coordination check
        return None
    for sid in owned.claim_session_ids:
        if sid != self_session_id:
            return sid
    return None


def ownership_unknown(worktree_path: str, *, kind: str, snap=None) -> bool:
    """True when ownership of ``worktree_path`` is genuinely UNRESOLVABLE.

    The claim store could not be read (a lock timeout or corrupt log) AND no
    marker exists to fall back on — see :attr:`WorktreeOwnership.unknown`. This
    is a THIRD answer, distinct from both "a live foreign claim/marker owns it"
    (``live_foreign_claim`` / ``_live_foreign_owner``, True) and "nobody owns
    it" (both return False/None because both sources were actually consulted
    and agree). A caller that only checks the first two would read this case as
    the second — "no owner, safe to service/dispatch/adopt" — which is exactly
    the P1 adoption-theft defect this exists to close. Callers that gate
    adoption or takeover must check this alongside the marker/claim checks and
    fail closed (defer/decline) when it is True, the same way
    ``reconcile._unknown_gh_state_record`` fails closed on an unresolvable `gh`
    probe rather than reporting a PR clean.

    Fail-open on an unexpected exception, matching every other helper in this
    module: a coordination-layer bug must not wedge maintenance broadly, and the
    caller's existing marker-based gate stays in force regardless.
    """
    try:
        return resolve_worktree(worktree_path, kind=kind, snap=snap).unknown
    except Exception:  # noqa: BLE001 — never break a coordination check
        return False
