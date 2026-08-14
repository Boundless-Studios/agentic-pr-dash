"""Head-scoped ownership for PR finalization mutations."""

from __future__ import annotations

from collections.abc import Callable

from agent_coordinator.models import OwnerIdentity, TaskIdentity
from agent_coordinator.service import ClaimConflictError, TaskCoordinator
from agent_coordinator.store import JsonlClaimStore
from pydantic import BaseModel, ConfigDict, Field

FINALIZATION_TASK_TYPE = "pr-finalization"


class FinalizationKey(BaseModel):
    """Immutable identity of one pull-request generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(min_length=1)
    pr_number: int = Field(gt=0)
    head_sha: str = Field(min_length=1)


class FinalizationActor(BaseModel):
    """Process identity requesting exclusive finalization authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(min_length=1)
    pid: int | None = Field(default=None, gt=0)
    agent: str = Field(min_length=1)
    worktree_path: str | None = None


class FinalizationLease(BaseModel):
    """Fencing token for one actor mutating one exact PR head."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: FinalizationKey
    owner_session_id: str
    claim_id: str
    lease_epoch: int = Field(ge=0)


class LeaseAcquisition(BaseModel):
    """Typed acquisition result; conflicts are ordinary outcomes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    acquired: bool
    lease: FinalizationLease | None = None
    conflict_session_id: str | None = None
    reason: str = ""


class LeaseRelease(BaseModel):
    """Typed fenced-release result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    released: bool
    reason: str = ""


class FinalizationRun(BaseModel):
    """Whether a mutation ran under authority and its process result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executed: bool
    exit_code: int
    reason: str = ""


def finalization_task(key: FinalizationKey) -> TaskIdentity:
    """Return the coordinator task fenced to exactly one PR head."""

    return TaskIdentity(
        task_type=FINALIZATION_TASK_TYPE,
        task_id=f"github:{key.repository}#{key.pr_number}@{key.head_sha}",
        fingerprint=key.head_sha,
    )


class FinalizationLeaseService:
    """Acquire and release exact-head finalization authority."""

    def __init__(self, store: JsonlClaimStore, *, lease_seconds: int = 300) -> None:
        self._coordinator = TaskCoordinator(store)
        self._lease_seconds = lease_seconds

    def acquire(
        self,
        key: FinalizationKey,
        actor: FinalizationActor,
    ) -> LeaseAcquisition:
        owner = OwnerIdentity(
            session_id=actor.session_id,
            pid=actor.pid,
            agent=actor.agent,
            worktree_path=actor.worktree_path,
        )
        try:
            claim = self._coordinator.claim_task(
                finalization_task(key),
                owner,
                lease_seconds=self._lease_seconds,
            )
        except ClaimConflictError as exc:
            holder = exc.decision.claim
            return LeaseAcquisition(
                acquired=False,
                conflict_session_id=(
                    holder.owner.session_id if holder is not None else None
                ),
                reason="finalization authority held by another actor",
            )
        return LeaseAcquisition(
            acquired=True,
            lease=FinalizationLease(
                key=key,
                owner_session_id=actor.session_id,
                claim_id=claim.claim_id,
                lease_epoch=claim.lease_epoch,
            ),
            reason="acquired",
        )

    def release(self, lease: FinalizationLease) -> LeaseRelease:
        try:
            self._coordinator.release_claim(
                lease.claim_id,
                owner_session_id=lease.owner_session_id,
                lease_epoch=lease.lease_epoch,
                reason="finalization_complete",
            )
        except Exception as exc:  # noqa: BLE001 - boundary returns typed refusal
            return LeaseRelease(
                released=False,
                reason=f"{type(exc).__name__}: {exc}",
            )
        return LeaseRelease(released=True, reason="released")


def default_finalization_service() -> FinalizationLeaseService:
    """Use the same durable, bounded claim store as PR ownership."""

    from . import coordinator, ownership

    return FinalizationLeaseService(
        ownership.BoundedLockClaimStore(coordinator.store_path())
    )


def run_with_finalization_lease(
    *,
    key: FinalizationKey,
    actor: FinalizationActor,
    operation: Callable[[], int],
    service: FinalizationLeaseService | None = None,
) -> FinalizationRun:
    """Run one finalization mutation only while holding exact-head authority."""

    lease_service = service or default_finalization_service()
    acquisition = lease_service.acquire(key, actor)
    if acquisition.lease is None:
        return FinalizationRun(
            executed=False,
            exit_code=10,
            reason=acquisition.reason,
        )
    try:
        exit_code = int(operation())
    finally:
        release = lease_service.release(acquisition.lease)
    if not release.released:
        return FinalizationRun(
            executed=True,
            exit_code=2,
            reason=f"finalization lease release failed: {release.reason}",
        )
    return FinalizationRun(executed=True, exit_code=exit_code, reason="completed")
