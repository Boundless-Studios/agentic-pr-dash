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

from agentic_pr_dash.config import load as load_config

# Cross-module dependencies are called module-qualified (e.g.
# ``pr_state._resolve_pr_for_branch``) so the OWNING module is the single seam
# tests patch — patching ``markers._live_foreign_owner`` intercepts the call
# below, same as before the maintenance_check.py split.
from . import _common, completion, markers, pr_state, waiter, worktrees


# Stable suffix on every warn-only defer text. The detached loop (loop._tick)
# matches on it to LOG these exit-0 notices instead of dropping them, so a
# blocked owned PR is visible in loop output too (BOU-1788, codex PR #48 review).
WARN_ONLY_MARKER = "NOT clean (no fix dispatched)"


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
    """True if we've ALREADY deferred to this wake-less owner once (grace used up →
    take over). Records the first grace tick; keyed by cwd so sibling worktrees don't
    interfere, and reset implicitly when the owner changes."""
    data = _read_wakeless_defer(cwd)
    entry = data.get(cwd) if isinstance(data.get(cwd), dict) else {}
    if entry.get("owner") == owner and int(entry.get("count", 0)) >= 1:
        return True
    data[cwd] = {"owner": owner, "count": 1}
    _write_wakeless_defer(cwd, data)
    return False


def _clear_wakeless_defer(cwd: str) -> None:
    """Drop this cwd's grace record — called once the owner is wake-capable again or
    we've taken over, so a future wake-less spell starts with a fresh grace tick."""
    data = _read_wakeless_defer(cwd)
    if cwd in data:
        data.pop(cwd, None)
        _write_wakeless_defer(cwd, data)


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

    pr = pr_state._resolve_pr_for_branch(cwd)
    if pr is pr_state._GH_UNAVAILABLE or pr is None or pr.is_draft:
        return pr, []
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
            if owner_blockers:
                return 0, _blocked_defer_text(
                    pr_number=owner_pr.number,
                    blockers=owner_blockers,
                    owner_desc=f"live PR-watch owner session {owner}",
                )
            return 0, f"deferring to live PR-watch owner session {owner}"
        # Grace exhausted for a wake-less owner — clear the record and take over.
        _clear_wakeless_defer(cwd)

    # Resolve PR + blockers (thread-aware). Purely read, no state written.
    pr, blockers = _resolve_and_blockers(cwd)

    if pr is pr_state._GH_UNAVAILABLE:
        return 2, pr_state._gh_unavailable_message(cwd)
    if pr is None:
        return 0, "no open PR for this branch"

    # Never service a DRAFT — the author marked it not-ready.
    if pr.is_draft:
        return 0, "PR is a draft; nothing pending"

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
    # LIMIT to the truly MARKERLESS/repointed-away case (PR #61 review, P2): run
    # the ledger gate ONLY when THIS worktree has no armed marker at all. If a
    # marker is present, the marker gate above already decided ownership for this
    # worktree — consulting the ledger would (a) let a stale PREVIOUS-owner row
    # make the current self-owner defer its own blockers, and (b) re-grant a
    # wake-less grace tick to a foreign owner the marker gate JUST took over
    # (double grace), deferring once more instead of taking over as promised.
    ledger_owner = (
        None
        if markers._marker_session_id(cwd)
        else markers._live_pr_owner(
            pr.number, _common._repo_slug(cwd), self_session_id, cwd
        )
    )
    if ledger_owner is not None:
        if waiter._await_alive(cwd, ledger_owner):
            _clear_wakeless_defer(cwd)
            take_over = False
        else:
            take_over = _wakeless_grace_exhausted(cwd, ledger_owner)
        if not take_over:
            if blockers:
                return 0, _blocked_defer_text(
                    pr_number=pr.number,
                    blockers=blockers,
                    owner_desc=f"live PR-watch owner session {ledger_owner} (ledger)",
                )
            return 0, f"deferring to live PR-watch owner session {ledger_owner} (ledger)"
        _clear_wakeless_defer(cwd)

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
        return 0, "nothing pending"

    # Work exists — but defer to a live INDEPENDENT owner BEFORE writing any
    # heartbeat/lease (BOU-1540). Blockers are known here, so name them rather
    # than emitting a clean-looking no-op (BOU-1788 family).
    if worktrees._live_independent_owner_paths([cwd], self_session_id):
        return 0, _blocked_defer_text(
            pr_number=pr.number,
            blockers=blockers,
            owner_desc="live independent owner of this worktree",
        )

    owner_session_id = self_session_id or f"pid:{_common._resolve_owner_pid()}"

    # READ-ONLY deferral decision — runs in BOTH modes (BOU-1783). The passive
    # stop-gate probe (claim=False) must defer to a LIVE active claim the same way
    # the active check (claim=True) does; only the claim-CREATING write below is
    # gated on `claim`. None of the calls here mutate the claim store.

    # Merge live unresolved threads into review_comments for accurate
    # fingerprinting (so both modes compute the SAME defer decision) and a
    # complete maintenance prompt. Read-only gh query.
    thread_comments = completion._review_comments_from_threads(
        pr_state._unresolved_review_threads(pr.number, cwd)
    )
    if thread_comments:
        merged = {c.id: c for c in pr.review_comments}
        for c in thread_comments:
            merged.setdefault(c.id, c)
        pr.review_comments = list(merged.values())

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

    coord_decision = coordinator.dispatch_decision_for_pr(pr)
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

    coordinator_claim_id: str | None = None
    coordinator_fingerprint: str | None = None
    if claim:
        # Creating/refreshing a claim is a WRITE — the passive stop-gate probe
        # never writes (a stop-gate-created claim would survive the idle session
        # and suppress the very work it surfaced). Only the active `check` path
        # claims + heartbeats here.
        claimed = coordinator.claim_pr(
            pr,
            session_id=owner_session_id,
            pid=_common._resolve_owner_pid(),
            agent="agentic-pr-dash-check",
            lease_seconds=_common._fix_lease_seconds(),
        )
        if claimed is None and not new_feedback and not self_owned:
            return 0, _blocked_defer_text(
                pr_number=pr.number,
                blockers=blockers,
                owner_desc="active agent-coordinator claim",
            )
        if claimed is not None:
            coordinator_claim_id = claimed.claim_id
            coordinator_fingerprint = claimed.task.fingerprint

        # We will service: refresh the heartbeat and set the long fix lease.
        markers._touch_owner_heartbeat(cwd, self_session_id, True)

    # Fetch failed CI logs
    logs: dict[str, str] = {}
    if pr.failing_checks:
        logs = github_api.get_failed_logs(pr.latest_commit_sha, pr.failing_checks, cwd)

    prompt = maintenance.build_maintenance_prompt(pr, failed_logs=logs)
    text = f"{prompt}\nPR_NUMBER={pr.number}"
    if coordinator_claim_id is not None:
        text += (
            f"\nCOORDINATOR_CLAIM_ID={coordinator_claim_id}"
            f"\nCOORDINATOR_TASK_FINGERPRINT={coordinator_fingerprint}"
        )
    return 10, text
