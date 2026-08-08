"""Quota-aware observation scheduling for open pull requests.

The controller keeps invalidation and reconciliation state separate from the
GitHub client.  Callers ask for a plan, perform the requested reads, and then
call :meth:`ObservationController.record_refresh` with the same plan.  That
explicit acknowledgement is what advances polling and reconciliation clocks;
asking for a plan is otherwise side-effect free.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Final


UTCClock = Callable[[], datetime]


class ObservationSlice(str, Enum):
    """The independently refreshable parts of a pull-request observation."""

    METADATA = "metadata"
    REVIEW = "review"
    CI = "ci"


class ObservationReason(str, Enum):
    """Why an observation plan became due."""

    INITIAL = "initial"
    EVENT = "event"
    HEAD_CHANGED = "head_changed"
    CI_POLL = "ci_poll"
    METADATA_RECONCILIATION = "metadata_reconciliation"
    REVIEW_RECONCILIATION = "review_reconciliation"
    FORCED = "forced"


@dataclass(frozen=True, slots=True)
class ObservationKey:
    """Identity of one immutable pull-request head observation."""

    repo: str
    number: int
    head_sha: str

    def __post_init__(self) -> None:
        repo = self.repo.strip().casefold()
        if not repo:
            raise ValueError("repo identity must not be empty")
        if self.number <= 0:
            raise ValueError("pull-request number must be positive")
        if not self.head_sha:
            raise ValueError("head SHA must not be empty")
        object.__setattr__(self, "repo", repo)


@dataclass(frozen=True, slots=True)
class ObservationPlan:
    """Immutable work request returned by the controller."""

    key: ObservationKey
    slices: frozenset[ObservationSlice]
    reason: ObservationReason
    invalidation_generations: frozenset[tuple[ObservationSlice, int]] = frozenset()


@dataclass(slots=True)
class _ObservationState:
    observed_at: dict[ObservationSlice, datetime | None] = field(
        default_factory=lambda: {
            ObservationSlice.METADATA: None,
            ObservationSlice.REVIEW: None,
            ObservationSlice.CI: None,
        }
    )
    ci_pending: bool = False
    invalidated: set[ObservationSlice] = field(default_factory=set)
    invalidated_at: datetime | None = None
    invalidation_reason: ObservationReason | None = None
    invalidation_generations: dict[ObservationSlice, int] = field(
        default_factory=lambda: {
            ObservationSlice.METADATA: 0,
            ObservationSlice.REVIEW: 0,
            ObservationSlice.CI: 0,
        }
    )


_ALL_SLICES: Final[frozenset[ObservationSlice]] = frozenset(ObservationSlice)
_REVIEW_AND_CI: Final[frozenset[ObservationSlice]] = frozenset(
    {ObservationSlice.REVIEW, ObservationSlice.CI}
)
_REVIEW_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "pull_request_review",
        "pull_request_review_comment",
        "pull_request_review_thread",
    }
)
_CI_EVENTS: Final[frozenset[str]] = frozenset({"check_run", "check_suite"})
_HEAD_CHANGING_ACTIONS: Final[frozenset[str]] = frozenset(
    {"opened", "reopened", "synchronize"}
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime, rejecting ambiguous naive clocks."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observation clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _validate_interval(name: str, value: timedelta) -> timedelta:
    if value < timedelta(0):
        raise ValueError(f"{name} must not be negative")
    return value


class ObservationController:
    """Schedule immutable-head PR metadata, review, and CI observations.

    ``clock`` is an injectable UTC clock so all due-time behavior can be
    evaluated without sleeping.  A plan does not mark work complete; callers
    must acknowledge completed reads with ``record_refresh``.  Reconciliation
    intervals are measured from the last acknowledged metadata refresh and
    from the last acknowledged refresh containing both review and CI.
    """

    def __init__(
        self,
        *,
        clock: UTCClock | None = None,
        debounce_window: timedelta = timedelta(seconds=2),
        ci_poll_interval: timedelta = timedelta(seconds=30),
        metadata_reconciliation_interval: timedelta = timedelta(minutes=15),
        review_reconciliation_interval: timedelta = timedelta(hours=1),
    ) -> None:
        self._clock: UTCClock = _utc_now if clock is None else clock
        self._debounce_window = _validate_interval(
            "debounce_window", debounce_window
        )
        self._ci_poll_interval = _validate_interval(
            "ci_poll_interval", ci_poll_interval
        )
        self._metadata_reconciliation_interval = _validate_interval(
            "metadata_reconciliation_interval", metadata_reconciliation_interval
        )
        self._review_reconciliation_interval = _validate_interval(
            "review_reconciliation_interval", review_reconciliation_interval
        )
        self._states: dict[ObservationKey, _ObservationState] = {}

    def plan_for(
        self,
        repo: str,
        number: int,
        head_sha: str,
        *,
        now: datetime | None = None,
        ci_pending: bool | None = None,
        force: bool = False,
    ) -> ObservationPlan | None:
        """Return due work for a repository, PR number, and immutable head.

        ``ci_pending`` is the latest CI status when the caller has one.  If it
        is omitted, the status recorded by the last CI refresh is used.  The
        optional current value lets a caller suppress a stale pending poll
        after it has independently observed terminal CI.
        """

        key = ObservationKey(repo, number, head_sha)
        observed_at = self._observation_time(now)
        state = self._states.get(key)
        if force:
            return ObservationPlan(
                key,
                _ALL_SLICES,
                ObservationReason.FORCED,
                self._invalidation_snapshot(state, _ALL_SLICES),
            )

        if state is None:
            return ObservationPlan(key, _ALL_SLICES, ObservationReason.INITIAL)

        if state.invalidated:
            invalidated_at = state.invalidated_at
            if invalidated_at is None:
                raise RuntimeError("invalidated observation has no debounce timestamp")
            if observed_at < invalidated_at + self._debounce_window:
                return None
            reason = state.invalidation_reason
            if reason is None:
                raise RuntimeError("invalidated observation has no reason")
            if not any(
                timestamp is not None for timestamp in state.observed_at.values()
            ):
                return ObservationPlan(
                    key,
                    _ALL_SLICES,
                    reason,
                    self._invalidation_snapshot(state, _ALL_SLICES),
                )
            return ObservationPlan(
                key,
                frozenset(state.invalidated),
                reason,
                self._invalidation_snapshot(state, state.invalidated),
            )

        pending = state.ci_pending if ci_pending is None else ci_pending
        ci_observed_at = state.observed_at[ObservationSlice.CI]
        metadata_at = state.observed_at[ObservationSlice.METADATA]
        review_observed_at = state.observed_at[ObservationSlice.REVIEW]
        review_due = review_observed_at is None or (
            observed_at
            >= review_observed_at + self._review_reconciliation_interval
        )
        ci_due = ci_observed_at is None or (
            pending
            and observed_at >= ci_observed_at + self._ci_poll_interval
        ) or (
            not pending
            and observed_at
            >= ci_observed_at + self._review_reconciliation_interval
        )
        metadata_due = metadata_at is None or (
            observed_at >= metadata_at + self._metadata_reconciliation_interval
        )
        due_slices: set[ObservationSlice] = set()
        if review_due:
            due_slices.add(ObservationSlice.REVIEW)
        if ci_due:
            due_slices.add(ObservationSlice.CI)
        if due_slices:
            if review_due:
                reason = ObservationReason.REVIEW_RECONCILIATION
            elif ci_due and pending:
                reason = ObservationReason.CI_POLL
            else:
                reason = ObservationReason.REVIEW_RECONCILIATION
            slices = frozenset(due_slices)
            return ObservationPlan(
                key,
                slices,
                reason,
                self._invalidation_snapshot(state, slices),
            )
        if metadata_due:
            slices = frozenset({ObservationSlice.METADATA})
            return ObservationPlan(
                key,
                slices,
                ObservationReason.METADATA_RECONCILIATION,
                self._invalidation_snapshot(state, slices),
            )
        return None

    def record_refresh(
        self,
        plan: ObservationPlan,
        *,
        now: datetime | None = None,
        ci_pending: bool = False,
    ) -> None:
        """Acknowledge the slices read for ``plan`` at ``now``.

        ``ci_pending`` is applied only when the plan contains the CI slice.
        Recording metadata alone therefore cannot accidentally turn a known
        pending CI observation into a terminal one.
        """

        observed_at = self._observation_time(now)
        state = self._states.setdefault(plan.key, _ObservationState())
        generations = dict(plan.invalidation_generations)
        for observation_slice in plan.slices:
            state.observed_at[observation_slice] = observed_at
            plan_generation = generations.get(observation_slice)
            if plan_generation is not None and (
                state.invalidation_generations[observation_slice] == plan_generation
            ):
                state.invalidated.discard(observation_slice)

        if ObservationSlice.CI in plan.slices:
            state.ci_pending = ci_pending
        if not state.invalidated:
            state.invalidated_at = None
            state.invalidation_reason = None

    def handle_event(
        self,
        event_name: str,
        repo: str,
        number: int,
        head_sha: str,
        *,
        action: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Invalidate only the observation slices affected by a GitHub event.

        Review events invalidate review, check events invalidate CI,
        non-head-changing pull-request actions invalidate metadata, and
        head-changing pull-request actions invalidate all slices.  Unknown
        event families do nothing.  Every accepted event restarts the
        two-second debounce window; several events in that window are returned
        as one plan with their union of slices.
        """

        normalized_event = event_name.strip().casefold()
        normalized_action = action.strip().casefold() if action is not None else None
        if normalized_event in _REVIEW_EVENTS:
            slices = frozenset({ObservationSlice.REVIEW})
            reason = ObservationReason.EVENT
        elif normalized_event in _CI_EVENTS:
            slices = frozenset({ObservationSlice.CI})
            reason = ObservationReason.EVENT
        elif normalized_event == "pull_request":
            if normalized_action in _HEAD_CHANGING_ACTIONS:
                slices = _ALL_SLICES
                reason = ObservationReason.HEAD_CHANGED
            else:
                slices = frozenset({ObservationSlice.METADATA})
                reason = ObservationReason.EVENT
        else:
            return

        key = ObservationKey(repo, number, head_sha)
        observed_at = self._observation_time(now)
        state = self._states.setdefault(key, _ObservationState())
        state.invalidated.update(slices)
        for observation_slice in slices:
            state.invalidation_generations[observation_slice] += 1
        state.invalidated_at = observed_at
        if (
            reason is ObservationReason.HEAD_CHANGED
            or state.invalidation_reason is None
        ):
            state.invalidation_reason = reason

    def prune(self, active_keys: Iterable[ObservationKey]) -> None:
        """Drop state for closed/missing PR heads.

        ``active_keys`` should contain every currently open PR head across the
        repositories represented by this controller.  A changed head naturally
        removes its previous key when the caller supplies only the current key.
        """

        active = frozenset(active_keys)
        for key in tuple(self._states):
            if key not in active:
                del self._states[key]

    def tracked_keys(self) -> frozenset[ObservationKey]:
        """Return the immutable-head keys currently held by the controller."""

        return frozenset(self._states)

    def _observation_time(self, now: datetime | None) -> datetime:
        return _as_utc(self._clock() if now is None else now)

    def now(self) -> datetime:
        """Return the controller's timezone-aware current observation time."""
        return self._observation_time(None)

    @property
    def metadata_reconciliation_interval(self) -> timedelta:
        """Configured interval for repository metadata reconciliation."""
        return self._metadata_reconciliation_interval

    @staticmethod
    def _invalidation_snapshot(
        state: _ObservationState | None,
        slices: Iterable[ObservationSlice],
    ) -> frozenset[tuple[ObservationSlice, int]]:
        if state is None:
            return frozenset()
        return frozenset(
            (observation_slice, state.invalidation_generations[observation_slice])
            for observation_slice in slices
        )
