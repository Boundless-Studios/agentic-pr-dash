"""agent-coordinator integration for dashboard-triggered PR maintenance.

The dashboard may notice that a PR needs work before an in-worktree agent does.
This module claims a stable "PR maintenance" task through ``agent-coordinator``
so repeated dashboard polls do not enqueue duplicate fixes and so another live
owner can defer cleanly. It owns claim identity, blocker fingerprints, and
release/adoption helpers; it does not inspect GitHub directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlparse

from agent_coordinator.models import ClaimRecord, OwnerIdentity, TaskIdentity
from agent_coordinator.service import ClaimConflictError, TaskCoordinator
from agent_coordinator.store import JsonlClaimStore

from .config import load as load_config
from . import maintenance
from .models import ClaimHandle, PRData

TASK_TYPE = "pr-maintenance"
STORE_ENV = "AGENTIC_PR_DASH_COORDINATOR_STORE"


@dataclass(frozen=True)
class DispatchDecision:
    should_dispatch: bool
    state: str
    reason: str
    claim_id: str | None = None
    owner_session_id: str | None = None
    owner_pid: int | None = None


def store_path() -> Path:
    configured = os.environ.get(STORE_ENV)
    if configured:
        return Path(configured)
    return Path.home() / ".agent-coordinator" / "claims.jsonl"


def _coordinator() -> TaskCoordinator:
    return TaskCoordinator(JsonlClaimStore(store_path()))


def _repo_slug_for_pr(pr: PRData) -> str:
    parsed = urlparse(str(getattr(pr, "url", "") or ""))
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc.lower() == "github.com" and len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    worktree_path = getattr(pr, "worktree_path", None)
    if worktree_path:
        repo = load_config(worktree_path).resolved_repo(Path(worktree_path))
        if repo:
            return repo
    repo = load_config().resolved_repo()
    return repo or "unknown/unknown"


def fingerprint_for_pr(pr: PRData) -> str:
    status_value = getattr(getattr(pr, "status", None), "value", "")
    merge_state = getattr(pr, "merge_state", "unknown")
    payload = {
        "blockers": sorted(maintenance.blockers_for_pr(pr)),
        "failing_checks": sorted(str(check) for check in getattr(pr, "failing_checks", [])),
        "merge_conflict": merge_state == "DIRTY" or status_value == "merge_conflict",
        "review_comment_ids": sorted(int(comment.id) for comment in getattr(pr, "review_comments", [])),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def task_identity_for_pr(pr: PRData) -> TaskIdentity:
    return TaskIdentity(
        task_type=TASK_TYPE,
        task_id=f"github:{_repo_slug_for_pr(pr)}#{pr.number}",
        fingerprint=fingerprint_for_pr(pr),
    )


def _porcelain_unquote(path: str) -> str:
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        return path[1:-1]
    return path


def _porcelain_paths(line: str) -> list[str]:
    """All path components of one ``git status --porcelain`` line, unquoted.

    Rename/copy lines (``R``/``C``) carry both sides as ``old -> new``. Both
    are returned so a rename *out of* a real tracked file into the state dir
    (or the legacy handoff filename) still registers the source path: such a
    line counts as loop-generated only when every side is a loop artifact.
    """
    body = line[3:]
    if " -> " in body:
        old, new = body.split(" -> ", 1)
        return [_porcelain_unquote(old), _porcelain_unquote(new)]
    return [_porcelain_unquote(body)]


def _is_loop_artifact(path: str, state_dir_name: str) -> bool:
    """True for files the maintenance loop/dashboard writes itself.

    The loop's own artifacts must never count as worktree dirt for reclaim
    purposes, or every handoff write self-wedges the claim into
    ``manual_intervention`` (BOU-2184): the handoff prompt (legacy root-level
    ``MAINTENANCE_HANDOFF.md`` copies included) and anything under the
    worktree's configured state dir (``.agentic-pr-dash/`` by default, where
    markers, maintenance state, and the handoff now live).
    """
    if path == maintenance.HANDOFF_FILENAME:
        return True
    if state_dir_name:
        prefix = state_dir_name.rstrip("/")
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def worktree_has_dirty_or_unpushed_changes(path: str) -> bool:
    worktree = Path(path)
    if not worktree.is_dir():
        return False
    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    try:
        state_dir_name = load_config(str(worktree)).state_dir_name
    except Exception:  # noqa: BLE001 - unparseable config must not break reclaim
        state_dir_name = ""
    dirty_lines = [
        line
        for line in status.stdout.splitlines()
        if line
        and not all(
            _is_loop_artifact(p, state_dir_name) for p in _porcelain_paths(line)
        )
    ]
    if status.returncode == 0 and dirty_lines:
        return True
    branch = subprocess.run(
        ["git", "-C", str(worktree), "branch", "--show-current"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if branch.returncode != 0:
        return False
    branch_name = branch.stdout.strip()
    upstream_ref = ""
    if branch_name:
        candidate_ref = f"refs/remotes/origin/{branch_name}"
        remote_branch = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--verify", "--quiet", candidate_ref],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if remote_branch.returncode == 0:
            upstream_ref = candidate_ref
    if not upstream_ref:
        upstream = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if upstream.returncode != 0:
            return False
        upstream_ref = upstream.stdout.strip()
    ahead = subprocess.run(
        ["git", "-C", str(worktree), "rev-list", "--count", f"{upstream_ref}..HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if ahead.returncode != 0:
        return False
    try:
        return int(ahead.stdout.strip() or "0") > 0
    except ValueError:
        return False


def _decisions_for_pr_task(pr: PRData) -> list:
    """Every decision recorded against this PR, ignoring the blocker fingerprint.

    Same reasoning as :func:`_best_active_claim_for_pr` (BOU-1637): a decision is
    recorded against the task identity that was current when the executor asked,
    and that identity carries the blocker fingerprint. A review comment landing
    while we wait changes the fingerprint, so a full-identity lookup would stop
    finding the decision — and dispatch would resume straight back into the
    boundary nobody has ruled on yet. Match on ``task_id`` alone.
    """
    task_id = task_identity_for_pr(pr).task_id
    return [
        record
        for record in _coordinator().list_decisions()
        if record.request.task.task_id == task_id
    ]


def pending_decision_for_pr(pr: PRData):
    """The decision blocking this PR, if a human still owes an answer.

    Returns ``None`` once the decision is resolved, cancelled, or superseded —
    an explicit human act is the only thing that lifts the wait. Nothing about
    elapsed time is consulted, by design (BOU-2039).
    """
    waiting = [record for record in _decisions_for_pr_task(pr) if record.is_blocking]
    if not waiting:
        return None
    return max(waiting, key=lambda record: record.request.created_at)


def resolved_decision_for_pr(pr: PRData):
    """The answered-but-not-yet-resumed decision for this PR, if any.

    Carries the human's selected option/direction so a redispatched run can cite
    the direction it was given rather than re-deriving it.
    """
    from agent_coordinator.models import DecisionState  # noqa: PLC0415

    resumable = [
        record
        for record in _decisions_for_pr_task(pr)
        if record.state is DecisionState.RESUMABLE
    ]
    if not resumable:
        return None
    return max(resumable, key=lambda record: record.request.created_at)


def decision_by_id_for_pr(pr: PRData, decision_id: str):
    """A single decision for this PR by id, fingerprint-independent."""
    for record in _decisions_for_pr_task(pr):
        if record.request.decision_id == decision_id:
            return record
    return None


def record_task_resume(pr: PRData, record, session_id: str) -> bool:
    """Record ``task_resumed`` for a resolved decision under the PR's live claim.

    Closes the coordinator's resume protocol so the completed work can cite the
    direction it was given. Returns False (without raising) when there is no
    live claim to resume under, or when the epoch fence rejects us — a deposed
    owner must not be able to record a resume.
    """
    claim = _best_active_claim_for_pr(pr)
    if claim is None:
        return False
    try:
        _coordinator().resume_task(
            record.request.decision_id,
            claim_id=claim.claim_id,
            owner_session_id=claim.owner.session_id,
            lease_epoch=claim.lease_epoch,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def decision_block_reason(pr: PRData) -> str | None:
    """Why this PR must not be dispatched right now, or None.

    The single gate BOTH the automatic poll path and the manual dashboard
    actions consult. ``/api/fix-comments`` and ``/api/retry-ci`` reach
    ``dispatch_pr_maintenance`` without going through
    ``dispatch_decision_for_pr``, so gating only the latter would let a button
    click queue an executor against an unresolved boundary (PR #109 review).
    """
    waiting = pending_decision_for_pr(pr)
    if waiting is None:
        return None
    request = waiting.request
    return (
        f"PR #{pr.number} is waiting on a human {request.category} decision "
        f"{request.decision_id}: {request.question}"
    )


def dispatch_decision_for_pr(pr: PRData, *, now: datetime | None = None) -> DispatchDecision:
    # BOU-2040: a question owed to a human outranks every claim consideration
    # below. Checking it first keeps the loop from reading "reclaimable" and
    # re-dispatching an executor that will hit the same boundary and stop again.
    # This is a WAITING state, not a failure: callers must not count it toward an
    # executor-failure streak or escalate on it.
    waiting = pending_decision_for_pr(pr)
    if waiting is not None:
        request = waiting.request
        return DispatchDecision(
            should_dispatch=False,
            state="waiting_human",
            reason=(
                f"PR #{pr.number} is waiting on a human {request.category} "
                f"decision {request.decision_id} "
                f"(asked by {request.requesting_runtime}): {request.question}"
            ),
            claim_id=request.claim_id,
            owner_session_id=request.requesting_session_id,
            owner_pid=None,
        )

    decision = _coordinator().status(task_identity_for_pr(pr), now=now)
    claim = decision.claim
    if decision.reclaimable and claim and claim.owner.worktree_path:
        if worktree_has_dirty_or_unpushed_changes(claim.owner.worktree_path):
            comment_count = len(getattr(pr, "review_comments", []) or [])
            comment_label = "comment" if comment_count == 1 else "comments"
            pending_context = (
                f"PR #{pr.number} has {comment_count} unaddressed review {comment_label}; "
                if comment_count
                else f"PR #{pr.number} has reclaimable maintenance work; "
            )
            return DispatchDecision(
                should_dispatch=False,
                state="manual_intervention",
                reason=(
                    pending_context
                    + "claim is reclaimable but owner worktree has dirty or "
                    f"unpushed changes: {claim.owner.worktree_path}"
                ),
                claim_id=claim.claim_id,
                owner_session_id=claim.owner.session_id,
                owner_pid=claim.owner.pid,
            )
    return DispatchDecision(
        should_dispatch=decision.reclaimable,
        state=decision.state.value,
        reason=decision.reason,
        claim_id=claim.claim_id if claim else None,
        owner_session_id=claim.owner.session_id if claim else None,
        owner_pid=claim.owner.pid if claim else None,
    )


def _best_active_claim_for_pr(pr: PRData, *, now: datetime | None = None) -> ClaimRecord | None:
    """The latest live, unexpired, unreleased ACTIVE claim for this PR's task_id,
    ignoring the PR's CURRENT fingerprint (BOU-1637).

    ``TaskCoordinator.status`` keys a claim by the FULL task identity (task_id +
    fingerprint), so a status() lookup with the round-2 fingerprint can't see a
    still-active round-1 claim whose fingerprint differs. To answer "is the
    in-flight claim for the SAME blocker set?" and "who owns it?", we look up the
    active claim by task_id alone. Returns None when no live, unexpired,
    unreleased claim exists for the PR's task_id — i.e. nothing to defer to.
    """
    coord = _coordinator()
    timestamp = now or datetime.now(timezone.utc)
    target_id = task_identity_for_pr(pr).task_id
    best: ClaimRecord | None = None
    best_key = None
    for claim in coord._claims_by_id().values():  # noqa: SLF001 — same package contract
        if claim.task.task_id != target_id:
            continue
        if claim.status != "active":
            continue
        if timestamp >= claim.lease_expires_at:
            continue
        if not coord.pid_is_live(claim.owner.pid):
            continue
        key = (claim.claimed_at, claim.heartbeat_at, claim.claim_id)
        if best_key is None or key > best_key:
            best, best_key = claim, key
    return best


def active_claim_fingerprint_for_pr(pr: PRData, *, now: datetime | None = None) -> str | None:
    """Fingerprint of the latest ACTIVE claim for this PR's task_id (see
    ``_best_active_claim_for_pr``). None when there is no claim to defer to."""
    best = _best_active_claim_for_pr(pr, now=now)
    return best.task.fingerprint if best is not None else None


def active_claim_owner_for_pr(pr: PRData, *, now: datetime | None = None) -> OwnerIdentity | None:
    """Owner of the latest ACTIVE claim for this PR's task_id, fingerprint-
    agnostic (see ``_best_active_claim_for_pr``). Lets ``check`` tell a claim it
    OWNS itself (service the known blockers) from a foreign session's claim
    (warn-only defer). None when there is no claim to defer to."""
    best = _best_active_claim_for_pr(pr, now=now)
    return best.owner if best is not None else None


def claim_pr(
    pr: PRData,
    *,
    session_id: str,
    pid: int | None,
    agent: str,
    lease_seconds: int,
) -> ClaimHandle | None:
    owner = OwnerIdentity(
        session_id=session_id,
        pid=pid,
        agent=agent,
        worktree_path=getattr(pr, "worktree_path", None),
    )
    try:
        record = _coordinator().claim_task(
            task_identity_for_pr(pr),
            owner,
            lease_seconds=lease_seconds,
        )
        return ClaimHandle(
            claim_id=record.claim_id,
            lease_epoch=record.lease_epoch,
        )
    except ClaimConflictError:
        return None


def heartbeat_claim(
    handle: ClaimHandle,
    session_id: str,
    *,
    lease_seconds: int | None = None,
) -> ClaimHandle:
    _coordinator().heartbeat_claim(
        handle.claim_id,
        owner_session_id=session_id,
        lease_epoch=handle.lease_epoch,
        lease_seconds=lease_seconds if lease_seconds is not None else load_config().lease_seconds,
    )
    return handle


def release_claim(handle: ClaimHandle, session_id: str, reason: str) -> ClaimHandle:
    _coordinator().release_claim(
        handle.claim_id,
        owner_session_id=session_id,
        lease_epoch=handle.lease_epoch,
        reason=reason,
    )
    return handle
