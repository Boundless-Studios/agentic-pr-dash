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
    # doing any work (cheap, before resolving the PR). See _live_foreign_owner.
    owner = markers._live_foreign_owner(cwd, self_session_id)
    if owner is not None:
        return 0, f"deferring to live PR-watch owner session {owner}"

    # Resolve PR
    pr = pr_state._resolve_pr_for_branch(cwd)

    if pr is pr_state._GH_UNAVAILABLE:
        return 2, pr_state._gh_unavailable_message(cwd)
    if pr is None:
        return 0, "no open PR for this branch"

    # Never service a DRAFT — the author marked it not-ready.
    if pr.is_draft:
        return 0, "PR is a draft; nothing pending"

    # Check for blockers — no state written, purely read
    blockers = maintenance.blockers_for_pr(pr)

    # Consult review threads directly when nothing else flags it.
    if not blockers:
        unresolved_threads = pr_state._unresolved_review_threads(pr.number, cwd)
        if unresolved_threads:
            pr.review_comments = completion._review_comments_from_threads(unresolved_threads)
            blockers = ["review_comments"]

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
    # heartbeat/lease (BOU-1540).
    if worktrees._live_independent_owner_paths([cwd], self_session_id):
        return 0, "deferring to live independent owner of this worktree"

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

        coord_decision = coordinator.dispatch_decision_for_pr(pr)
        if not coord_decision.should_dispatch and not new_feedback:
            return 0, f"deferring to agent-coordinator {coord_decision.state}: {coord_decision.reason}"

        claimed = coordinator.claim_pr(
            pr,
            session_id=owner_session_id,
            pid=_common._resolve_owner_pid(),
            agent="agentic-pr-dash-check",
            lease_seconds=_common._fix_lease_seconds(),
        )
        if claimed is None and not new_feedback:
            return 0, "deferring to active agent-coordinator claim"
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
