"""The shared per-worktree blocker check — core of ``check`` and ``stop-gate``.

``_check_worktree`` is the single read-only engine that resolves a worktree's
branch→PR, computes live blockers, and renders the maintenance prompt. Both the
``check`` CLI (``_cmd_check``) and the Stop-hook ``stop-gate`` path
(``stop_gate._stop_gate_impl``) call it; keeping it in its own module lets both
import it without depending on the CLI facade.
"""
from __future__ import annotations

import os

# Cross-module dependencies are called module-qualified (e.g.
# ``pr_state._resolve_pr_for_branch``) so the OWNING module is the single seam
# tests patch — patching ``markers._live_foreign_owner`` intercepts the call
# below, same as before the maintenance_check.py split.
from . import _common, completion, markers, pr_state, worktrees


# Stable suffix on every warn-only defer text. The detached loop (loop._tick)
# matches on it to LOG these exit-0 notices instead of dropping them, so a
# blocked owned PR is visible in loop output too (BOU-1788, codex PR #48 review).
WARN_ONLY_MARKER = "NOT clean (no fix dispatched)"


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

    ``claim`` controls whether a found-work path creates an agent-coordinator
    claim. The ``check`` CLI (loop dispatch) claims so the loop owns the fix and
    later heartbeats/releases it. The Stop-hook ``stop-gate`` path is PASSIVE —
    it only prints a prompt to the interactive session and never releases — so it
    passes ``claim=False``; a stop-gate-created claim would survive the idle
    session and suppress the very work it just surfaced on the next Stop attempt
    (codex P1). When ``claim=False`` the active-claim suppression check below is
    also skipped so the passive probe always reports the pending work.
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
        owner_pr, owner_blockers = _resolve_and_blockers(cwd)
        if owner_blockers:
            return 0, _blocked_defer_text(
                pr_number=owner_pr.number,
                blockers=owner_blockers,
                owner_desc=f"live PR-watch owner session {owner}",
            )
        return 0, f"deferring to live PR-watch owner session {owner}"

    # Resolve PR + blockers (thread-aware). Purely read, no state written.
    pr, blockers = _resolve_and_blockers(cwd)

    if pr is pr_state._GH_UNAVAILABLE:
        return 2, pr_state._gh_unavailable_message(cwd)
    if pr is None:
        return 0, "no open PR for this branch"

    # Never service a DRAFT — the author marked it not-ready.
    if pr.is_draft:
        return 0, "PR is a draft; nothing pending"

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

    coordinator_claim_id: str | None = None
    coordinator_fingerprint: str | None = None
    if claim:
        # Merge live unresolved threads into review_comments for accurate fingerprinting.
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

        # A claim owned by THIS session must be SERVICED, not deferred to: the
        # caller already holds it and the blockers are confirmed, so deferring to
        # one's own active claim hides the work for the whole lease window
        # (BOU-1785 repro). `new_feedback` only catches a CHANGED fingerprint; a
        # claim made for the SAME still-unresolved blockers slips through it.
        active_owner = coordinator.active_claim_owner_for_pr(pr)
        self_owned = active_owner is not None and active_owner.session_id == owner_session_id

        coord_decision = coordinator.dispatch_decision_for_pr(pr)
        if not coord_decision.should_dispatch and not new_feedback and not self_owned:
            # Preserve the coordinator's own state + reason (e.g. the
            # manual_intervention "owner worktree has dirty/unpushed changes:
            # <path>" guidance, or the active-claim "claim is active") so the
            # operator still learns WHY this defers — wrapping it in the
            # blockers + NOT-clean framing rather than replacing it (codex PR #48).
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
