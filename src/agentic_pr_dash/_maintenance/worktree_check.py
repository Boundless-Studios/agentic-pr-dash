"""The shared per-worktree blocker check — core of ``check`` and ``stop-gate``.

``_check_worktree`` is the single read-only engine that resolves a worktree's
branch→PR, computes live blockers, and renders the maintenance prompt. Both the
``check`` CLI (``_cmd_check``) and the Stop-hook ``stop-gate`` path
(``stop_gate._stop_gate_impl``) call it; keeping it in its own module lets both
import it without depending on the CLI facade.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar

from agentic_pr_dash import observability
from agentic_pr_dash.config import load as load_config
from agentic_pr_dash import models
from agentic_pr_dash.models import PRStatus

# Cross-module dependencies are called module-qualified (e.g.
# ``pr_state._resolve_pr_for_branch``) so the OWNING module is the single seam
# tests patch — patching ``markers._live_foreign_owner`` intercepts the call
# below, same as before the maintenance_check.py split.
from . import _common, completion, markers, pr_state, waiter, worktrees


# Stable suffix on every warn-only defer text. The detached loop (loop._tick)
# matches on it to LOG these exit-0 notices instead of dropping them, so a
# blocked owned PR is visible in loop output too (BOU-1788, codex PR #48 review).
WARN_ONLY_MARKER = "NOT clean (no fix dispatched)"
DRAFT_PR_MARKER = "PR is a draft; nothing pending"


# ── BOU-1879: never defer to a WAKE-LESS owner beyond one grace tick ────────────
# A "live foreign owner" holds ownership while its marker pid is alive or its
# heartbeat is fresh. But a wake-less owner — a session with NO live feedback
# waiter (e.g. codex, which has no wake channel) — can't be woken to service the
# PR, so deferring to it every tick leaves the PR unserviced forever (the observed
# "deferring … NOT clean (no fix dispatched)" loop). Grant exactly ONE grace tick
# (in case it's mid-task / about to arm a waiter), then take over.
def _wakeless_defer_path(cwd: str):
    return load_config(cwd).state_dir_for(cwd) / "pr-watch.wakeless-defer.json"


def _read_wakeless_defer(cwd: str) -> dict:
    try:
        with open(_wakeless_defer_path(cwd), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_wakeless_defer(cwd: str, data: dict) -> None:
    try:
        path = _wakeless_defer_path(cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def _wakeless_grace_exhausted(cwd: str, owner: str) -> bool:
    """True once this wake-less owner has held the PR for ``wakeless_takeover_seconds``.

    BOU-2475: this used to grant exactly ONE tick of grace, which in practice meant
    "take over almost immediately" — the loop's ``--interval`` is configurable and
    every dashboard-triggered ``--once`` run burns a tick too, so the grace could be
    spent in seconds. It is now a wall-clock duration, because what we actually want
    to express is "give the owner a real chance to finish", and that is a time, not a
    tick count.

    Still bounded rather than absolute: a wake-less owner (no live feedback waiter —
    e.g. codex, which has no wake channel) cannot be told its PR needs work, so
    deferring forever would strand the PR with nobody servicing it (BOU-1879).

    Keyed by cwd so sibling worktrees don't interfere, and reset implicitly when the
    owner changes. A record whose timestamp is unreadable or in the future is
    rewritten from now — a corrupt record must not grant instant takeover.
    """
    now = time.time()
    horizon = load_config(cwd).wakeless_takeover_seconds
    data = _read_wakeless_defer(cwd)
    entry = data.get(cwd) if isinstance(data.get(cwd), dict) else {}
    if entry.get("owner") == owner:
        try:
            first_seen = float(entry["first_seen"])
        except (KeyError, TypeError, ValueError):
            first_seen = None
        if first_seen is not None and first_seen <= now:
            return (now - first_seen) >= horizon
    data[cwd] = {"owner": owner, "first_seen": now}
    _write_wakeless_defer(cwd, data)
    return False


def _clear_wakeless_defer(cwd: str) -> None:
    """Drop this cwd's grace record — called once the owner is wake-capable again or
    we've taken over, so a future wake-less spell starts with a fresh grace tick."""
    data = _read_wakeless_defer(cwd)
    if cwd in data:
        data.pop(cwd, None)
        _write_wakeless_defer(cwd, data)


# ── BOU-1812: reclaim a demonstrably-stuck live owner ───────────────────────────
# Defense-in-depth for the "defer to live owner" path. A live owner keeps
# ownership as long as its heartbeat is fresh / its pid is alive — but a fresh
# heartbeat is NOT proof the PR is being serviced. When the owner's Stop-gate
# keeps returning a FALSE-NEGATIVE "nothing pending" (it refreshes the heartbeat
# with work_found=False, popping the fix-lease) while the PR still has REAL
# blockers, deferring every tick strands the PR — neither the owner nor the loop
# services it. Mirror the BOU-1789 per-PR failure-streak: count consecutive
# no-progress ticks (same blockers, no active fix-lease) and, once the streak
# reaches ``reclaim_no_progress_threshold``, RECLAIM (take over) instead of
# deferring. A progressing owner — an active fix-lease, or a CHANGED blocker set
# — resets the streak and keeps ownership, so only the stuck defer is broken.
def _no_progress_path(cwd: str):
    return load_config(cwd).state_dir_for(cwd) / "pr-watch.reclaim-no-progress.json"


def _read_no_progress(cwd: str) -> dict:
    try:
        with open(_no_progress_path(cwd), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_no_progress(cwd: str, data: dict) -> None:
    import tempfile  # noqa: PLC0415

    try:
        path = _no_progress_path(cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace so a concurrent reader never sees a torn JSON file (a
        # torn read would reset the streak to 0 and postpone the reclaim — an
        # extra defer tick, not a wrong reclaim, but the write is cheap to make
        # safe). A lost update between two racing loops is at worst an off-by-one
        # on the count (one extra defer tick), which is benign.
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".pr-watch.reclaim-no-progress."
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, str(path))
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
    except OSError:
        pass


def _reclaim_no_progress_threshold(cwd: str) -> int:
    return load_config(cwd).reclaim_no_progress_threshold


def _record_no_progress_tick(
    cwd: str, *, owner: str, pr_number: int, fingerprint: str
) -> int:
    """Increment (or reset) the consecutive no-progress streak for ``cwd``.

    The streak advances only while the SAME ``(owner, pr, fingerprint)`` recurs;
    any change — a different owner, a different PR on this worktree, or a changed
    blocker fingerprint (the owner or CI made progress) — resets it to 1, so a
    transient single tick can never reach a threshold ≥ 2 and a progressing owner
    never gets reclaimed. Returns the new count.
    """
    data = _read_no_progress(cwd)
    entry = data.get(cwd) if isinstance(data.get(cwd), dict) else {}
    same = (
        entry.get("owner") == owner
        and str(entry.get("pr", "")) == str(pr_number)
        and entry.get("fingerprint") == fingerprint
    )
    try:
        prior = int(entry.get("count", 0))
    except (TypeError, ValueError):
        prior = 0
    count = (prior + 1) if same else 1
    # BOU-2475: carry the streak's START time so takeover can require real elapsed
    # time, not just a tick count. Preserved across same-streak ticks; reset with
    # the count whenever the streak resets.
    now = time.time()
    first_seen = now
    if same:
        try:
            prior_first = float(entry["first_seen"])
        except (KeyError, TypeError, ValueError):
            prior_first = None
        if prior_first is not None and prior_first <= now:
            first_seen = prior_first
    data[cwd] = {
        "owner": owner,
        "pr": pr_number,
        "fingerprint": fingerprint,
        "count": count,
        "first_seen": first_seen,
    }
    _write_no_progress(cwd, data)
    return count


def _no_progress_elapsed(cwd: str, *, owner: str, pr_number: int) -> float | None:
    """Seconds since the current no-progress streak began, or None if unknown.

    None means "no usable timestamp" — a record written by a pre-BOU-2475 install,
    or a corrupt/future one. Callers treat None as "the time floor cannot be
    evaluated" and fall back to the tick threshold alone, so an upgrade mid-streak
    degrades to the old behaviour rather than deferring forever.
    """
    entry = _read_no_progress(cwd).get(cwd)
    if not isinstance(entry, dict):
        return None
    if entry.get("owner") != owner or str(entry.get("pr", "")) != str(pr_number):
        return None
    try:
        first_seen = float(entry["first_seen"])
    except (KeyError, TypeError, ValueError):
        return None
    now = time.time()
    if first_seen > now:
        return None
    return now - first_seen


def _clear_no_progress(cwd: str) -> None:
    """Drop this worktree's no-progress streak — called whenever the owner is no
    longer stuck (PR clean, active fix-lease, or we've just reclaimed) so a future
    stuck spell starts a fresh streak."""
    data = _read_no_progress(cwd)
    if cwd in data:
        data.pop(cwd, None)
        _write_no_progress(cwd, data)


def _owner_progress_state(marker_cwd: str, owner: str, pr_number: int) -> str:
    """Classify the owner's marker as ``fixing`` / ``idle`` / ``unknown``.

      * ``fixing``  — an ACTIVE fix-lease: the owner last stamped its heartbeat
        with work_found=True, so it is actively dispatching a fix (progress).
      * ``idle``    — a FRESH heartbeat but NO active fix-lease: the owner ticked
        recently and reported "nothing pending" while blockers persist. This is
        the exact false-negative that strands the PR — the only state a
        no-progress tick is counted against.
      * ``unknown`` — no readable marker for ``owner`` / the marker names a
        DIFFERENT PR / a stale heartbeat: we cannot POSITIVELY confirm the owner
        ticked-and-found-nothing FOR THIS PR, so we do NOT count a no-progress
        tick (stay conservative and keep deferring).

    A merely-fresh heartbeat is deliberately NOT treated as progress: the stuck
    "nothing pending" owner refreshes its heartbeat every tick while popping the
    fix-lease — that IS the ``idle`` signal we reclaim on.

    The marker's ``pr`` MUST equal ``pr_number`` (P1, codex PR #83 review): a live
    session can hold LEDGER ownership of several PRs while its worktree marker
    names only its *current* branch's PR (``markers._live_pr_owner_record``). So a
    fresh-heartbeat/no-lease marker for a DIFFERENT PR is not evidence that this
    PR was reported idle — treating it as such would reclaim a ledger-owned PR the
    live owner never surfaced, defeating the ledger gate's no-concurrent-
    maintenance guarantee.
    """
    fields = markers._read_marker(marker_cwd)
    if not fields or fields.get("session_id", "") != owner:
        return "unknown"
    if str(fields.get("pr", "")) != str(pr_number):
        return "unknown"
    if markers._fix_lease_active(fields.get("fix_lease_until", "")):
        return "fixing"
    heartbeat_fresh = any(
        markers._heartbeat_fresh(value, marker_cwd)
        for value in (fields.get("last_heartbeat", ""), fields.get("heartbeat", ""))
        if value
    )
    return "idle" if heartbeat_fresh else "unknown"


def _hydrate_unresolved_threads(pr, cwd: str) -> None:
    """Merge live unresolved review threads into ``pr.review_comments`` in place.

    A read-only gh query. Uses ``setdefault`` semantics (keep any comment already
    present, add thread-only ones) so an already-hydrated PR is not disturbed —
    the single source of truth shared by the reclaim fingerprint and the main
    service path, so both compute the SAME fully-hydrated fingerprint.
    """
    thread_comments = completion._review_comments_from_threads(
        pr_state._unresolved_review_threads(pr.number, cwd)
    )
    if not thread_comments:
        return
    merged = {c.id: c for c in pr.review_comments}
    for c in thread_comments:
        merged.setdefault(c.id, c)
    pr.review_comments = list(merged.values())


def _should_reclaim_stuck_owner(
    cwd: str, *, owner: str, pr, marker_cwd: str, claim: bool
) -> bool:
    """Decide whether to RECLAIM a live owner that is deferring with blockers.

    Returns True (take over) only once the consecutive no-progress streak reaches
    ``reclaim_no_progress_threshold``; otherwise False (keep deferring). We count a
    no-progress tick ONLY when the owner is demonstrably ``idle`` (fresh
    heartbeat, no fix-lease) with the SAME blocker fingerprint — so an actively
    ``fixing`` owner, an owner whose blockers changed (progress), and an owner we
    cannot confirm ticked are all kept deferred. The blocker fingerprint is the
    same content the coordinator claims on, so any real change (a fix commit,
    resolved comments, new CI) resets the streak.

    Confined to the ACTIVE check (``claim=True`` — the detached loop's dispatch):
    the passive stop-gate probe (``claim=False``) never writes coordination state
    (a stop-gate-created claim would outlive the idle session), so it must not
    advance/reset this streak nor reclaim on it either. Gating the whole mechanism
    on ``claim`` keeps the loop the single authoritative writer, so the counter
    tracks LOOP ticks rather than incidental foreign stop-gate probes.
    """
    if not claim:
        return False
    from agentic_pr_dash import coordinator  # noqa: PLC0415

    state = _owner_progress_state(marker_cwd, owner, pr.number)
    if state == "fixing":
        _clear_no_progress(cwd)  # actively servicing → progress; reset the streak
        return False
    if state != "idle":
        # Can't positively confirm the owner ticked-and-found-nothing — keep
        # deferring without advancing (or resetting) the streak, so a transient
        # unreadable marker neither reclaims nor wipes a real streak.
        return False
    # Hydrate live unresolved review threads BEFORE fingerprinting (P2, codex PR
    # #83 review). ``_resolve_and_blockers`` only fetches threads when the PR has
    # NO other blocker; a PR that already has e.g. failed CI keeps its
    # un-hydrated ``review_comments`` here, so a newly-added GraphQL-only
    # unresolved thread would leave the fingerprint unchanged and fail to reset
    # the streak (wrongly counting toward reclaim). Merge threads in so the
    # fingerprint tracks the SAME fully-hydrated blocker state the coordinator
    # claims on — a new thread now changes the fingerprint and resets the streak.
    _hydrate_unresolved_threads(pr, cwd)
    fingerprint = coordinator.fingerprint_for_pr(pr)
    count = _record_no_progress_tick(
        cwd, owner=owner, pr_number=pr.number, fingerprint=fingerprint
    )
    if count < _reclaim_no_progress_threshold(cwd):
        return False
    # BOU-2475: the streak is necessary but NOT sufficient. Reclaiming a live,
    # wake-capable session's PR is the expensive mistake: the executor we dispatch
    # instead has none of that session's context — the design intent, what was
    # already tried and rejected, why a reviewer's suggestion does not apply. It has
    # produced confidently wrong changes that then merged. So require real elapsed
    # time on top of the tick count, since ticks are a poor clock (configurable
    # --interval, plus every dashboard `--once` run burns one).
    elapsed = _no_progress_elapsed(cwd, owner=owner, pr_number=pr.number)
    if elapsed is not None and elapsed < load_config(cwd).live_owner_takeover_seconds:
        return False
    _clear_no_progress(cwd)
    return True


def _decision_wait_marker_path(cwd: str):
    return _wakeless_defer_path(cwd).parent / "decision-wait.emitted"


def _mark_decision_wait_emitted(cwd: str, decision_id: str) -> bool:
    """True the first time we see ``decision_id``; False on every later pass.

    Edge-triggering for the ``decision_wait`` event. The marker records the last
    decision id emitted for this worktree, so a NEW decision (including one that
    superseded the previous) still emits, while repeated polls on the same
    unanswered question stay silent.

    Best-effort: if the marker cannot be read or written we return True and
    accept a duplicate event rather than losing the signal entirely — a missing
    record is worse than a repeated one.
    """
    path = _decision_wait_marker_path(cwd)
    try:
        previous = path.read_text(encoding="utf-8").strip()
    except OSError:
        previous = ""
    if previous == decision_id:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(decision_id, encoding="utf-8")
    except OSError:
        pass
    return True


def _blocked_defer_text(*, pr_number: int, blockers: list[str], owner_desc: str) -> str:
    """Warn-only defer text for a NON-owning checker that sees a blocked owned PR.

    The invariant (BOU-1785/1788): no defer path may emit a clean-looking no-op
    while the owned PR has known blockers. This names the PR, the blockers, and
    the owner so loop logs / sibling output / manual ``check`` can't be mistaken
    for ``nothing pending``. The caller stays exit 0 and does NOT dispatch a fix
    (the live owner is responsible) — preserving the don't-double-fix invariant.
    """
    return (
        f"owned PR #{pr_number} has blockers {sorted(blockers)}; "
        f"deferring to {owner_desc} — {WARN_ONLY_MARKER}"
    )


_CURRENT_PR_BINDING: ContextVar[object | None] = ContextVar(
    "pr_watch_current_pr_binding", default=None
)


@contextmanager
def _use_current_pr_binding(binding):
    """Make one strict current-branch resolution drive the blocker probe."""
    token = _CURRENT_PR_BINDING.set(binding)
    try:
        yield
    finally:
        _CURRENT_PR_BINDING.reset(token)


def _resolve_and_blockers(cwd: str):
    """Resolve the worktree's branch→PR and compute its live blockers.

    Returns ``(pr, blockers)``. ``pr`` may be the ``_GH_UNAVAILABLE`` sentinel,
    ``None`` (no PR), or a draft ``PRData`` — ``blockers`` is ``[]`` for all of
    those non-actionable cases. For a real, non-draft PR the blockers include a
    thread-aware fallback (an OLD unresolved review thread that ``blockers_for_pr``
    misses). Shared by the owner-defer paths and the main service path so a
    deferral can name the blockers without re-deriving them.
    """
    from agentic_pr_dash import maintenance  # noqa: PLC0415 — avoid import cycle

    binding = _CURRENT_PR_BINDING.get()
    if binding is not None and getattr(binding, "unknown", False):
        pr = pr_state._GH_UNAVAILABLE
    elif binding is not None and getattr(binding, "resolved", False):
        number = getattr(binding, "pr_number", None)
        pr = (
            pr_state._resolve_pr_by_number(number, cwd)
            if number is not None
            else None
        )
        if pr is not None and pr is not pr_state._GH_UNAVAILABLE:
            # The strict resolver validated draft state for this checkout. The
            # explicit-number detail lookup may still be served by a warm list
            # snapshot from before a draft/ready transition, so its state must
            # not override the binding that selected this PR.
            pr = pr.model_copy(
                update={"is_draft": bool(getattr(binding, "is_draft", False))}
            )
    else:
        pr = pr_state._resolve_pr_for_branch(cwd)
    if pr is pr_state._GH_UNAVAILABLE or pr is None or pr.is_draft:
        return pr, []
    from agentic_pr_dash import github_api  # noqa: PLC0415
    github_api.record_published_pr_head(pr.number, pr.latest_commit_sha, cwd)
    blockers = maintenance.blockers_for_pr(pr)
    if not blockers:
        unresolved_threads = pr_state._unresolved_review_threads(pr.number, cwd)
        if unresolved_threads:
            pr.review_comments = completion._review_comments_from_threads(unresolved_threads)
            blockers = ["review_comments"]
    return pr, blockers


def _check_worktree(cwd: str, self_session_id: str, *, claim: bool = True) -> tuple[int, str]:
    """Read-only blocker check for ONE worktree. Returns ``(exit_code, text)``.

    The single shared core of the ``check`` CLI and the ``stop-gate`` Stop hook:
      * 0  — clean / deferred-to-live-owner / no PR / draft (``text`` explains).
      * 2  — gh unavailable.
      * 10 — work pending; ``text`` is the self-contained maintenance prompt
             (ending in ``PR_NUMBER=<n>``).

    READ-ONLY except for the owner heartbeat/lease stamp (same as before the
    extraction). ``check`` prints ``text`` and returns the code; ``stop-gate``
    aggregates the exit-10 texts across owned worktrees.

    ``claim`` controls whether a found-work path CREATES an agent-coordinator
    claim (a WRITE). The ``check`` CLI (loop dispatch) claims so the loop owns the
    fix and later heartbeats/releases it. The Stop-hook ``stop-gate`` path is
    PASSIVE — it only prints a prompt to the interactive session and never
    releases — so it passes ``claim=False``; a stop-gate-created claim would
    survive the idle session and suppress the very work it just surfaced on the
    next Stop attempt (codex P1).

    The READ-ONLY deferral to an EXISTING active claim runs in BOTH modes
    (BOU-1783): if a LIVE agent-coordinator claim (the detached loop) already owns
    this PR's current blocker set, the passive probe defers (exit 0) just like the
    active ``check`` does, so the Stop gate doesn't re-block the idle session for
    work the loop is already handling. Only the claim-CREATING write below stays
    gated behind ``claim=True``. A DEAD/expired claim is reclaimable
    (``should_dispatch``) and so does NOT suppress the work (BOU-1789
    "pid-alive ≠ working").
    """
    from agentic_pr_dash import coordinator, github_api, maintenance  # noqa: PLC0415

    cwd = os.path.abspath(cwd)

    # Ownership gate — defer to a live, actively-looping in-session owner before
    # doing the claim/dispatch work. See _live_foreign_owner. We DO still resolve
    # the PR + compute blockers here so a deferral to a live owner cannot report
    # a blocked owned PR as a clean no-op (BOU-1788): the owner is responsible for
    # the fix, but every checker must still SURFACE that the PR is blocked.
    owner = markers._live_foreign_owner(cwd, self_session_id)
    if owner is None:
        # Claim-side half of the same gate. Unioned, not a replacement: from
        # Stage 4 the marker is no longer written, so the marker-only check
        # degrades to "nobody owns this" and this — the primary cross-session
        # ownership gate — would stop firing entirely, letting two sessions (or
        # the detached loop and a live session) both dispatch on one PR. The
        # coordinator claim below is a different store with its own lease and
        # does NOT implement "live in-session owner wins" (BOU-2223 Stage 4).
        from .ownership_resolution import live_foreign_claim_owner  # noqa: PLC0415
        owner = live_foreign_claim_owner(
            cwd, self_session_id, kind="worktree_check_divergence"
        )
        if owner is None:
            # Fail closed (P1 adoption-theft defect): neither the marker nor a
            # live claim named an owner, but that can mean two very different
            # things — "both sources agree nobody owns this" (safe to proceed)
            # or "the claim store could not be read this tick, so we simply
            # could not tell" (NOT safe). ``ownership_unknown`` distinguishes
            # them; collapsing the two here would let this session dispatch or
            # take over a PR a live session actually holds, whose claim we
            # just failed to read (e.g. a lock-contention race on the shared
            # store). Report unobservable — the same convention as a `gh`
            # outage (code 2) — so neither the active `check`/claim path nor
            # the passive stop-gate concludes clean or dispatches; a later
            # tick with a healthy store resolves it correctly from either
            # side.
            from .ownership_resolution import ownership_unknown  # noqa: PLC0415
            if ownership_unknown(cwd, kind="worktree_check_divergence"):
                return 2, (
                    "[pr-watch] ownership state unresolvable for this worktree "
                    "(claim store unreadable, no local marker) — deferring "
                    "this tick rather than risk taking over a live session's PR"
                )
    if owner is not None:
        # BOU-1879: a wake-capable owner (live feedback waiter) keeps ownership; a
        # WAKE-LESS owner gets exactly one grace tick, then we take over (fall through
        # to the normal claim/dispatch path below) so its PR isn't stuck forever.
        if waiter._await_alive(cwd, owner):
            _clear_wakeless_defer(cwd)  # wake-capable → reset any prior grace record
            take_over = False
        else:
            take_over = _wakeless_grace_exhausted(cwd, owner)
        if not take_over:
            owner_pr, owner_blockers = _resolve_and_blockers(cwd)
            if not owner_blockers:
                _clear_no_progress(cwd)  # owner's PR is clean → reset the streak
                return 0, f"deferring to live PR-watch owner session {owner}"
            # Blockers exist under a live owner. BOU-1812: reclaim ONLY when the
            # owner is demonstrably stuck (same blockers, no fix-lease) for N
            # consecutive ticks; otherwise surface + defer (the owner owns the
            # fix). A progressing owner resets the streak inside the helper.
            if not _should_reclaim_stuck_owner(
                cwd, owner=owner, pr=owner_pr, marker_cwd=cwd, claim=claim
            ):
                return 0, _blocked_defer_text(
                    pr_number=owner_pr.number,
                    blockers=owner_blockers,
                    owner_desc=f"live PR-watch owner session {owner}",
                )
            # else: streak reached threshold — fall through and take over.
        # Grace exhausted for a wake-less owner, or reclaimed a stuck wake-capable
        # owner — clear the coordination records and take over.
        _clear_wakeless_defer(cwd)
        _clear_no_progress(cwd)

    # Resolve PR + blockers (thread-aware). Purely read, no state written.
    pr, blockers = _resolve_and_blockers(cwd)

    if pr is pr_state._GH_UNAVAILABLE:
        return 2, pr_state._gh_unavailable_message(cwd)
    if pr is None:
        return 0, "no open PR for this branch"

    # Never service a DRAFT — the author marked it not-ready.
    if pr.is_draft:
        return 0, DRAFT_PR_MARKER

    # ── BOU-1924: ledger/registry ownership gate ───────────────────────────────
    # The marker gate above only sees THIS worktree's marker. A PR owned by a
    # live OTHER session whose worktree was repointed away has no marker here, so
    # resolve ownership from the durable session ledger + registry and defer to
    # that live session rather than servicing (or taking over) its PR. Same
    # wake-capability semantics as the marker gate: a wake-capable owner (live
    # SESSION-scoped waiter) keeps ownership; a wake-less owner gets one grace
    # tick then we take over. Self is excluded by the resolver, so a session
    # checking its OWN owned PR falls through to service it.
    #
    # LIMIT to cases where the marker gate did NOT resolve ownership (PR #61
    # review, P2): a self-owned marker means this session should service its own
    # PR, and a live foreign marker owner has already been handled above. But a
    # stale foreign marker whose owner is no longer live must NOT suppress the
    # durable ledger gate; another live session may still own this PR there.
    marker_session = markers._marker_session_id(cwd)
    marker_pr_matches = worktrees._marker_pr(cwd) == str(pr.number)
    marker_resolved_ownership = bool(marker_session) and (
        (marker_session == (self_session_id or "") and marker_pr_matches)
        or owner is not None
    )
    ledger_owner_record = None
    ledger_owner = None
    ledger_owner_cwd = cwd
    if not marker_resolved_ownership:
        ledger_owner_record = markers._live_pr_owner_record(
            pr.number, _common._repo_slug(cwd), self_session_id, cwd
        )
        if ledger_owner_record is not None:
            ledger_owner, ledger_owner_cwd = ledger_owner_record
        else:
            # Keep tests and older patch points that monkeypatch _live_pr_owner
            # working while real code uses the richer record above.
            ledger_owner = markers._live_pr_owner(
                pr.number, _common._repo_slug(cwd), self_session_id, cwd
            )
    if ledger_owner is not None:
        if waiter._await_alive(ledger_owner_cwd, ledger_owner):
            _clear_wakeless_defer(cwd)
            take_over = False
        else:
            take_over = _wakeless_grace_exhausted(cwd, ledger_owner)
        if not take_over:
            if not blockers:
                _clear_no_progress(cwd)  # owner's PR is clean → reset the streak
                return 0, f"deferring to live PR-watch owner session {ledger_owner} (ledger)"
            # BOU-1812: same reclaim-on-persistent-no-progress guard as the marker
            # gate. The owner's marker lives in ITS worktree context, so probe the
            # fix-lease there (``ledger_owner_cwd``).
            if not _should_reclaim_stuck_owner(
                cwd, owner=ledger_owner, pr=pr, marker_cwd=ledger_owner_cwd, claim=claim
            ):
                return 0, _blocked_defer_text(
                    pr_number=pr.number,
                    blockers=blockers,
                    owner_desc=f"live PR-watch owner session {ledger_owner} (ledger)",
                )
            # else: streak reached threshold — fall through and take over.
        _clear_wakeless_defer(cwd)
        _clear_no_progress(cwd)

    # Populate ci_watch_pending for non-draft PRs (best-effort; False on gh
    # error). BOU-1789: required-checks-in-progress as a watch condition.
    pr.ci_watch_pending = github_api.required_checks_pending(pr.number, cwd)

    if not blockers:
        # Clean check: refresh the alive heartbeat (hold ownership) and clear any
        # stale fix lease.
        if markers._marker_session_id(cwd) == self_session_id and worktrees._live_independent_owner_paths(
            [cwd], self_session_id
        ):
            return 0, "stale stolen marker; deferring to live independent owner"
        markers._touch_owner_heartbeat(cwd, self_session_id, False)
        # BOU-2567: "nothing pending" must not go silent about deferred threads
        # still sitting open on GitHub — a deferral is a tracked exclusion, not
        # an erased one, and the gate must distinguish `deferred: N` from
        # `unresolved: N` in its output rather than let the two collapse to the
        # same clean-looking text.
        deferred_n = len(pr_state._deferred_review_threads(pr.number, cwd))
        if deferred_n:
            return 0, f"nothing pending (deferred: {deferred_n})"
        return 0, "nothing pending"

    # Work exists — but defer to a live INDEPENDENT owner BEFORE writing any
    # heartbeat/lease (BOU-1540). Blockers are known here, so name them rather
    # than emitting a clean-looking no-op (BOU-1788 family).
    independent_owners = worktrees._live_independent_owner_sessions(
        [cwd], self_session_id
    ).get(cwd, ())
    if independent_owners:
        has_registry_owner = any(
            owner.registry_backed for owner in independent_owners
        )
        waiter_owner = next(
            (
                owner
                for owner in independent_owners
                if waiter._await_alive(cwd, owner.session_id)
            ),
            None,
        )
        if has_registry_owner or waiter_owner is not None:
            _clear_wakeless_defer(cwd)
            take_over = False
        else:
            owner_key = "independent:" + "|".join(
                owner.session_id for owner in independent_owners
            )
            take_over = _wakeless_grace_exhausted(cwd, owner_key)
        if not take_over:
            return 0, _blocked_defer_text(
                pr_number=pr.number,
                blockers=blockers,
                owner_desc="live independent owner of this worktree",
            )
        _clear_wakeless_defer(cwd)
        _clear_no_progress(cwd)

    owner_session_id = self_session_id or f"pid:{_common._resolve_owner_pid()}"

    # READ-ONLY deferral decision — runs in BOTH modes (BOU-1783). The passive
    # stop-gate probe (claim=False) must defer to a LIVE active claim the same way
    # the active check (claim=True) does; only the claim-CREATING write below is
    # gated on `claim`. None of the calls here mutate the claim store.

    # Merge live unresolved threads into review_comments for accurate
    # fingerprinting (so both modes compute the SAME defer decision) and a
    # complete maintenance prompt. Read-only gh query.
    _hydrate_unresolved_threads(pr, cwd)

    live_fingerprint = coordinator.fingerprint_for_pr(pr)
    active_fingerprint = coordinator.active_claim_fingerprint_for_pr(pr)
    new_feedback = (
        active_fingerprint is not None and active_fingerprint != live_fingerprint
    )

    # A claim owned by THIS session must be SERVICED, not deferred to: the caller
    # already holds it and the blockers are confirmed, so deferring to one's own
    # active claim hides the work for the whole lease window (BOU-1785 repro).
    # `new_feedback` only catches a CHANGED fingerprint; a claim made for the SAME
    # still-unresolved blockers slips through it.
    active_owner = coordinator.active_claim_owner_for_pr(pr)
    self_owned = active_owner is not None and active_owner.session_id == owner_session_id

    # BOU-2491: this checker (the loop's dispatch precursor, or a session's
    # own passive probe) IS a genuinely executing/servicing caller — it must
    # never idle behind a dashboard's advisory (can_execute=false) claim, the
    # bug where "the component that cannot run an executor holds a claim
    # against the component that can."
    coord_decision = coordinator.dispatch_decision_for_pr(pr, caller_can_execute=True)

    # BOU-2040: a pending human decision defers UNCONDITIONALLY — it must
    # outrank both `new_feedback` and `self_owned`, unlike a foreign claim.
    # Those two exist to stop a live owner's work being stolen; neither says
    # anything about whether the human has answered. A comment landing while we
    # wait flips `new_feedback` true, and the owning session re-entering flips
    # `self_owned` true — either would fall through to dispatch and send an
    # executor straight back into the boundary nobody has ruled on, which is the
    # retry loop this whole path exists to prevent.
    if coord_decision.state == "waiting_human":
        # BOU-2402: make the pause legible. The status moves the card into its
        # own board column, the carried fields let a viewer read the question
        # without opening the coordinator ledger, and the event lets
        # `pr-observe` answer "why did this PR stop being dispatched?".
        waiting = coordinator.pending_decision_for_pr(pr)
        if waiting is not None:
            pr.status = PRStatus.WAITING_HUMAN_DECISION
            pr.waiting_decision_id = waiting.request.decision_id
            pr.waiting_decision_question = waiting.request.question
            pr.waiting_decision_category = waiting.request.category
            pr.waiting_decision_runtime = waiting.request.requesting_runtime
            # Edge-triggered, not level-triggered (PR #110 review). The waiter
            # polls this every ~150s and detached loops check the same worktree,
            # so emitting on every pass would fill the bounded event store with
            # identical records and evict the history observability exists to
            # preserve. Emit once per decision, on entry.
            if _mark_decision_wait_emitted(cwd, waiting.request.decision_id):
                observability.emit(
                    cwd,
                    "decision_wait",
                    pr_number=pr.number,
                    session_id=owner_session_id,
                    details={
                        "decision_id": waiting.request.decision_id,
                        "claim_id": waiting.request.claim_id,
                        "category": waiting.request.category,
                        "requesting_runtime": waiting.request.requesting_runtime,
                        "logical_key": waiting.request.logical_key,
                    },
                )
        return 0, _blocked_defer_text(
            pr_number=pr.number,
            blockers=blockers,
            owner_desc=(
                f"agent-coordinator {coord_decision.state}: {coord_decision.reason} "
                f"(claim {coord_decision.claim_id}, "
                f"owner session {coord_decision.owner_session_id})"
            ),
        )

    if not coord_decision.should_dispatch and not new_feedback and not self_owned:
        # A LIVE foreign claim owns this PR's blocker set — defer (exit 0) in both
        # modes. Preserve the coordinator's own state + reason (e.g. the
        # manual_intervention "owner worktree has dirty/unpushed changes: <path>"
        # guidance, or the active-claim "claim is active") so the operator still
        # learns WHY this defers — wrapping it in the blockers + NOT-clean framing
        # rather than replacing it (codex PR #48). A dead/expired claim is
        # reclaimable (should_dispatch) so it falls through and surfaces below.
        return 0, _blocked_defer_text(
            pr_number=pr.number,
            blockers=blockers,
            owner_desc=(
                f"agent-coordinator {coord_decision.state}: {coord_decision.reason} "
                f"(claim {coord_decision.claim_id}, "
                f"owner session {coord_decision.owner_session_id}, "
                f"pid {coord_decision.owner_pid})"
            ),
        )

    coordinator_claim: coordinator.ClaimHandle | None = None
    coordinator_fingerprint: str | None = None
    if claim:
        # Creating/refreshing a claim is a WRITE — the passive stop-gate probe
        # never writes (a stop-gate-created claim would survive the idle session
        # and suppress the very work it surfaced). Only the active `check` path
        # claims + heartbeats here.
        if coord_decision.state == "advisory_only":
            coordinator.release_advisory_claim_for_pr(
                pr, claim_id=coord_decision.claim_id
            )
        claimed = coordinator.claim_pr(
            pr,
            session_id=owner_session_id,
            pid=_common._resolve_owner_pid(),
            agent="agentic-pr-dash-check",
            lease_seconds=_common._fix_lease_seconds(),
            # Unlike the dashboard's advisory claim, this one is taken by the
            # active `check` path — the immediate precursor to a real dispatch
            # (loop._service_cwd shells out to `check`, then runs the executor on
            # the prompt it returns). So the holder genuinely will write code, and
            # deferring to it is correct. Both executing actors answer can_execute
            # the same way, so this classification does not change today's
            # behavior — only the dashboard's claim becomes advisory (BOU-2490).
            actor=models.MaintenanceActor.LOOP_EXECUTOR,
        )
        if claimed is None and not new_feedback and not self_owned:
            return 0, _blocked_defer_text(
                pr_number=pr.number,
                blockers=blockers,
                owner_desc="active agent-coordinator claim",
            )
        if claimed is not None:
            coordinator_claim = claimed
            coordinator_fingerprint = coordinator.fingerprint_for_pr(pr)

        # We will service: refresh the heartbeat and set the long fix lease.
        markers._touch_owner_heartbeat(cwd, self_session_id, True)

    # Fetch failed CI logs
    logs: dict[str, str] = {}
    if pr.failing_checks:
        logs = github_api.get_failed_logs(pr.latest_commit_sha, pr.failing_checks, cwd)

    prompt = maintenance.build_maintenance_prompt(pr, failed_logs=logs)
    # BOU-2567: report deferred threads distinctly even on a dispatch tick —
    # some OTHER blocker (CI, a different thread) may coexist with deferred
    # ones, and the summary must not let those go uncounted just because
    # something else is also pending.
    deferred_n = len(pr_state._deferred_review_threads(pr.number, cwd))
    summary = maintenance.build_maintenance_summary(pr, deferred_count=deferred_n)
    text = f"{prompt}\nSUMMARY={summary}\nPR_NUMBER={pr.number}"
    if coordinator_claim is not None:
        text += (
            f"\nCOORDINATOR_CLAIM_ID={coordinator_claim.claim_id}"
            f"\nCOORDINATOR_LEASE_EPOCH={coordinator_claim.lease_epoch}"
            f"\nCOORDINATOR_TASK_FINGERPRINT={coordinator_fingerprint}"
        )
    return 10, text
