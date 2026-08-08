"""Typed, process-local GitHub GraphQL quota accounting for observation work."""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import RLock
from typing import Final


UTCClock = Callable[[], datetime]
ROLLING_WINDOW: Final[timedelta] = timedelta(hours=1)
DEFAULT_BACKGROUND_HOURLY_BUDGET: Final[int] = 500
DEFAULT_MAINTENANCE_RESERVE: Final[int] = 1000


def _nonnegative_env_int(names: tuple[str, ...], default: int) -> int:
    for name in names:
        raw = os.environ.get(name)
        if raw is None:
            continue
        try:
            value = int(raw.strip())
        except ValueError:
            return default
        return value if value >= 0 else default
    return default


class QuotaCaller(str, Enum):
    """Logical caller used to attribute GitHub GraphQL usage."""

    DASHBOARD = "dashboard"
    OPERATOR = "operator"
    MAINTENANCE = "maintenance"
    POLLER = "dashboard"


class QuotaWorkClass(str, Enum):
    """Work class used to apply the shared quota policy."""

    BACKGROUND_OBSERVATION = "background_observation"
    EXPLICIT_OPERATOR = "explicit_operator"
    MAINTENANCE_GATE = "maintenance_gate"
    BACKGROUND = "background_observation"
    OPERATOR = "explicit_operator"
    MAINTENANCE = "maintenance_gate"


class QuotaDecisionReason(str, Enum):
    """Stable reasons for allowing or deferring a GraphQL request."""

    ALLOWED = "allowed"
    BACKGROUND_HOURLY_BUDGET = "background_hourly_budget"
    BACKGROUND_BUDGET = "background_hourly_budget"
    MAINTENANCE_RESERVE = "maintenance_reserve"
    RESERVE_PROTECTED = "maintenance_reserve"
    NO_REMAINING_QUOTA = "no_remaining_quota"
    QUOTA_EXHAUSTED = "no_remaining_quota"
    BACKOFF = "backoff"


_DERIVED_DENIAL_REASONS: Final[frozenset[QuotaDecisionReason]] = frozenset({
    QuotaDecisionReason.NO_REMAINING_QUOTA,
    QuotaDecisionReason.MAINTENANCE_RESERVE,
    QuotaDecisionReason.BACKGROUND_HOURLY_BUDGET,
})


@dataclass(frozen=True, slots=True)
class RateLimitSnapshot:
    """Most recent GraphQL ``rateLimit`` sample."""

    cost: int
    remaining: int
    reset_at: datetime
    limit: int
    observed_at: datetime
    caller: QuotaCaller | None = None
    work_class: QuotaWorkClass | None = None

    @property
    def resetAt(self) -> datetime:  # noqa: N802 - mirrors GitHub's field name
        """GraphQL-shaped spelling for callers at the API boundary."""

        return self.reset_at


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    """Decision made before starting a potentially expensive GraphQL read."""

    allowed: bool
    reason: QuotaDecisionReason
    degraded: bool = False
    backoff_until: datetime | None = None

    @property
    def denied(self) -> bool:
        return not self.allowed


@dataclass(frozen=True, slots=True)
class QuotaTelemetry:
    """Read-only quota status suitable for logs or an API response."""

    latest: RateLimitSnapshot | None
    rolling_cost_by_caller: dict[QuotaCaller, int]
    rolling_cost_by_work_class: dict[QuotaWorkClass, int]
    request_count: int
    cache_hit_count: int
    cache_hit_rate: float
    background_hourly_spend: int
    background_hourly_budget: int
    maintenance_reserve: int
    backoff_until: datetime | None
    backoff_reason: str | None
    degraded: bool
    degraded_reason: str | QuotaDecisionReason | None
    last_decision: QuotaDecision | None
    last_denial_reason: QuotaDecisionReason | None

    @property
    def snapshot(self) -> RateLimitSnapshot | None:
        """Alias for consumers that call the latest sample a snapshot."""

        return self.latest

    @property
    def total_request_count(self) -> int:
        """Count GraphQL requests and cache-served observation requests."""

        return self.request_count + self.cache_hit_count

    @property
    def cache_hits(self) -> int:
        return self.cache_hit_count

    @property
    def backoff_active(self) -> bool:
        return self.backoff_until is not None


@dataclass(frozen=True, slots=True)
class _Usage:
    at: datetime
    caller: QuotaCaller
    work_class: QuotaWorkClass
    cost: int


@dataclass(frozen=True, slots=True)
class QuotaReservation:
    """An atomic in-flight admission awaiting request reconciliation."""

    reservation_id: int
    caller: QuotaCaller
    work_class: QuotaWorkClass
    estimated_cost: int
    reserved_at: datetime


@dataclass(frozen=True, slots=True)
class QuotaContext:
    """Typed attribution context for a GraphQL observation caller."""

    ledger: QuotaLedger
    caller: QuotaCaller
    work_class: QuotaWorkClass
    estimated_cost: int = 1

    def __post_init__(self) -> None:
        if self.estimated_cost < 1:
            raise ValueError("estimated_cost must be at least one")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("quota clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _parse_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not isinstance(value, str):
        raise TypeError("reset_at must be a timezone-aware ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _as_utc(parsed)


class QuotaLedger:
    """Track GraphQL cost and gate background work before it starts.

    The ledger is intentionally in-process.  GitHub's ``remaining`` value is
    the shared installation-wide signal; local rolling counters provide the
    attribution and hourly dashboard budget needed to keep a poller from
    consuming the entire shared bucket.
    """

    def __init__(
        self,
        *,
        clock: UTCClock | None = None,
        background_hourly_budget: int | None = None,
        maintenance_reserve: int | None = None,
    ) -> None:
        if background_hourly_budget is None:
            background_hourly_budget = _nonnegative_env_int(
                (
                    "APD_GRAPHQL_BACKGROUND_HOURLY_BUDGET",
                    "AGENTIC_PR_DASH_GRAPHQL_BACKGROUND_HOURLY_BUDGET",
                ),
                DEFAULT_BACKGROUND_HOURLY_BUDGET,
            )
        if maintenance_reserve is None:
            maintenance_reserve = _nonnegative_env_int(
                (
                    "APD_GRAPHQL_MAINTENANCE_RESERVE",
                    "AGENTIC_PR_DASH_GRAPHQL_MAINTENANCE_RESERVE",
                ),
                DEFAULT_MAINTENANCE_RESERVE,
            )
        if background_hourly_budget < 0:
            raise ValueError("background_hourly_budget must not be negative")
        if maintenance_reserve < 0:
            raise ValueError("maintenance_reserve must not be negative")
        self._clock = _utc_now if clock is None else clock
        self._background_hourly_budget = background_hourly_budget
        self._maintenance_reserve = maintenance_reserve
        self._latest: RateLimitSnapshot | None = None
        self._usage: list[_Usage] = []
        self._request_count = 0
        self._cache_hit_count = 0
        self._cache_hits_by_caller: defaultdict[QuotaCaller, int] = defaultdict(int)
        self._cache_hits_by_work_class: defaultdict[QuotaWorkClass, int] = defaultdict(int)
        self._backoff_until: datetime | None = None
        self._backoff_reason: str | None = None
        self._degraded = False
        self._degraded_reason: str | QuotaDecisionReason | None = None
        self._failure_active = False
        self._failure_reason: str | None = None
        self._denied_estimates: dict[
            tuple[QuotaDecisionReason, QuotaWorkClass], int
        ] = {}
        self._last_decision: QuotaDecision | None = None
        self._last_denial_reason: QuotaDecisionReason | None = None
        self._lock = RLock()
        self._reservations: dict[int, QuotaReservation] = {}
        self._next_reservation_id = 1

    @property
    def background_hourly_budget(self) -> int:
        return self._background_hourly_budget

    @property
    def maintenance_reserve(self) -> int:
        return self._maintenance_reserve

    @property
    def latest(self) -> RateLimitSnapshot | None:
        with self._lock:
            return self._latest

    @property
    def latest_snapshot(self) -> RateLimitSnapshot | None:
        with self._lock:
            return self._latest

    @property
    def reservation_count(self) -> int:
        with self._lock:
            return len(self._reservations)

    def _now(self) -> datetime:
        return _as_utc(self._clock())

    def _prune(self, now: datetime | None = None) -> datetime:
        current = self._now() if now is None else _as_utc(now)
        cutoff = current - ROLLING_WINDOW
        self._usage = [entry for entry in self._usage if entry.at > cutoff]
        if self._backoff_until is not None and current >= self._backoff_until:
            self._backoff_until = None
            self._backoff_reason = None
            if self._degraded_reason is QuotaDecisionReason.BACKOFF:
                self._degraded_reason = None
        self._refresh_degraded(current)
        return current

    def _effective_remaining(self, now: datetime) -> int | None:
        latest = self._latest
        if latest is None or now >= latest.reset_at:
            return None
        return latest.remaining

    def _reserved_cost(
        self,
        *,
        work_class: QuotaWorkClass | None = None,
    ) -> int:
        return sum(
            reservation.estimated_cost
            for reservation in self._reservations.values()
            if work_class is None or reservation.work_class is work_class
        )

    def _denied_threshold_is_admissible(
        self,
        reason: QuotaDecisionReason,
        work_class: QuotaWorkClass,
        estimated_cost: int,
        projected_remaining: int | None,
        background_spend: int,
    ) -> bool:
        if reason is QuotaDecisionReason.NO_REMAINING_QUOTA:
            return (
                projected_remaining is None
                or projected_remaining >= estimated_cost
            )
        if reason is QuotaDecisionReason.MAINTENANCE_RESERVE:
            if work_class is not QuotaWorkClass.BACKGROUND_OBSERVATION:
                return True
            return (
                projected_remaining is None
                or projected_remaining - estimated_cost >= self._maintenance_reserve
            )
        if reason is QuotaDecisionReason.BACKGROUND_HOURLY_BUDGET:
            if work_class is not QuotaWorkClass.BACKGROUND_OBSERVATION:
                return True
            return (
                background_spend + estimated_cost
                <= self._background_hourly_budget
            )
        return True

    def _refresh_degraded(self, now: datetime) -> None:
        if self._failure_active:
            self._degraded = True
            self._degraded_reason = self._failure_reason
            return
        if self._backoff_until is not None and now < self._backoff_until:
            self._degraded = True
            if self._degraded_reason is None:
                self._degraded_reason = QuotaDecisionReason.BACKOFF
            return
        remaining = self._effective_remaining(now)
        projected_remaining = (
            None if remaining is None else remaining - self._reserved_cost()
        )
        background_spend = self._background_spend(now) + self._reserved_cost(
            work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        )
        active_denials: set[QuotaDecisionReason] = set()
        for key, estimated_cost in tuple(self._denied_estimates.items()):
            reason, work_class = key
            if self._denied_threshold_is_admissible(
                reason,
                work_class,
                estimated_cost,
                projected_remaining,
                background_spend,
            ):
                del self._denied_estimates[key]
            else:
                active_denials.add(reason)
        for reason in (
            QuotaDecisionReason.NO_REMAINING_QUOTA,
            QuotaDecisionReason.MAINTENANCE_RESERVE,
            QuotaDecisionReason.BACKGROUND_HOURLY_BUDGET,
        ):
            if reason in active_denials:
                self._degraded = True
                self._degraded_reason = reason
                return
        if (
            projected_remaining is not None
            and projected_remaining <= self._maintenance_reserve
        ):
            self._degraded = True
            self._degraded_reason = QuotaDecisionReason.MAINTENANCE_RESERVE
            return
        if (
            background_spend >= self._background_hourly_budget
            and (background_spend > 0 or self._background_hourly_budget == 0)
        ):
            self._degraded = True
            self._degraded_reason = QuotaDecisionReason.BACKGROUND_HOURLY_BUDGET
            return
        if self._degraded_reason in {
            QuotaDecisionReason.BACKGROUND_HOURLY_BUDGET,
            QuotaDecisionReason.MAINTENANCE_RESERVE,
            QuotaDecisionReason.NO_REMAINING_QUOTA,
        }:
            self._degraded_reason = None
        if self._degraded_reason is not None:
            self._degraded = True
            return
        self._degraded = False

    def allow(
        self,
        caller: QuotaCaller,
        work_class: QuotaWorkClass,
        estimated_cost: int = 1,
    ) -> QuotaDecision:
        with self._lock:
            return self._allow_locked(caller, work_class, estimated_cost)

    def _allow_locked(
        self,
        caller: QuotaCaller,
        work_class: QuotaWorkClass,
        estimated_cost: int,
    ) -> QuotaDecision:
        """Return whether a request may spend ``estimated_cost`` points.

        Background observation is the first class to defer: it respects both
        the local hourly budget and the maintenance reserve.  Explicit
        operator and maintenance-gate calls may consume the reserve, but are
        still denied when GitHub reports no remaining primary quota.
        """

        if estimated_cost < 1:
            raise ValueError("estimated_cost must be at least one")
        now = self._prune()
        remaining = self._effective_remaining(now)
        reserved_cost = self._reserved_cost()
        projected_remaining = (
            None if remaining is None else remaining - reserved_cost
        )
        if projected_remaining is not None and (
            projected_remaining <= 0 or projected_remaining < estimated_cost
        ):
            return self._deny(
                QuotaDecisionReason.NO_REMAINING_QUOTA,
                now,
                work_class=work_class,
                estimated_cost=estimated_cost,
            )

        if (
            work_class is QuotaWorkClass.BACKGROUND_OBSERVATION
            and self._backoff_until is not None
            and now < self._backoff_until
        ):
            return self._deny(
                QuotaDecisionReason.BACKOFF,
                now,
                work_class=work_class,
            )

        background_spend = self._background_spend(now) + self._reserved_cost(
            work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        )
        if (
            work_class is QuotaWorkClass.BACKGROUND_OBSERVATION
            and background_spend + estimated_cost > self._background_hourly_budget
        ):
            return self._deny(
                QuotaDecisionReason.BACKGROUND_HOURLY_BUDGET,
                now,
                work_class=work_class,
                estimated_cost=estimated_cost,
            )

        if (
            work_class is QuotaWorkClass.BACKGROUND_OBSERVATION
            and projected_remaining is not None
            and projected_remaining - estimated_cost < self._maintenance_reserve
        ):
            return self._deny(
                QuotaDecisionReason.MAINTENANCE_RESERVE,
                now,
                work_class=work_class,
                estimated_cost=estimated_cost,
            )

        decision = QuotaDecision(True, QuotaDecisionReason.ALLOWED, self._degraded)
        self._last_decision = decision
        return decision

    def reserve(
        self,
        caller: QuotaCaller,
        work_class: QuotaWorkClass,
        estimated_cost: int = 1,
    ) -> QuotaReservation | None:
        """Atomically admit a request and reserve its estimated cost."""

        with self._lock:
            decision = self._allow_locked(caller, work_class, estimated_cost)
            if not decision.allowed:
                return None
            now = self._now()
            reservation = QuotaReservation(
                reservation_id=self._next_reservation_id,
                caller=caller,
                work_class=work_class,
                estimated_cost=estimated_cost,
                reserved_at=now,
            )
            self._next_reservation_id += 1
            self._reservations[reservation.reservation_id] = reservation
            self._refresh_degraded(now)
            return reservation

    reserve_request = reserve

    def release(self, reservation: QuotaReservation) -> bool:
        """Release an in-flight reservation when its request did not complete."""

        with self._lock:
            current = self._reservations.get(reservation.reservation_id)
            if current != reservation:
                return False
            del self._reservations[reservation.reservation_id]
            self._refresh_degraded(self._now())
            return True

    def _deny(
        self,
        reason: QuotaDecisionReason,
        now: datetime,
        *,
        work_class: QuotaWorkClass | None = None,
        estimated_cost: int | None = None,
    ) -> QuotaDecision:
        decision = QuotaDecision(
            allowed=False,
            reason=reason,
            degraded=True,
            backoff_until=self._backoff_until,
        )
        self._last_decision = decision
        self._last_denial_reason = reason
        if (
            reason in _DERIVED_DENIAL_REASONS
            and work_class is not None
            and estimated_cost is not None
        ):
            key = (reason, work_class)
            self._denied_estimates[key] = max(
                self._denied_estimates.get(key, 0),
                estimated_cost,
            )
        if self._failure_active:
            self._degraded_reason = self._failure_reason
        else:
            self._degraded_reason = reason
        self._degraded = True
        return decision

    def record_graphql(
        self,
        caller: QuotaCaller,
        work_class: QuotaWorkClass,
        cost: int,
        remaining: int,
        reset_at: datetime | str,
        limit: int,
        reservation: QuotaReservation | None = None,
    ) -> RateLimitSnapshot:
        with self._lock:
            return self._record_graphql_locked(
                caller,
                work_class,
                cost,
                remaining,
                reset_at,
                limit,
                reservation,
            )

    def _record_graphql_locked(
        self,
        caller: QuotaCaller,
        work_class: QuotaWorkClass,
        cost: int,
        remaining: int,
        reset_at: datetime | str,
        limit: int,
        reservation: QuotaReservation | None,
    ) -> RateLimitSnapshot:
        """Record one GraphQL request and its top-level ``rateLimit`` sample."""

        if cost < 0 or remaining < 0 or limit < 0:
            raise ValueError("GraphQL rate-limit values must not be negative")
        now = self._prune()
        snapshot = RateLimitSnapshot(
            cost=cost,
            remaining=remaining,
            reset_at=_parse_datetime(reset_at),
            limit=limit,
            observed_at=now,
            caller=caller,
            work_class=work_class,
        )
        if reservation is not None:
            current = self._reservations.get(reservation.reservation_id)
            if current != reservation:
                raise ValueError("unknown or already reconciled quota reservation")
            del self._reservations[reservation.reservation_id]
        self._latest = snapshot
        self._usage.append(_Usage(now, caller, work_class, cost))
        self._request_count += 1
        self._failure_active = False
        self._failure_reason = None
        self._degraded_reason = None
        if self._backoff_until is None:
            self._backoff_reason = None
        self._refresh_degraded(now)
        return snapshot

    # Explicit name for callers that receive the top-level GraphQL object.
    record_observation = record_graphql
    record = record_graphql

    def record_cache_hit(
        self, caller: QuotaCaller, work_class: QuotaWorkClass
    ) -> None:
        """Record a conditional REST 304 or equivalent local cache hit."""

        with self._lock:
            self._prune()
            self._cache_hit_count += 1
            self._cache_hits_by_caller[caller] += 1
            self._cache_hits_by_work_class[work_class] += 1
            self._failure_active = False
            self._failure_reason = None
            self._degraded_reason = None
            if self._backoff_until is None:
                self._backoff_reason = None
            self._refresh_degraded(self._now())

    def record_backoff(self, duration: timedelta, *, reason: str) -> None:
        """Record a bounded backoff/degraded interval."""

        with self._lock:
            if duration < timedelta(0):
                raise ValueError("backoff duration must not be negative")
            now = self._prune()
            until = now + duration
            if self._backoff_until is None or until > self._backoff_until:
                self._backoff_until = until
            self._backoff_reason = reason
            self._degraded = True
            if not self._failure_active and self._degraded_reason is None:
                self._degraded_reason = QuotaDecisionReason.BACKOFF
            self._last_decision = QuotaDecision(
                allowed=False,
                reason=QuotaDecisionReason.BACKOFF,
                degraded=True,
                backoff_until=until,
            )
            self._last_denial_reason = QuotaDecisionReason.BACKOFF

    def record_failure(self, *, reason: str, count_request: bool = True) -> None:
        """Mark a failed observation without pretending it was clean."""

        with self._lock:
            self._prune()
            if count_request:
                self._request_count += 1
            self._backoff_reason = reason
            self._degraded = True
            self._degraded_reason = reason
            self._failure_active = True
            self._failure_reason = reason

    def clear_degraded(self) -> None:
        """Explicitly recover after a caller has handled a degraded read."""

        with self._lock:
            self._failure_active = False
            self._failure_reason = None
            self._denied_estimates.clear()
            self._degraded_reason = None
            self._degraded = False
            self._last_denial_reason = None
            if self._backoff_until is None:
                self._backoff_reason = None

    recover = clear_degraded

    def reset(self) -> None:
        """Forget all observations and return to an un-degraded state."""

        with self._lock:
            self._latest = None
            self._usage.clear()
            self._request_count = 0
            self._cache_hit_count = 0
            self._cache_hits_by_caller.clear()
            self._cache_hits_by_work_class.clear()
            self._backoff_until = None
            self._backoff_reason = None
            self._degraded = False
            self._degraded_reason = None
            self._failure_active = False
            self._failure_reason = None
            self._denied_estimates.clear()
            self._last_decision = None
            self._last_denial_reason = None
            self._reservations.clear()

    def _background_spend(self, now: datetime) -> int:
        return sum(
            entry.cost
            for entry in self._usage
            if entry.work_class is QuotaWorkClass.BACKGROUND_OBSERVATION
            and entry.at > now - ROLLING_WINDOW
        )

    def telemetry(self) -> QuotaTelemetry:
        with self._lock:
            return self._telemetry_locked()

    def _telemetry_locked(self) -> QuotaTelemetry:
        now = self._prune()
        by_caller: defaultdict[QuotaCaller, int] = defaultdict(int)
        by_class: defaultdict[QuotaWorkClass, int] = defaultdict(int)
        for entry in self._usage:
            by_caller[entry.caller] += entry.cost
            by_class[entry.work_class] += entry.cost
        total_requests = self._request_count + self._cache_hit_count
        return QuotaTelemetry(
            latest=self._latest,
            rolling_cost_by_caller=dict(by_caller),
            rolling_cost_by_work_class=dict(by_class),
            request_count=self._request_count,
            cache_hit_count=self._cache_hit_count,
            cache_hit_rate=(
                self._cache_hit_count / total_requests if total_requests else 0.0
            ),
            background_hourly_spend=self._background_spend(now),
            background_hourly_budget=self._background_hourly_budget,
            maintenance_reserve=self._maintenance_reserve,
            backoff_until=self._backoff_until,
            backoff_reason=self._backoff_reason,
            degraded=self._degraded,
            degraded_reason=self._degraded_reason,
            last_decision=self._last_decision,
            last_denial_reason=self._last_denial_reason,
        )


def ledger_from_environment(
    *,
    clock: UTCClock | None = None,
) -> QuotaLedger:
    """Create a ledger using deployment-configurable observation budgets."""

    return QuotaLedger(
        clock=clock,
        background_hourly_budget=_nonnegative_env_int(
            ("APD_GRAPHQL_BACKGROUND_HOURLY_BUDGET",),
            DEFAULT_BACKGROUND_HOURLY_BUDGET,
        ),
        maintenance_reserve=_nonnegative_env_int(
            ("APD_GRAPHQL_MAINTENANCE_RESERVE",),
            DEFAULT_MAINTENANCE_RESERVE,
        ),
    )


# Short names are convenient for call sites that use the typed contract as a
# generic quota policy rather than this package's specific implementation.
Caller = QuotaCaller
WorkClass = QuotaWorkClass
Decision = QuotaDecision
Snapshot = RateLimitSnapshot
Ledger = QuotaLedger
Reservation = QuotaReservation
GraphQLQuotaContext = QuotaContext
