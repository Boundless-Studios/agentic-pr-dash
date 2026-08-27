"""Dashboard consumer for durable pull-request lifecycle intents."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TypeAlias

from agent_review_coordinator import (
    FindingSettlementState,
    ReviewLedger,
    ReviewPolicy,
    ReviewResult,
    ReviewStage,
)

from . import coordinator, github_api
from ._maintenance import completion, deferred_review
from ._maintenance.review_settlement import (
    FinalizationObservation,
    combine_clean_observations,
    evaluate_pr_snapshot,
)
from .config import load as load_config
from .lifecycle_models import (
    IntentLifecycleStateV1,
    MaintenanceBlockerV1,
    MaintenanceIntentRecordV1,
    MaintenanceKeyV1,
    MaintenanceNextActionV1,
    MaintenanceSnapshotV1,
    MaintenanceTargetV1,
    MergeabilityStateV1,
    ObservationHealthV1,
    RequiredCIStateV1,
    ReviewStateV1,
)
from .lifecycle_store import LifecycleStore, StaleIntentVersionError
from .models import PRData, ReviewComment

ReviewContext: TypeAlias = tuple[ReviewPolicy, ReviewLedger]
ReviewContextLoader: TypeAlias = Callable[
    [MaintenanceIntentRecordV1], ReviewContext | None
]


class ReviewContextUnavailableError(RuntimeError):
    """Configured review settlement files exist but cannot be trusted."""


def load_review_context(record: MaintenanceIntentRecordV1) -> ReviewContext | None:
    """Load the worktree's review policy and ledger for one lifecycle intent."""

    return load_review_context_for_worktree(record.intent.worktree_path)


def load_review_context_for_worktree(
    worktree_path: str | Path,
) -> ReviewContext | None:
    """Load the review policy and ledger rooted at ``worktree_path``."""

    cwd = Path(worktree_path)
    policy_path = _review_file(
        cwd,
        "AGENTIC_PR_DASH_REVIEW_POLICY",
        ("config/review-policy.yaml", "config/agent-review-policy.yaml"),
    )
    ledger_path = _review_file(
        cwd,
        "AGENTIC_PR_DASH_REVIEW_LEDGER",
        (".agentic-review/ledger.json", ".agentic-pr-dash/review-ledger.json"),
    )
    if policy_path is None and ledger_path is None:
        return None
    if policy_path is None or ledger_path is None:
        raise ReviewContextUnavailableError(
            f"review context is partially configured in {cwd}"
        )
    try:
        policy = ReviewPolicy.from_yaml(policy_path.read_text(encoding="utf-8"))
        ledger = ReviewLedger.model_validate_json(
            ledger_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ReviewContextUnavailableError(
            f"review context is invalid or unreadable in {cwd}"
        ) from exc
    return policy, ledger


def _review_file(cwd: Path, env_name: str, candidates: tuple[str, ...]) -> Path | None:
    configured = os.environ.get(env_name, "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else cwd / path
    for candidate in candidates:
        path = cwd / candidate
        if path.is_file():
            return path
    return None


@dataclass(frozen=True, slots=True)
class LifecycleDrainResult:
    """Summary of one bounded dashboard lifecycle drain."""

    examined: int = 0
    progressed: int = 0
    no_pr: int = 0
    deferred: int = 0
    failed: int = 0


class _DispatchTarget(Protocol):
    async def dispatch_pr_maintenance(self, pr: PRData) -> None: ...


@dataclass(frozen=True, slots=True)
class _ResolvedPR:
    payload: dict[str, object]
    repository: str
    number: int
    head_sha: str


ResolvedRecord: TypeAlias = tuple[MaintenanceIntentRecordV1, _ResolvedPR]


@dataclass(frozen=True, slots=True)
class _ObservedPR:
    observation: FinalizationObservation
    pr: PRData
    raw_unresolved_thread_count: int
    unaddressed_thread_count: int


class LifecycleWorkflow:
    """Consume durable intents and project exact-head maintenance snapshots."""

    def __init__(
        self,
        store: LifecycleStore,
        *,
        policy: ReviewPolicy | None = None,
        ledger: ReviewLedger | None = None,
        context_loader: ReviewContextLoader | None = None,
        orchestrator: _DispatchTarget | None = None,
        maintenance_author: str | None = None,
        now: Callable[[], datetime] | None = None,
        batch_size: int = 16,
        resolution_concurrency: int = 4,
        stabilization_interval: timedelta = timedelta(seconds=30),
        retry_interval: timedelta = timedelta(seconds=30),
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if resolution_concurrency <= 0:
            raise ValueError("resolution_concurrency must be positive")
        if stabilization_interval < timedelta(0):
            raise ValueError("stabilization_interval must not be negative")
        if retry_interval <= timedelta(0):
            raise ValueError("retry_interval must be positive")
        if (policy is None) != (ledger is None):
            raise ValueError("policy and ledger must be provided together")
        if policy is None and context_loader is None:
            raise ValueError("a review context or context loader is required")
        self.store = store
        self.policy = policy
        self.ledger = ledger
        self.context_loader = context_loader
        self.orchestrator = orchestrator
        self.maintenance_author = maintenance_author
        self._now = now or (lambda: datetime.now(UTC))
        self.batch_size = batch_size
        self.retry_interval = retry_interval
        self.stabilization_interval = stabilization_interval
        self._resolution_semaphore = asyncio.Semaphore(resolution_concurrency)
        self._drain_lock = asyncio.Lock()

    async def drain(self) -> LifecycleDrainResult:
        """Process at most ``batch_size`` pending or unresolved intents."""
        async with self._drain_lock:
            return await self._drain_locked()

    async def _drain_locked(self) -> LifecycleDrainResult:
        records = self.store.list_intents(
            states={
                IntentLifecycleStateV1.PENDING,
                IntentLifecycleStateV1.NO_PR,
                IntentLifecycleStateV1.PROMOTED,
            },
            eligible_at=self._now(),
        )[: self.batch_size]
        result = LifecycleDrainResult()
        resolved_records: list[tuple[MaintenanceIntentRecordV1, _ResolvedPR]] = []
        resolutions = await asyncio.gather(
            *(self._resolve_off_loop(record) for record in records),
            return_exceptions=True,
        )
        for record, resolved in zip(records, resolutions, strict=True):
            if isinstance(resolved, BaseException):
                if not isinstance(resolved, Exception):
                    raise resolved
                self._persist_unavailable(record, record.intent.pr_number)
                result = _count_outcome(result, "deferred")
                continue
            try:
                outcome = self._resolution_outcome(record, resolved)
            except StaleIntentVersionError:
                result = _count_outcome(result, "deferred")
                continue
            if outcome is not None:
                result = _count_outcome(result, outcome)
                continue
            assert resolved is not None
            resolved_records.append((record, resolved))

        for group in _group_resolved_records(resolved_records):
            try:
                batch = await self._collect_batch(group)
            except Exception:  # noqa: BLE001 - unavailable observations retry
                for record, resolved in group:
                    self._persist_unavailable(record, resolved.number)
                    result = _count_outcome(result, "deferred")
                continue
            for record, resolved in group:
                observed = batch.observed.get(resolved.number)
                if observed is None:
                    self._persist_unavailable(record, resolved.number)
                    result = _count_outcome(result, "deferred")
                    continue
                try:
                    outcome = await self._consume_observed(record, resolved, observed)
                except Exception:  # noqa: BLE001 - isolate one durable intent
                    outcome = "failed"
                result = _count_outcome(result, outcome)
        return result

    async def _consume(self, record: MaintenanceIntentRecordV1) -> str:
        try:
            resolved = await self._resolve_off_loop(record)
        except Exception:  # noqa: BLE001 - unavailable observations retry
            self._persist_unavailable(record, record.intent.pr_number)
            return "deferred"
        try:
            outcome = self._resolution_outcome(record, resolved)
        except StaleIntentVersionError:
            return "deferred"
        if outcome is not None:
            return outcome
        assert resolved is not None
        return await self._consume_observed(record, resolved)

    def _resolution_outcome(
        self,
        record: MaintenanceIntentRecordV1,
        resolved: _ResolvedPR | None,
    ) -> str | None:
        if resolved is None:
            if record.intent.pr_number is None:
                self.store.mark_no_pr(
                    record.intent,
                    next_attempt_at=self._now() + self.retry_interval,
                    expected_generation=record.generation,
                    expected_revision=record.revision,
                )
                return "no_pr"
            self._persist_unavailable(record, record.intent.pr_number)
            return "deferred"
        if resolved.payload.get("state") == "MERGED":
            key = MaintenanceKeyV1(
                repository=resolved.repository,
                pr_number=resolved.number,
                head_sha=record.intent.head_sha,
                workflow_type=record.intent.workflow_type,
            )
            self.store.settle_intent(
                record.intent,
                key,
                expected_generation=record.generation,
                expected_revision=record.revision,
            )
            return "progressed"
        if (
            resolved.payload.get("state") != "OPEN"
            or resolved.payload.get("isDraft") is not False
        ):
            self._persist_unavailable(record, resolved.number)
            return "deferred"
        if resolved.repository.casefold() != record.intent.repository.casefold():
            self._persist_unavailable(record, resolved.number)
            return "deferred"
        if resolved.head_sha != record.intent.head_sha:
            key = MaintenanceKeyV1(
                repository=resolved.repository,
                pr_number=resolved.number,
                head_sha=record.intent.head_sha,
                workflow_type=record.intent.workflow_type,
            )
            self.store.settle_intent(
                record.intent,
                key,
                expected_generation=record.generation,
                expected_revision=record.revision,
            )
            return "progressed"
        return None

    async def _collect_batch(
        self,
        records: list[tuple[MaintenanceIntentRecordV1, _ResolvedPR]],
    ) -> github_api.PrMaintenanceSnapshotBatch:
        """Collect one aggregate observation for all resolved records in a repo."""

        first_record, first_resolved = records[0]
        owner, repo = first_resolved.repository.split("/", 1)
        numbers = sorted({resolved.number for _, resolved in records})
        return await asyncio.to_thread(
            github_api.collect_pr_maintenance_snapshots,
            owner,
            repo,
            numbers,
            cwd=first_record.intent.worktree_path,
        )

    async def _consume_observed(
        self,
        record: MaintenanceIntentRecordV1,
        resolved: _ResolvedPR,
        aggregate: github_api.PrMaintenanceSnapshot | None = None,
    ) -> str:
        try:
            observed = await self._observe(record, resolved, aggregate)
        except Exception:  # noqa: BLE001 - unavailable observations retry
            self._persist_unavailable(record, resolved.number)
            return "deferred"
        if observed is None:
            self._persist_unavailable(record, resolved.number)
            return "deferred"
        key = MaintenanceKeyV1(
            repository=resolved.repository,
            pr_number=resolved.number,
            head_sha=record.intent.head_sha,
            workflow_type=record.intent.workflow_type,
        )
        snapshot = self._snapshot(record, key, observed)
        try:
            if snapshot.settled:
                self.store.settle_intent(
                    record.intent,
                    key,
                    expected_generation=record.generation,
                    expected_revision=record.revision,
                    snapshot=snapshot,
                )
            else:
                self.store.promote_intent(
                    record.intent,
                    key,
                    next_attempt_at=self._now() + self.retry_interval,
                    expected_generation=record.generation,
                    expected_revision=record.revision,
                    snapshot=snapshot,
                )
        except StaleIntentVersionError:
            return "deferred"
        if (
            self.orchestrator is not None
            and _actionable(snapshot)
            and _dispatch_allowed(observed.pr, record.intent)
        ):
            await self.orchestrator.dispatch_pr_maintenance(observed.pr)
        return "progressed"

    def _previous_snapshot(self, key: MaintenanceKeyV1) -> MaintenanceSnapshotV1 | None:
        result = self.store.read_snapshot(
            MaintenanceTargetV1.exact(key),
            max_age_seconds=10**12,
            now=_utc(self._now()),
        )
        return result.snapshot if result.snapshot is not None else None

    def _persist_unavailable(
        self, record: MaintenanceIntentRecordV1, pr_number: int | None
    ) -> None:
        snapshot = None
        if pr_number is not None:
            key = MaintenanceKeyV1(
                repository=record.intent.repository,
                pr_number=pr_number,
                head_sha=record.intent.head_sha,
                workflow_type=record.intent.workflow_type,
            )
            snapshot = _head_drift_snapshot(key, self._now())
        try:
            self.store.schedule_retry(
                record.intent,
                next_attempt_at=self._now() + self.retry_interval,
                expected_generation=record.generation,
                expected_revision=record.revision,
                snapshot=snapshot,
            )
        except StaleIntentVersionError:
            return

    async def _resolve_off_loop(
        self, record: MaintenanceIntentRecordV1
    ) -> _ResolvedPR | None:
        async with self._resolution_semaphore:
            return await asyncio.to_thread(self._resolve, record)

    def _resolve(self, record: MaintenanceIntentRecordV1) -> _ResolvedPR | None:
        intent = record.intent
        cwd = intent.worktree_path
        if intent.pr_number is None:
            branch = intent.pushed_ref.removeprefix("refs/heads/")
            payload = github_api.find_pr_by_head(
                branch, "open", cwd, head_oid=intent.head_sha
            )
            if isinstance(payload, dict):
                payload = {**payload, "state": "OPEN"}
        else:
            payload = github_api.resolve_pr(
                intent.pr_number,
                "number,title,headRefName,headRefOid,baseRefName,url,state,isDraft,"
                "mergeStateStatus,mergeable,reviewDecision,author",
                cwd,
                force=True,
            )
        if not isinstance(payload, dict):
            return None
        number = payload.get("number")
        head_sha = payload.get("headRefOid")
        url = payload.get("url")
        if not isinstance(number, int) or not isinstance(head_sha, str):
            return None
        repository = _repository_from_url(url, intent.repository)
        return _ResolvedPR(payload, repository, number, head_sha)

    async def _observe(
        self,
        record: MaintenanceIntentRecordV1,
        resolved: _ResolvedPR,
        aggregate: github_api.PrMaintenanceSnapshot | None = None,
    ) -> _ObservedPR | None:
        context = self._review_context(record)
        if context is None:
            context = _policy_neutral_context(record, resolved.repository)
        policy, ledger = context
        if aggregate is None:
            owner, repo = resolved.repository.split("/", 1)
            batch = await asyncio.to_thread(
                github_api.collect_pr_maintenance_snapshots,
                owner,
                repo,
                [resolved.number],
                cwd=record.intent.worktree_path,
            )
            aggregate = batch.observed.get(resolved.number)
        observed = aggregate
        if observed is None or observed.head_sha != record.intent.head_sha:
            return None
        pr = _pr_data(resolved, observed, record.intent.worktree_path)
        review_observation = await asyncio.to_thread(
            github_api.get_review_submissions_observation,
            resolved.number,
            record.intent.head_sha,
            record.intent.worktree_path,
            excluded_authors=_excluded_review_authors(pr),
        )
        if not review_observation.observable:
            return None
        deferrals = deferred_review.deferred_threads_for_pr(
            record.intent.worktree_path, resolved.number
        )
        observation = evaluate_pr_snapshot(
            pr=pr,
            policy=policy,
            ledger=ledger,
            threads=observed.unresolved_threads,
            deferrals=deferrals,
            review_observation=review_observation,
            maintenance_author=(
                self.maintenance_author
                if self.maintenance_author is not None
                else load_config(
                    record.intent.worktree_path
                ).maintenance_mutation_identity
            ),
        )
        addressed_thread_ids = set(observation.addressed_thread_ids)
        pr.review_comments = [
            comment
            for comment in pr.review_comments
            if comment.thread_id not in addressed_thread_ids
        ]
        pr.review_comments.extend(_policy_review_comments(observation))
        return _ObservedPR(
            observation,
            pr,
            len(observed.unresolved_threads),
            len(observation.unaddressed_thread_ids),
        )

    def _review_context(
        self, record: MaintenanceIntentRecordV1
    ) -> ReviewContext | None:
        if self.policy is not None and self.ledger is not None:
            return self.policy, self.ledger
        if self.context_loader is None:
            return None
        return self.context_loader(record)

    def _snapshot(
        self,
        record: MaintenanceIntentRecordV1,
        key: MaintenanceKeyV1,
        observed: _ObservedPR,
    ) -> MaintenanceSnapshotV1:
        now = _utc(self._now())
        observation = observed.observation
        policy_count = _policy_unsettled_count(observation)
        blockers, actions = _snapshot_blockers(observation, policy_count, observed)
        current = _build_snapshot(
            key,
            now,
            observation,
            blockers,
            actions,
            policy_count,
            observed,
        )
        clean = _snapshot_is_clean(current, observation)
        if not clean:
            return current
        return self._stabilize_clean(
            key,
            current,
            observation,
            now,
            reset=record.state is not IntentLifecycleStateV1.PROMOTED,
        )

    def _stabilize_clean(
        self,
        key: MaintenanceKeyV1,
        current: MaintenanceSnapshotV1,
        observation: FinalizationObservation,
        now: datetime,
        *,
        reset: bool = False,
    ) -> MaintenanceSnapshotV1:
        previous = None if reset else self._previous_snapshot(key)
        if previous is None or not _same_snapshot_facts(previous, current):
            return _stabilization_pending(current, now)
        first_at = previous.stable_observation_first_at or previous.observed_at
        if now - first_at < self.stabilization_interval:
            return _stabilization_pending(current, now, first_at=first_at)
        if previous.settlement_key != current.settlement_key:
            return _stabilization_pending(current, now)
        combined = combine_clean_observations(
            observation.model_copy(update={"settlement_key": previous.settlement_key}),
            observation.model_copy(update={"settlement_key": current.settlement_key}),
        )
        if not combined.settled:
            return _stabilization_pending(current, now, first_at=first_at)
        return current.model_copy(
            update={
                "stable_observation_count": max(
                    2, previous.stable_observation_count + 1
                ),
                "stable_observation_first_at": first_at,
                "stable_observation_last_at": now,
                "settled": True,
            }
        )


def _group_resolved_records(
    records: list[ResolvedRecord],
) -> tuple[list[ResolvedRecord], ...]:
    groups: dict[str, list[ResolvedRecord]] = {}
    for record, resolved in records:
        groups.setdefault(resolved.repository.casefold(), []).append((record, resolved))
    return tuple(groups.values())


def _snapshot_blockers(
    observation: FinalizationObservation,
    policy_count: int,
    observed: _ObservedPR,
) -> tuple[list[MaintenanceBlockerV1], list[MaintenanceNextActionV1]]:
    blockers, actions = _project_blockers(observation, observed.pr)
    if policy_count or observed.unaddressed_thread_count:
        blockers.append(MaintenanceBlockerV1.REVIEW_FINDINGS)
        actions.append(MaintenanceNextActionV1.ADDRESS_REVIEW)
    return _unique(blockers), _unique(actions)


def _build_snapshot(
    key: MaintenanceKeyV1,
    now: datetime,
    observation: FinalizationObservation,
    blockers: list[MaintenanceBlockerV1],
    actions: list[MaintenanceNextActionV1],
    policy_count: int,
    observed: _ObservedPR,
) -> MaintenanceSnapshotV1:
    clean = _snapshot_is_clean_values(
        observation,
        blockers,
        actions,
        policy_count,
        observed.unaddressed_thread_count,
    )
    return MaintenanceSnapshotV1(
        key=key,
        observed_at=now,
        observation_health=_observation_health(observation, clean),
        blockers=tuple(blockers),
        next_actions=tuple(actions),
        required_ci_state=_required_ci_state(observation),
        mergeability=_mergeability(observation, observed.pr),
        review_state=_review_state(observation),
        policy_unsettled_finding_count=policy_count,
        raw_unresolved_thread_count=observed.raw_unresolved_thread_count,
        unaddressed_thread_count=observed.unaddressed_thread_count,
        settlement_key=_lifecycle_settlement_key(observation, observed.pr),
        stable_observation_count=0,
        stable_observation_first_at=None,
        stable_observation_last_at=None,
        settled=False,
    )


def _snapshot_is_clean(
    snapshot: MaintenanceSnapshotV1, observation: FinalizationObservation
) -> bool:
    return _snapshot_is_clean_values(
        observation,
        list(snapshot.blockers),
        list(snapshot.next_actions),
        snapshot.policy_unsettled_finding_count,
        snapshot.unaddressed_thread_count,
    )


def _lifecycle_settlement_key(observation: FinalizationObservation, pr: PRData) -> str:
    """Fingerprint the detailed evidence omitted by aggregate snapshot fields."""

    payload = {
        "review": observation.settlement_key,
        "ci_checks": [check.model_dump(mode="json") for check in pr.ci_checks],
        "threads": sorted(
            comment.thread_id
            for comment in pr.review_comments
            if comment.thread_id is not None
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _snapshot_is_clean_values(
    observation: FinalizationObservation,
    blockers: list[MaintenanceBlockerV1],
    actions: list[MaintenanceNextActionV1],
    policy_count: int,
    unaddressed_count: int,
) -> bool:
    return (
        observation.clean
        and observation.review_observation_state.value == "observed"
        and not blockers
        and not actions
        and not policy_count
        and not unaddressed_count
    )


def _observation_health(
    observation: FinalizationObservation, clean: bool
) -> ObservationHealthV1:
    if observation.review_observation_state.value != "observed":
        return ObservationHealthV1.PARTIAL
    return ObservationHealthV1.HEALTHY if clean else ObservationHealthV1.UNHEALTHY


def _policy_review_comments(
    observation: FinalizationObservation,
) -> list[ReviewComment]:
    fingerprints = sorted(
        fingerprint
        for fingerprint, state in observation.review.finding_states.items()
        if state is FindingSettlementState.UNRESOLVED
    )
    return [
        ReviewComment(
            id=-(ordinal + 1),
            author="review-policy",
            body=f"Unsettled policy finding {fingerprint}",
            path=".github/pull-request",
            created_at="",
            thread_id=f"policy:{fingerprint}",
        )
        for ordinal, fingerprint in enumerate(fingerprints)
    ]


def _same_snapshot_facts(
    previous: MaintenanceSnapshotV1, current: MaintenanceSnapshotV1
) -> bool:
    return (
        previous.key == current.key
        and previous.observation_health == current.observation_health
        and previous.blockers == current.blockers
        and _without_stabilization_action(previous)
        == _without_stabilization_action(current)
        and previous.required_ci_state == current.required_ci_state
        and previous.mergeability == current.mergeability
        and previous.review_state == current.review_state
        and previous.policy_unsettled_finding_count
        == current.policy_unsettled_finding_count
        and previous.raw_unresolved_thread_count == current.raw_unresolved_thread_count
        and previous.unaddressed_thread_count == current.unaddressed_thread_count
        and previous.settlement_key == current.settlement_key
    )


def _without_stabilization_action(
    snapshot: MaintenanceSnapshotV1,
) -> tuple[MaintenanceNextActionV1, ...]:
    return tuple(
        action
        for action in snapshot.next_actions
        if action is not MaintenanceNextActionV1.RETRY_OBSERVATION
    )


def _stabilization_pending(
    snapshot: MaintenanceSnapshotV1,
    now: datetime,
    *,
    first_at: datetime | None = None,
) -> MaintenanceSnapshotV1:
    return snapshot.model_copy(
        update={
            "next_actions": (MaintenanceNextActionV1.RETRY_OBSERVATION,),
            "stable_observation_count": 1,
            "stable_observation_first_at": first_at or now,
            "stable_observation_last_at": now,
            "settled": False,
        }
    )


def _count_outcome(result: LifecycleDrainResult, outcome: str) -> LifecycleDrainResult:
    values = {
        "examined": result.examined + 1,
        "progressed": result.progressed,
        "no_pr": result.no_pr,
        "deferred": result.deferred,
        "failed": result.failed,
    }
    if outcome in values:
        values[outcome] += 1
    return LifecycleDrainResult(**values)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _repository_from_url(url: object, fallback: str) -> str:
    if isinstance(url, str) and "/pull/" in url:
        return "/".join(url.split("/pull/", 1)[0].rstrip("/").split("/")[-2:])
    return fallback


def _pr_data(
    resolved: _ResolvedPR,
    observed: github_api.PrMaintenanceSnapshot,
    worktree_path: str,
) -> PRData:
    payload = resolved.payload
    return PRData(
        number=resolved.number,
        repo=resolved.repository,
        title=str(payload.get("title") or ""),
        branch=str(payload.get("headRefName") or ""),
        base_branch=str(payload.get("baseRefName") or "main"),
        url=str(payload.get("url") or ""),
        author=(
            str(payload["author"].get("login") or "")
            if isinstance(payload.get("author"), dict)
            else ""
        ),
        is_draft=bool(payload.get("isDraft")),
        merge_state=observed.merge_state,
        mergeable=observed.mergeable,
        review_decision=observed.review_decision,
        latest_commit_sha=observed.head_sha,
        latest_commit_date=observed.head_committed_at,
        ci_checks=list(observed.ci_checks),
        failing_checks=_failing_checks(observed),
        ci_watch_pending=_ci_pending(observed),
        review_comments=completion._review_comments_from_threads(
            observed.unresolved_threads
        ),
        worktree_path=worktree_path,
    )


def _policy_neutral_context(
    record: MaintenanceIntentRecordV1, repository: str
) -> ReviewContext:
    """Represent repositories that have not opted into policy settlement."""

    policy = ReviewPolicy.model_validate(
        {
            "version": 1,
            "review": {
                "local": {"reviewer_count": 1},
                "backstop": {"reviewer_count": 1, "trigger": "new_head_sha"},
            },
        }
    )
    ledger = ReviewLedger(
        repository=repository,
        head_sha=record.intent.head_sha,
        delivery_id=f"policy-neutral:{record.ingress_id}",
        review_charter_version="policy-neutral-v1",
    )
    for stage in (ReviewStage.LOCAL, ReviewStage.BACKSTOP):
        ledger.submit(
            ReviewResult(
                repository=ledger.repository,
                head_sha=ledger.head_sha,
                stage=stage,
                round_number=1,
                slot_number=1,
                reviewer_execution_id=f"policy-neutral-{stage.value}",
            )
        )
    return policy, ledger


def _excluded_review_authors(pr: PRData) -> set[str]:
    """Keep PR-author submissions out of lifecycle review quorum evidence."""

    return {pr.author} if pr.author else set()


def _failing_checks(observed: github_api.PrMaintenanceSnapshot) -> list[str]:
    return [
        check.name
        for check in observed.ci_checks
        if check.status == "completed"
        and (check.conclusion or "").lower() not in {"success", "neutral", "skipped"}
    ]


def _ci_pending(observed: github_api.PrMaintenanceSnapshot) -> bool:
    return observed.required_pending or any(
        check.status in {"queued", "in_progress"} for check in observed.ci_checks
    )


def _project_blockers(
    observation: FinalizationObservation,
    pr: PRData,
) -> tuple[list[MaintenanceBlockerV1], list[MaintenanceNextActionV1]]:
    blockers: list[MaintenanceBlockerV1] = []
    actions: list[MaintenanceNextActionV1] = []
    if observation.review_observation_state.value != "observed":
        blockers.append(MaintenanceBlockerV1.OBSERVATION_UNAVAILABLE)
        actions.append(MaintenanceNextActionV1.RETRY_OBSERVATION)
    for blocker in observation.blockers:
        if blocker == "ci_not_successful":
            blockers.append(MaintenanceBlockerV1.REQUIRED_CI_FAILED)
            actions.append(MaintenanceNextActionV1.FIX_CI)
        elif blocker == "ci_unavailable":
            blockers.append(MaintenanceBlockerV1.OBSERVATION_UNAVAILABLE)
            actions.append(MaintenanceNextActionV1.RETRY_OBSERVATION)
        elif blocker == "ci_pending":
            blockers.append(MaintenanceBlockerV1.REQUIRED_CI_PENDING)
            actions.append(MaintenanceNextActionV1.WAIT_FOR_CI)
        elif blocker == "merge_conflict":
            blockers.append(MaintenanceBlockerV1.MERGE_CONFLICT)
            actions.append(MaintenanceNextActionV1.RESOLVE_CONFLICT)
        elif blocker == "not_mergeable":
            if _merge_conflict_is_known(pr):
                blockers.append(MaintenanceBlockerV1.MERGE_CONFLICT)
                actions.append(MaintenanceNextActionV1.RESOLVE_CONFLICT)
            else:
                blockers.append(MaintenanceBlockerV1.OBSERVATION_UNAVAILABLE)
                actions.append(MaintenanceNextActionV1.RETRY_OBSERVATION)
        else:
            blockers.append(MaintenanceBlockerV1.REVIEW_FINDINGS)
            actions.append(MaintenanceNextActionV1.ADDRESS_REVIEW)
    if observation.review.required_actions or observation.review.missing_slots:
        blockers.append(MaintenanceBlockerV1.REVIEW_FINDINGS)
        actions.append(MaintenanceNextActionV1.ADDRESS_REVIEW)
    return _unique(blockers), _unique(actions)


def _unique(values):
    return list(dict.fromkeys(values))


def _required_ci_state(observation: FinalizationObservation) -> RequiredCIStateV1:
    if "ci_pending" in observation.blockers:
        return RequiredCIStateV1.PENDING
    if "ci_not_successful" in observation.blockers:
        return RequiredCIStateV1.FAILING
    if "ci_unavailable" in observation.blockers:
        return RequiredCIStateV1.UNAVAILABLE
    return RequiredCIStateV1.PASSING


def _policy_unsettled_count(observation: FinalizationObservation) -> int:
    return sum(
        state is FindingSettlementState.UNRESOLVED
        for state in observation.review.finding_states.values()
    )


def _mergeability(
    observation: FinalizationObservation, pr: PRData
) -> MergeabilityStateV1:
    if _merge_conflict_is_known(pr):
        return MergeabilityStateV1.CONFLICTING
    if pr.mergeable.strip().upper() == "MERGEABLE":
        return MergeabilityStateV1.MERGEABLE
    return MergeabilityStateV1.UNKNOWN


def _merge_conflict_is_known(pr: PRData) -> bool:
    return pr.merge_state.strip().upper() == "DIRTY" or (
        pr.mergeable.strip().upper() == "CONFLICTING"
    )


def _review_state(observation: FinalizationObservation) -> ReviewStateV1:
    if observation.review.required_actions:
        return ReviewStateV1.CHANGES_REQUESTED
    if observation.review.missing_slots:
        return ReviewStateV1.PENDING
    return ReviewStateV1.CLEAN


def _actionable(snapshot: MaintenanceSnapshotV1) -> bool:
    return any(
        blocker
        in {
            MaintenanceBlockerV1.REQUIRED_CI_FAILED,
            MaintenanceBlockerV1.MERGE_CONFLICT,
            MaintenanceBlockerV1.REVIEW_FINDINGS,
        }
        for blocker in snapshot.blockers
    )


def _dispatch_allowed(pr: PRData, intent: object | None = None) -> bool:
    session_id = str(getattr(intent, "session_id", "") or "")
    worktree_path = str(getattr(intent, "worktree_path", "") or "")
    if session_id and session_id != "unattributed" and worktree_path:
        try:
            from . import session_registry

            active_sessions = session_registry.active_sessions_for_worktree(
                worktree_path, require_feature_pipeline=False
            )
        except Exception:  # noqa: BLE001 - coordinator remains the fallback fence
            active_sessions = ()
        if any(state.session_id == session_id for state in active_sessions):
            return False
    try:
        return coordinator.dispatch_decision_for_pr(pr).should_dispatch
    except Exception:  # noqa: BLE001 - dispatch remains best effort
        return False


def _head_drift_snapshot(
    key: MaintenanceKeyV1, observed_at: datetime
) -> MaintenanceSnapshotV1:
    return MaintenanceSnapshotV1(
        key=key,
        observed_at=_utc(observed_at),
        observation_health=ObservationHealthV1.UNAVAILABLE,
        blockers=(MaintenanceBlockerV1.OBSERVATION_UNAVAILABLE,),
        next_actions=(MaintenanceNextActionV1.RETRY_OBSERVATION,),
        required_ci_state=RequiredCIStateV1.UNAVAILABLE,
        mergeability=MergeabilityStateV1.UNAVAILABLE,
        review_state=ReviewStateV1.UNAVAILABLE,
        policy_unsettled_finding_count=0,
        raw_unresolved_thread_count=0,
        unaddressed_thread_count=0,
        stable_observation_count=0,
        stable_observation_first_at=None,
        stable_observation_last_at=None,
        settled=False,
    )
