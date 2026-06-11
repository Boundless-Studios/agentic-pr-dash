"""agent-coordinator integration for PR maintenance ownership."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
from .models import PRData

TASK_TYPE = "pr-maintenance"
STORE_ENV = "AGENTIC_PR_DASH_COORDINATOR_STORE"


@dataclass(frozen=True)
class DispatchDecision:
    should_dispatch: bool
    state: str
    reason: str
    claim_id: str | None = None


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
    if status.returncode == 0 and status.stdout.strip():
        return True
    upstream = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if upstream.returncode != 0:
        return False
    ahead = subprocess.run(
        ["git", "-C", str(worktree), "rev-list", "--count", "@{u}..HEAD"],
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


def dispatch_decision_for_pr(pr: PRData, *, now: datetime | None = None) -> DispatchDecision:
    decision = _coordinator().status(task_identity_for_pr(pr), now=now)
    claim = decision.claim
    if decision.reclaimable and claim and claim.owner.worktree_path:
        if worktree_has_dirty_or_unpushed_changes(claim.owner.worktree_path):
            return DispatchDecision(
                should_dispatch=False,
                state="manual_intervention",
                reason=(
                    "claim is reclaimable but owner worktree has dirty or "
                    f"unpushed changes: {claim.owner.worktree_path}"
                ),
                claim_id=claim.claim_id,
            )
    return DispatchDecision(
        should_dispatch=decision.reclaimable,
        state=decision.state.value,
        reason=decision.reason,
        claim_id=claim.claim_id if claim else None,
    )


def claim_pr(
    pr: PRData,
    *,
    session_id: str,
    pid: int | None,
    agent: str,
    lease_seconds: int,
) -> ClaimRecord | None:
    owner = OwnerIdentity(
        session_id=session_id,
        pid=pid,
        agent=agent,
        worktree_path=getattr(pr, "worktree_path", None),
    )
    try:
        return _coordinator().claim_task(
            task_identity_for_pr(pr),
            owner,
            lease_seconds=lease_seconds,
        )
    except ClaimConflictError:
        return None


def heartbeat_claim_id(claim_id: str, session_id: str, *, lease_seconds: int | None = None) -> None:
    _coordinator().heartbeat_claim(
        claim_id,
        owner_session_id=session_id,
        lease_seconds=lease_seconds if lease_seconds is not None else load_config().lease_seconds,
    )


def release_claim_id(claim_id: str, session_id: str, reason: str) -> None:
    _coordinator().release_claim(
        claim_id,
        owner_session_id=session_id,
        reason=reason,
    )
