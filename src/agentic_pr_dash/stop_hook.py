"""Snapshot-only advisory Stop hook for durable PR convergence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from os import PathLike

from .codex_hooks.run_pr_convergence import (
    WORKFLOW_TYPE,
    build_maintenance_intent,
    local_git_identity,
)
from .lifecycle_models import (
    MaintenanceSnapshotReadResultV1,
    MaintenanceTargetV1,
    SnapshotReadStatusV1,
)
from .lifecycle_store import enqueue_maintenance, read_maintenance_snapshot


@dataclass(frozen=True, slots=True)
class StopHookRequest:
    """Local inputs supplied by a host runtime's Stop adapter."""

    cwd: str
    session_id: str = ""
    state_root: str | PathLike[str] | None = None
    max_snapshot_age_seconds: float = 90.0


def _render_advisory(
    result: MaintenanceSnapshotReadResultV1,
    *,
    head_sha: str,
    enqueued: bool,
) -> str:
    details = [
        "[pr-convergence] advisory",
        f"snapshot={result.status.value}",
        f"head={head_sha[:12]}",
    ]
    snapshot = result.snapshot
    if snapshot is not None:
        blockers = ",".join(blocker.value for blocker in snapshot.blockers) or "none"
        actions = ",".join(action.value for action in snapshot.next_actions) or "none"
        details.extend(
            (
                f"blockers={blockers}",
                f"next_actions={actions}",
                f"settled={str(snapshot.settled).lower()}",
            )
        )
    if enqueued:
        details.append("enqueued")
    return " ".join(details)


def run_stop_hook(
    request: StopHookRequest,
    *,
    now: datetime | None = None,
) -> int:
    """Read the current-head snapshot, enqueue stale work, and always allow."""

    try:
        identity = local_git_identity(request.cwd)
        if identity is None:
            print("[pr-convergence] advisory snapshot=invalid local_git_identity=missing")
            return 0
        target = MaintenanceTargetV1.unresolved(
            repository=identity.repository,
            pushed_ref=identity.pushed_ref,
            head_sha=identity.head_sha,
            workflow_type=WORKFLOW_TYPE,
        )
        result = read_maintenance_snapshot(
            target,
            root=request.state_root,
            max_age_seconds=request.max_snapshot_age_seconds,
            now=now,
        )
        enqueued = result.status in {
            SnapshotReadStatusV1.STALE,
            SnapshotReadStatusV1.MISSING,
            SnapshotReadStatusV1.INVALID,
        }
        if enqueued:
            enqueue_maintenance(
                build_maintenance_intent(
                    identity,
                    session_id=request.session_id,
                    reason=f"stop-{result.status.value} maintenance",
                    now=now,
                ),
                root=request.state_root,
            )
        print(
            _render_advisory(
                result,
                head_sha=identity.head_sha,
                enqueued=enqueued,
            )
        )
    except Exception:  # noqa: BLE001 - Stop is advisory under every outage
        print("[pr-convergence] advisory snapshot=invalid local_state=unavailable")
    return 0
