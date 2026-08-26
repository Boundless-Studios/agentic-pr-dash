"""Typed, provider-neutral contracts for pull-request lifecycle state."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _required_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("value must be a non-blank string")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return _required_text(value)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def canonical_repository(value: str) -> str:
    """Return a trimmed owner/name identity while retaining its casing."""

    candidate = _required_text(value).strip("/")
    if "://" in candidate:
        candidate = candidate.split("//", 1)[1].split("/", 1)[-1]
    candidate = candidate.removesuffix("/").removesuffix(".git")
    pieces = [piece for piece in candidate.split("/") if piece]
    if len(pieces) >= 2:
        candidate = "/".join(pieces[-2:])
    return _required_text(candidate)


class ObservationHealthV1(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class MaintenanceBlockerV1(StrEnum):
    REQUIRED_CI_FAILED = "required_ci_failed"
    REQUIRED_CI_PENDING = "required_ci_pending"
    MERGE_CONFLICT = "merge_conflict"
    REVIEW_FINDINGS = "review_findings"
    OBSERVATION_UNAVAILABLE = "observation_unavailable"
    NO_PR = "no_pr"
    UNKNOWN = "unknown"


class MaintenanceNextActionV1(StrEnum):
    WAIT_FOR_CI = "wait_for_ci"
    FIX_CI = "fix_ci"
    ADDRESS_REVIEW = "address_review"
    RESOLVE_CONFLICT = "resolve_conflict"
    RETRY_OBSERVATION = "retry_observation"
    CREATE_PR = "create_pr"
    NONE = "none"


class RequiredCIStateV1(StrEnum):
    PASSING = "passing"
    PENDING = "pending"
    FAILING = "failing"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    NOT_REQUIRED = "not_required"


class MergeabilityStateV1(StrEnum):
    MERGEABLE = "mergeable"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class ReviewStateV1(StrEnum):
    CLEAN = "clean"
    CHANGES_REQUESTED = "changes_requested"
    PENDING = "pending"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class IntentLifecycleStateV1(StrEnum):
    PENDING = "pending"
    NO_PR = "no_pr"
    PROMOTED = "promoted"


class EnqueueStatusV1(StrEnum):
    ENQUEUED = "enqueued"
    DUPLICATE = "duplicate"
    REACTIVATED = "reactivated"


class SnapshotReadStatusV1(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"
    INVALID = "invalid"


class MaintenanceIntentV1(BaseModel):
    """One push-triggered maintenance intent, including unresolved PR events."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str
    pushed_ref: str
    head_sha: str
    workflow_type: str
    reason: str
    worktree_path: str
    session_id: str
    requested_at: datetime
    pr_number: int | None = Field(default=None, gt=0)

    @field_validator("repository", mode="before")
    @classmethod
    def _repository(cls, value: str) -> str:
        return canonical_repository(value)

    @field_validator(
        "pushed_ref",
        "head_sha",
        "workflow_type",
        "reason",
        "worktree_path",
        "session_id",
        mode="before",
    )
    @classmethod
    def _text(cls, value: Any) -> str:
        return _required_text(value)

    @field_validator("requested_at", mode="after")
    @classmethod
    def _requested_at_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @property
    def normalized_repository(self) -> str:
        return self.repository.casefold()


class MaintenanceKeyV1(BaseModel):
    """Immutable identity of one exact repository/PR/head/workflow job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str
    pr_number: int = Field(gt=0)
    head_sha: str
    workflow_type: str

    @field_validator("repository", mode="before")
    @classmethod
    def _repository(cls, value: str) -> str:
        return canonical_repository(value)

    @field_validator("head_sha", "workflow_type", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return _required_text(value)

    @property
    def normalized_repository(self) -> str:
        return self.repository.casefold()


class MaintenanceSnapshotV1(BaseModel):
    """A validated observation for one exact PR generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: MaintenanceKeyV1
    observed_at: datetime
    observation_health: ObservationHealthV1
    blockers: list[MaintenanceBlockerV1]
    next_actions: list[MaintenanceNextActionV1]
    required_ci_state: RequiredCIStateV1
    mergeability: MergeabilityStateV1
    review_state: ReviewStateV1
    policy_unsettled_finding_count: int = Field(ge=0)
    raw_unresolved_thread_count: int = Field(ge=0)
    unaddressed_thread_count: int = Field(ge=0)
    stable_observation_count: int = Field(ge=0)
    stable_observation_first_at: datetime | None
    stable_observation_last_at: datetime | None
    settled: bool

    @field_validator("observed_at", mode="after")
    @classmethod
    def _observed_at_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator(
        "stable_observation_first_at", "stable_observation_last_at", mode="after"
    )
    @classmethod
    def _stable_at_utc(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None

    @model_validator(mode="after")
    def _validate_settlement(self) -> MaintenanceSnapshotV1:
        if (
            self.stable_observation_first_at
            and self.stable_observation_last_at
            and self.stable_observation_first_at > self.stable_observation_last_at
        ):
            raise ValueError("stable observation timestamps must be ordered")
        if not self.settled:
            return self
        if self.observation_health is not ObservationHealthV1.HEALTHY:
            raise ValueError("an unsettled observation cannot be marked settled")
        if self.required_ci_state is not RequiredCIStateV1.PASSING:
            raise ValueError("settled observations require passing required CI")
        if self.mergeability is not MergeabilityStateV1.MERGEABLE:
            raise ValueError("settled observations require mergeability")
        if self.review_state is not ReviewStateV1.CLEAN:
            raise ValueError("settled observations require clean review state")
        if self.blockers or self.next_actions:
            raise ValueError("settled observations cannot have blockers or actions")
        if (
            self.policy_unsettled_finding_count
            or self.raw_unresolved_thread_count
            or self.unaddressed_thread_count
        ):
            raise ValueError("settled observations cannot have open findings")
        if self.stable_observation_count < 2:
            raise ValueError("settled observations require stable observations")
        if not self.stable_observation_first_at or not self.stable_observation_last_at:
            raise ValueError("settled observations require stable timestamps")
        return self


class MaintenanceTargetV1(BaseModel):
    """Either an exact key or an unresolved ingress lookup."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: MaintenanceKeyV1 | None = None
    repository: str | None = None
    pushed_ref: str | None = None
    head_sha: str | None = None
    workflow_type: str | None = None
    pr_number: int | None = Field(default=None, gt=0)

    @field_validator("repository", mode="before")
    @classmethod
    def _repository(cls, value: str | None) -> str | None:
        return canonical_repository(value) if value is not None else None

    @field_validator(
        "pushed_ref", "head_sha", "workflow_type", mode="before"
    )
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        return _optional_text(value)

    @model_validator(mode="after")
    def _validate_target(self) -> MaintenanceTargetV1:
        lookup = (
            self.repository,
            self.pushed_ref,
            self.head_sha,
            self.workflow_type,
        )
        has_lookup = any(value is not None for value in lookup)
        if self.key is not None:
            if has_lookup or self.pr_number is not None:
                raise ValueError("exact targets cannot contain lookup fields")
            return self
        if self.pr_number is not None:
            if any(value is None for value in lookup):
                raise ValueError("exact target fields must be complete")
            return self
        if not all(value is not None for value in lookup):
            raise ValueError(
                "unresolved targets require repository, ref, head, workflow"
            )
        return self

    @classmethod
    def exact(cls, key: MaintenanceKeyV1) -> MaintenanceTargetV1:
        return cls(key=key)

    @classmethod
    def unresolved(
        cls,
        *,
        repository: str,
        pushed_ref: str,
        head_sha: str,
        workflow_type: str,
    ) -> MaintenanceTargetV1:
        return cls(
            repository=repository,
            pushed_ref=pushed_ref,
            head_sha=head_sha,
            workflow_type=workflow_type,
        )

    @property
    def is_exact(self) -> bool:
        return self.key is not None or self.pr_number is not None

    @property
    def exact_key(self) -> MaintenanceKeyV1 | None:
        if self.key is not None:
            return self.key
        if self.pr_number is None:
            return None
        return MaintenanceKeyV1(
            repository=self.repository or "",
            pr_number=self.pr_number,
            head_sha=self.head_sha or "",
            workflow_type=self.workflow_type or "",
        )

    @property
    def normalized_repository(self) -> str:
        repository = self.key.repository if self.key else self.repository
        return (repository or "").casefold()


class MaintenanceIntentRecordV1(BaseModel):
    """Durable state for one ingress identity and its optional promotion link."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ingress_id: str = Field(min_length=1)
    intent: MaintenanceIntentV1
    state: IntentLifecycleStateV1
    canonical_key: MaintenanceKeyV1 | None = None


class EnqueueResultV1(BaseModel):
    """Typed outcome of enqueueing an ingress intent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: EnqueueStatusV1
    intent: MaintenanceIntentV1
    state: IntentLifecycleStateV1
    ingress_id: str = Field(min_length=1)


class MaintenanceSnapshotReadResultV1(BaseModel):
    """Typed fresh/stale/missing/invalid snapshot read outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: SnapshotReadStatusV1
    snapshot: MaintenanceSnapshotV1 | None = None
    age_seconds: float | None = None
    reason: str = ""
