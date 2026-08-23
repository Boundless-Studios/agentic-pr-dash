"""Orchestrator — PR state machine + queue-only dispatch.

Polls GitHub every 60s, updates PR state, and queues maintenance work for
CI failures, merge conflicts, and unaddressed review comments (bot + human).
Work is delegated to the local worktree agent; the dashboard never spawns
a standalone background subprocess.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from . import agents, coordinator, github_api, maintenance, session_registry
from .config import load as load_config
from .maintenance_check import _resolve_maintenance_roots
from .models import (
    CICheck,
    EventEntry,
    MaintenanceActor,
    MaintenanceStatus,
    PRData,
    PRStatus,
    RunnerExecutionSummary,
)
from .observability import ObservabilityEvent, get_event_store
from .observation import (
    ObservationController,
    ObservationKey,
    ObservationPlan,
    ObservationReason,
    ObservationSlice,
)
from .quota import (
    QuotaCaller,
    QuotaContext,
    QuotaLedger,
    QuotaTelemetry,
    QuotaWorkClass,
    ledger_from_environment,
)
from .worktrees import discover_worktrees, find_worktree_for_branch

# Composite key for the tracked-PR map. The dashboard aggregates PRs across any
# number of repos (anchor + ``maintenance_repo_roots``), so the PR number alone
# is not unique — same-number PRs in two repos must be distinct entries.
PRKey = tuple[str, int]


class _EventRepoResolution(Enum):
    UNKNOWN = "unknown"


def _repo_from_url(url: str) -> str:
    """``owner/name`` parsed from a GitHub PR url, or ``""`` if unparseable.

    ``https://github.com/Boundless-Studios/gaia-free/pull/12`` -> ``Boundless-Studios/gaia-free``.
    Used to tag each PR with its repo so the multi-repo dashboard never collides
    same-number PRs across repos (BOU-1598). Falls back to an empty tag (legacy
    single-repo behavior) when the url has no recognisable ``/pull/`` segment.
    """
    if not url:
        return ""
    marker = "/pull/"
    idx = url.find(marker)
    if idx == -1:
        return ""
    tail = url[:idx]
    parts = [p for p in tail.split("/") if p]
    if len(parts) < 2:
        return ""
    return f"{parts[-2]}/{parts[-1]}"


def _normalize_repo(repo: str | None) -> str:
    """Return the case-insensitive GitHub ``owner/name`` identity."""

    return (repo or "").strip().casefold()


def _observation_head_matches(plan_head: str, observed_head: str) -> bool:
    """Match a read to its immutable plan, with one legacy adapter exception."""

    return bool(
        observed_head
        and (
            plan_head.startswith("legacy-head-")
            or observed_head == plan_head
        )
    )

# Rolling "past 7 days" runner usage. The full recompute is heavy (~hundreds of
# REST calls, several minutes), so it can't run every poll, but 24h left the
# figure visibly stale all day. 6h keeps it fresh enough to be useful while the
# resilient recompute (see github_api.get_weekly_runner_execution_summary)
# tolerates the transient fetch failures that more frequent runs invite.
RUNNER_SUMMARY_REFRESH_SECONDS = 6 * 60 * 60

# Statuses that mean a dispatch is already in flight — don't re-queue for the
# same blocker set every poll cycle.
ACTIVE_QUEUED_STATES = frozenset({MaintenanceStatus.QUEUED, MaintenanceStatus.SIGNALED})

# Stable owner id for the agent-coordinator claim the dashboard takes when it
# hands maintenance off to the local agent. The claim suppresses the dashboard
# from re-queuing the same maintenance every poll cycle (the old queued-state
# guard was removed; suppression now rides entirely on coordinator claims).
# Derived from the actor vocabulary (BOU-2490) rather than spelled independently,
# so "who is the dashboard" has exactly one definition. The literal value is
# unchanged ("agentic-pr-dash-dashboard"), which matters: it is persisted in live
# coordinator claims and matched by existing tests and log-scraping.
DASHBOARD_OWNER_SESSION_ID = f"agentic-pr-dash-{MaintenanceActor.DASHBOARD_QUEUE.value.removesuffix('-queue')}"
# The refresh primes all immutable-head PR state from one batched GraphQL
# observation per repository. A 15-second cadence therefore stays responsive
# without returning to the old per-PR request fan-out that exhausted GitHub's
# API budget.
POLL_INTERVAL_SECONDS = 15

# Per-PR enrichment (mergeability / latest commit / CI checks / queue health /
# unaddressed comments) is independent across PRs, so each poll enriches them
# concurrently (BOU-1637 #4) instead of awaiting one PR's gh calls before the
# next. The semaphore bounds in-flight enrichments so a large PR pool doesn't
# burst the gh API into a rate-limit wall.
ENRICHMENT_CONCURRENCY = 6

# Metadata is the expensive repository-wide observation.  The controller owns
# the same interval for its per-head reconciliation plan; the orchestrator
# uses it to decide when the cached raw PR list must be replaced.
METADATA_RECONCILIATION_INTERVAL = timedelta(minutes=15)
# A conditional REST 304 validates the open-PR list, but not GraphQL-only
# fields such as reviewDecision or freshly recomputed mergeability. Keep the
# cheap 15-minute probe while forcing one rich snapshot per hour so those
# fields cannot remain stale forever behind an unchanged REST validator.
RICH_METADATA_RECONCILIATION_INTERVAL = timedelta(hours=1)
# The conditional REST validator is cheap in a way the rich relist is not: it
# spends REST budget rather than GraphQL points, and a 304 costs no rate limit
# at all. Gating it behind the rich relist's 15-minute clock (BOU-3095) meant a
# merged PR kept rendering as open for the whole interval — and far longer in
# practice, because a quota-denied or failed rich relist stamped that same clock
# and bought another full interval without pruning anything. Validate the
# open-PR list on its own fast cadence; keep the slow clocks for the slow read.
LIST_VALIDATION_INTERVAL = timedelta(
    seconds=float(os.environ.get("APD_LIST_VALIDATION_INTERVAL_S", "60"))
)


@dataclass(frozen=True, slots=True)
class _MetadataRead:
    """One metadata read's result.

    ``open_numbers`` is the authoritative set of open PR numbers this read
    proved, or ``None`` when it proved nothing. A stale projection is still
    worth rendering, but only a positive observation may remove a PR.

    Only a successful rich relist populates it. The cheap REST probe is a change
    detector: it decides *when* to spend that relist, not *what* is open. Three
    review rounds on BOU-3095 PR #169 found three separate ways for a
    page-limited, author-filtered REST body to be wrong about the open set, each
    of which pruned real PRs off the board, so the capability was removed rather
    than guarded case by case. The latency win survives — the probe still gets a
    merged PR reconciled in about a minute instead of fifteen — but under GraphQL
    exhaustion the board now holds stale entries rather than risking wrong ones.
    """

    raw_prs: list[dict] | None
    rich_read: bool
    open_numbers: set[int] | None
    #: Whether GitHub positively confirmed this root's open set — which a
    #: conditional 304 does (it proves the list is unchanged) even though it
    #: supplies no set to prune against. Kept separate from ``open_numbers`` so
    #: withdrawing the probe's authority over *removal* did not also make the
    #: freshness indicator claim staleness that isn't there.
    observed: bool = False


@dataclass(frozen=True, slots=True)
class ObservationFreshness:
    """How current the board's open-PR set is, and why when it is not.

    ``complete`` is False while some watched repo root has never been observed,
    so the UI can say "partial" instead of quoting an age that only covers the
    roots that happen to have answered.
    """

    observed_at: datetime | None
    degraded_reason: str | None
    complete: bool

    def age_seconds(self, now: datetime) -> float | None:
        if self.observed_at is None:
            return None
        return max(0.0, (now - self.observed_at).total_seconds())


def _open_numbers(raw_prs: list[dict]) -> set[int]:
    return {
        raw["number"] for raw in raw_prs if isinstance(raw.get("number"), int)
    }
# Cold start has no cached projection to fall back on, so the board is simply
# empty until the first list succeeds. One transient at daemon startup must not
# hold it empty for a whole reconciliation interval; retry on the next poll tick
# instead. Once a usable cache exists, the long backoff above applies again.
METADATA_COLD_START_RETRY_INTERVAL = timedelta(seconds=POLL_INTERVAL_SECONDS)
OPENED_EVENT_DISCOVERY_WINDOW = timedelta(seconds=45)
EVENT_DEBOUNCE_WINDOW = timedelta(seconds=2)
# Blocker statuses that are only as current as the last successful review+CI
# observation of the PR's immutable head. See ``_enrich_pr``'s status
# computation: when that head has never been observed these describe a previous
# head and must not be reported (or auto-dispatched) as if they were current.
_OBSERVATION_DERIVED_STATUSES = frozenset(
    {
        PRStatus.CLEAN,
        PRStatus.CI_PENDING,
        PRStatus.CI_FAILING,
        PRStatus.HAS_COMMENTS,
        PRStatus.CI_AND_COMMENTS,
    }
)
# A positively observed head mismatch also invalidates mergeability metadata:
# the relist still describes the superseded plan head. Keep this separate from
# ``_OBSERVATION_DERIVED_STATUSES`` so an unavailable review/CI read does not
# hide an authoritative current-head merge conflict.
_STALE_HEAD_DERIVED_STATUSES = _OBSERVATION_DERIVED_STATUSES | {PRStatus.MERGE_CONFLICT}
_REVIEW_INVALIDATION_EVENTS = frozenset(
    {
        "pull_request_review",
        "pull_request_review_comment",
        "pull_request_review_thread",
    }
)
_CI_INVALIDATION_EVENTS = frozenset({"check_run", "check_suite", "status"})
_HEAD_INVALIDATION_ACTIONS = frozenset({"opened", "reopened", "synchronize"})
# Admission estimates are intentionally conservative until the GraphQL batch
# returns a real ``rateLimit.cost`` sample. They are only used to decide if a
# background request may start; successful batch requests reconcile to the
# server-provided cost in github_api.
METADATA_GRAPHQL_ESTIMATED_COST = 50
REVIEW_GRAPHQL_ESTIMATED_COST = 25
MERGEABILITY_GRAPHQL_ESTIMATED_COST = 5
BATCH_GRAPHQL_ESTIMATED_COST = github_api.BATCH_GRAPHQL_ESTIMATED_COST
REVIEW_GRAPHQL_FAILURE_BACKOFF = timedelta(seconds=30)
METADATA_GRAPHQL_FAILURE_BACKOFF = timedelta(seconds=30)


def _parse_session_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _has_process_owner(pr: PRData) -> bool:
    if not pr.worktree_path:
        return False
    discovery_names = set(load_config(pr.worktree_path).discovery_names)
    by_path = agents.discover_primary_feature_pipeline_agents(
        [pr.worktree_path],
        min_cpu=0.0,
        discovery_names=discovery_names,
    )
    return any(agent.cli_name in discovery_names for agent in by_path.get(pr.worktree_path, []))


def _has_matching_session_owner(pr: PRData, queued_at: datetime | None = None) -> bool | None:
    """Return live owner state for this PR when the session registry knows it."""
    if not pr.worktree_path:
        return None

    summary = session_registry.summarize_sessions(
        path=session_registry.registry_path(pr.worktree_path)
    )
    matched = False
    latest_matched_at: datetime | None = None
    dead_nonterminal_owner = False
    for state in summary.sessions.values():
        if state.worktree_path != pr.worktree_path:
            continue
        if state.pr_number not in (None, pr.number):
            continue
        if state.pr_number is None and state.branch not in (None, "", pr.branch):
            continue
        if not state.is_feature_pipeline:
            continue

        matched = True
        timestamp = _parse_session_timestamp(state.timestamp)
        if timestamp and (latest_matched_at is None or timestamp > latest_matched_at):
            latest_matched_at = timestamp
        if not state.is_terminal:
            if session_registry.pid_is_live(state.pid):
                return True
            dead_nonterminal_owner = True

    if matched:
        if _has_process_owner(pr):
            return True
        if dead_nonterminal_owner:
            if queued_at is None or latest_matched_at is None or queued_at <= latest_matched_at:
                return False
            return None
        if queued_at is None or latest_matched_at is None or queued_at <= latest_matched_at:
            return False
    return None


class Orchestrator:
    def __init__(
        self,
        repo_cwd: str | None = None,
        *,
        observation_controller: ObservationController | None = None,
        quota_ledger: QuotaLedger | None = None,
    ):
        self.repo_cwd = repo_cwd
        self.observation_controller = (
            observation_controller or ObservationController()
        )
        # One process-local ledger owns all dashboard observations.  It is
        # deliberately shared by every repo root in this orchestrator so a
        # multi-repo board cannot spend the background allowance once per root.
        self.quota_ledger = quota_ledger or ledger_from_environment()
        # Keyed by ``(repo, number)`` so PRs aggregated from multiple repos
        # (anchor + ``maintenance_repo_roots``) never collide on PR number.
        self.prs: dict[PRKey, PRData] = {}
        # Which repo-root discovered each tracked PR, so a per-root poll prunes
        # only its OWN merged/closed PRs (even when that root returns zero PRs)
        # and never drops another repo's PRs on a transient failure.
        self._pr_root: dict[PRKey, str | None] = {}
        self._observation_keys: dict[PRKey, ObservationKey] = {}
        # A synchronize event can advance the immutable head before the
        # debounced repository listing reflects it.  Retain that event-owned
        # identity until a successful rich metadata read supersedes it so a
        # failed relist cannot fall back to actionable state from the old head.
        self._event_head_overrides: dict[PRKey, str] = {}
        self._event_head_override_observed_at: dict[PRKey, datetime] = {}
        self._observed_blocker_keys: set[ObservationKey] = set()
        self._observed_blocker_slices: dict[
            ObservationKey, set[ObservationSlice]
        ] = {}
        self._mergeability_pending_keys: set[ObservationKey] = set()
        self._mergeability_retry_after: dict[ObservationKey, datetime] = {}
        self._metadata_unconfirmed_keys: set[ObservationKey] = set()
        # The poll loop and manual refresh endpoint share one mutable PR/cache
        # projection. Serialize complete refresh transactions so an older
        # GitHub response can never land after a newer one.
        self._refresh_lock = asyncio.Lock()
        self.observed_roots: set[str | None] = set()
        # The raw ``gh pr list`` payload is a repository-wide observation.  A
        # 15-second dashboard tick reuses it and only replaces it after a
        # successful metadata reconciliation.  Keep attempt timestamps
        # separate from successful timestamps so a failed relist neither
        # prunes the old projection nor hammers GitHub on every tick.
        self._metadata_cache: dict[str | None, list[dict]] = {}
        self._metadata_last_success: dict[str | None, datetime] = {}
        self._metadata_last_attempt: dict[str | None, datetime] = {}
        self._metadata_last_rich_success: dict[str | None, datetime] = {}
        self._metadata_validators: dict[
            str | None, tuple[str | None, str | None]
        ] = {}
        # The cheap open-PR validator runs on ``LIST_VALIDATION_INTERVAL``,
        # independent of the rich relist's clocks above (BOU-3095).
        self._list_validation_last_attempt: dict[str | None, datetime] = {}
        # When each root's open-PR set was last positively observed, and why the
        # last attempt failed when it was not. This is what the board reports as
        # its observation age — the PR's own ``updatedAt`` cannot serve, because
        # a stale payload carries a stale timestamp and reads as plausibly
        # recent (BOU-3095).
        self._open_set_last_observed: dict[str | None, datetime] = {}
        self._open_set_degraded_reason: dict[str | None, str] = {}
        # Roots the dashboard considers itself responsible for, refreshed once
        # per poll tick. Rendering reads THIS and never resolves, because
        # ``_resolve_maintenance_roots`` shells out ``git worktree list`` with a
        # 10-second timeout per root — doing that on the request path put it on
        # the event loop on every five-second poll, from three tabs (BOU-3095
        # PR #169 review round 4).
        self._watched_roots: set[str | None] = set()
        self._resolved_roots: set[str | None] = set()
        # Truncation state of each root's stored REST validator. A 304 against a
        # validator that was established from a full page only revalidates that
        # page, so it cannot count as observing the whole open set.
        self._metadata_validator_truncated: dict[str | None, bool] = {}
        # Last ``updatedAt`` observed per PR key. GitHub advances it when a
        # comment is posted or resolved without a push, which is the only cheap
        # signal that a review re-scan is worth spending (BOU-3095).
        self._pr_last_updated_at: dict[PRKey, str] = {}
        self._root_repo_identity: dict[str | None, str] = {}
        self._root_repo_slugs: dict[str | None, set[str]] = {}
        self._repo_roots_by_slug: dict[str, str | None] = {}
        # Pull-request events that affect repository metadata are debounced by
        # the same ManualClock-backed window as ObservationController.  The
        # next refresh after the deadline relists that root; an event for an
        # unknown repo never gets to mutate another root's controller state.
        self._metadata_event_due: dict[str | None, datetime] = {}
        self._opened_event_prs: dict[str | None, set[int]] = {}
        self._opened_event_expirations: dict[
            tuple[str | None, int], datetime
        ] = {}
        self._opened_event_retry_due: dict[str | None, datetime] = {}
        self._inflight_prs: set[int] = set()
        self.events: list[EventEntry] = []
        cached_runner_summary = github_api.load_runner_execution_summary_cache()
        self.weekly_runner_execution_summary = cached_runner_summary or RunnerExecutionSummary()
        self._weekly_runner_summary_polled_at: datetime | None = (
            github_api.load_runner_execution_summary_cache_generated_at()
            if cached_runner_summary is not None
            else None
        )
        self._poll_task: asyncio.Task | None = None
        self._max_events = 200

    def _emit(
        self,
        kind: str,
        *,
        pr_number: int | None = None,
        repo: str | None = None,
        details: dict | None = None,
        actor: MaintenanceActor = MaintenanceActor.DASHBOARD_QUEUE,
    ) -> None:
        """Best-effort observability event emission. Never raises.

        ``actor`` defaults to :attr:`MaintenanceActor.DASHBOARD_QUEUE` because every
        emit site in this module *is* the dashboard — it polls, enriches and queues,
        and never runs an executor. Keeping the default here (rather than forcing
        each call to repeat it) means a new dashboard emit site is correctly
        attributed by construction; the loop has its own emitter with its own actor.
        """
        try:
            cwd = repo if repo is not None else self.repo_cwd
            event = ObservabilityEvent(
                ts=datetime.now(timezone.utc),
                repo=cwd,
                pr_number=pr_number,
                kind=kind,
                actor=actor.value,
                session_id=DASHBOARD_OWNER_SESSION_ID,
                details=details or {},
            )
            get_event_store(cwd).append(event)
        except Exception:  # noqa: BLE001
            pass

    def log(self, message: str, pr_number: int | None = None, level: str = "info") -> None:
        entry = EventEntry(
            timestamp=datetime.now(timezone.utc),
            pr_number=pr_number,
            message=message,
            level=level,
        )
        self.events.insert(0, entry)
        if len(self.events) > self._max_events:
            self.events = self.events[:self._max_events]
        self._emit(
            "state_transition",
            pr_number=pr_number,
            details={"message": message, "level": level},
        )

    def get_pr(self, pr_number: int, repo: str | None = None) -> PRData | None:
        """Look up a tracked PR by number (and optionally repo).

        The internal map is keyed by ``(repo, number)`` for multi-repo
        aggregation, but the dashboard's human-dispatch routes only carry a PR
        number. When ``repo`` is omitted, return the first tracked PR with that
        number (single-repo and the overwhelmingly common case); a caller that
        needs disambiguation across repos passes ``repo`` explicitly.
        """
        if repo is not None:
            return self.prs.get((repo, pr_number))
        for key, pr in self.prs.items():
            # Composite ``(repo, number)`` key; tolerate a bare-int key too so
            # fixtures/legacy callers that seed ``prs`` directly still resolve.
            num = key[1] if isinstance(key, tuple) else key
            if num == pr_number:
                return pr
        return None

    def _repo_roots(self) -> list[str]:
        """Repo roots the dashboard polls = ``[anchor] + maintenance_repo_roots``.

        Reuses ``_resolve_maintenance_roots`` (the same expansion the maintenance
        loop honors) so the dashboard covers exactly the repos the loop does.
        When no extra roots are configured this resolves to just the anchor (or
        ``[None]`` when the orchestrator has no repo_cwd), preserving today's
        single-repo behavior.
        """
        if not self.repo_cwd:
            return [None]
        try:
            roots = _resolve_maintenance_roots(self.repo_cwd)
        except Exception as exc:
            self.log(f"Could not resolve maintenance roots: {exc}", level="warn")
            return [self.repo_cwd]
        if self.repo_cwd not in roots:
            roots = [self.repo_cwd, *roots]
        return roots

    def _metadata_interval(self) -> timedelta:
        """Read the controller's configured metadata reconciliation interval."""

        # ObservationController intentionally exposes ``now`` as its public
        # clock boundary.  The interval is immutable configuration, but keep a
        # defensive fallback for lightweight controller doubles used by callers.
        return getattr(
            self.observation_controller,
            "metadata_reconciliation_interval",
            getattr(
                self.observation_controller,
                "_metadata_reconciliation_interval",
                METADATA_RECONCILIATION_INTERVAL,
            ),
        )

    @property
    def quota_telemetry(self) -> QuotaTelemetry:
        """Current dashboard observation quota state for API/UI consumers."""

        return self.quota_ledger.telemetry()

    @property
    def quota(self) -> QuotaLedger:
        """Compatibility alias for integrations that expose the ledger itself."""

        return self.quota_ledger

    def _quota_context(self, *, force: bool, estimated_cost: int) -> QuotaContext:
        if force:
            return QuotaContext(
                ledger=self.quota_ledger,
                caller=QuotaCaller.OPERATOR,
                work_class=QuotaWorkClass.EXPLICIT_OPERATOR,
                estimated_cost=estimated_cost,
            )
        return QuotaContext(
            ledger=self.quota_ledger,
            caller=QuotaCaller.DASHBOARD,
            work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
            estimated_cost=estimated_cost,
        )

    def _quota_allows(self, *, force: bool, estimated_cost: int):
        context = self._quota_context(force=force, estimated_cost=estimated_cost)
        return context, self.quota_ledger.allow(
            context.caller,
            context.work_class,
            estimated_cost=estimated_cost,
        )

    def _record_estimated_success(
        self,
        context: QuotaContext,
        reservation,
    ) -> None:
        self.quota_ledger.record_estimated(
            context.caller,
            context.work_class,
            context.estimated_cost,
            reservation=reservation,
        )

    def _record_estimated_failure(self, reservation) -> None:
        if reservation is not None:
            self.quota_ledger.release(reservation)

    async def _review_fallback_observation(
        self,
        pr_number: int,
        latest_commit_date: str,
        root: str | None,
        *,
        force: bool,
    ) -> github_api.ObservationReadResult:
        """Run an explicitly admitted per-PR review fallback.

        A batch omission is not permission to spend an untracked GraphQL read.
        Reserve its conservative cost first, account the successful read when
        the legacy scanner does not expose a ``rateLimit`` sample, and preserve
        an unavailable result when admission or transport fails.
        """

        context, decision = self._quota_allows(
            force=force, estimated_cost=REVIEW_GRAPHQL_ESTIMATED_COST
        )
        if not decision.allowed:
            return github_api.ObservationReadResult.unavailable(
                f"review fallback deferred: quota {decision.reason.value}"
            )
        reservation = self.quota_ledger.reserve(
            context.caller,
            context.work_class,
            estimated_cost=context.estimated_cost,
        )
        if reservation is None:
            return github_api.ObservationReadResult.unavailable(
                "review fallback deferred: quota admission changed"
            )
        try:
            result = await asyncio.to_thread(
                github_api.scan_review_threads_observation,
                pr_number,
                latest_commit_date,
                root,
            )
        except Exception as exc:  # noqa: BLE001 - keep the PR fail-closed
            self._record_estimated_failure(reservation)
            self.quota_ledger.record_failure(
                reason="review_fallback_failed"
            )
            self.quota_ledger.record_backoff(
                REVIEW_GRAPHQL_FAILURE_BACKOFF,
                reason="review_fallback_failed",
            )
            return github_api.ObservationReadResult.unavailable(
                f"review fallback raised: {exc}"
            )
        if result.observable:
            self._record_estimated_success(context, reservation)
            return result
        if result.graphql_observed:
            # The composite read failed only after its GraphQL thread query
            # completed. Reconcile that consumed work, then record the REST
            # failure without double-counting the request.
            self._record_estimated_success(context, reservation)
            failure_counts_request = False
        else:
            self._record_estimated_failure(reservation)
            failure_counts_request = True
        self.quota_ledger.record_failure(
            reason="review_fallback_unavailable",
            count_request=failure_counts_request,
        )
        self.quota_ledger.record_backoff(
            REVIEW_GRAPHQL_FAILURE_BACKOFF,
            reason="review_fallback_unavailable",
        )
        return result

    async def _mergeability_fallback(
        self,
        pr_number: int,
        root: str | None,
        *,
        force: bool,
    ) -> tuple[str, str]:
        """Run a quota-admitted per-PR mergeability recomputation."""

        context, decision = self._quota_allows(
            force=force,
            estimated_cost=MERGEABILITY_GRAPHQL_ESTIMATED_COST,
        )
        if not decision.allowed:
            return "", ""
        reservation = self.quota_ledger.reserve(
            context.caller,
            context.work_class,
            estimated_cost=context.estimated_cost,
        )
        if reservation is None:
            return "", ""
        try:
            result = await asyncio.to_thread(
                github_api.get_mergeability,
                pr_number,
                root,
            )
        except Exception:  # noqa: BLE001 - preserve last-known mergeability
            result = ("", "")
        if any(result):
            self._record_estimated_success(context, reservation)
            return result
        self._record_estimated_failure(reservation)
        self.quota_ledger.record_failure(
            reason="mergeability_fallback_unavailable"
        )
        return "", ""

    def _configured_roots(self) -> set[str]:
        """Watched roots taken from configuration, with no subprocess at all.

        ``_resolve_maintenance_roots`` proves each root is a git repo by running
        ``git worktree list``, and drops any root whose call fails. That makes it
        useless for answering "what are we responsible for?": a sibling whose
        very first resolution fails has never been observed, degraded, or
        assigned a PR, so retention logic keyed on past success cannot see it
        either, and the board reports complete while that repo is absent
        (BOU-3095 PR #169 review round 4).

        Existence is checked with a stat rather than a git call: it keeps an
        uncloned/removed root from being reported as permanently missing, which
        would be a standing false alarm, while costing nothing on the hot path.
        """

        if not self.repo_cwd:
            return set()
        try:
            cfg = load_config(self.repo_cwd)
        except Exception:  # noqa: BLE001 — freshness must never raise
            return {os.path.abspath(os.path.expanduser(self.repo_cwd))}
        roots: set[str] = set()
        for candidate in (
            self.repo_cwd,
            *(getattr(cfg, "maintenance_repo_roots", ()) or ()),
        ):
            path = os.path.abspath(os.path.expanduser(str(candidate)))
            if os.path.isdir(path):
                roots.add(path)
        return roots

    def _refresh_watched_roots(self, resolved: Iterable[str | None]) -> None:
        """Recompute the watched set from one poll tick's resolution."""

        resolved_set = set(resolved)
        watched: set[str | None] = set(self._configured_roots())
        watched |= resolved_set
        watched |= set(self._open_set_last_observed)
        watched |= set(self._open_set_degraded_reason)
        watched |= set(self._pr_root.values())
        self._watched_roots = watched
        # Kept separately so freshness can tell "watched but not resolved this
        # tick" from "watched and resolving fine". A root that resolved before
        # and has since vanished keeps a stale observation timestamp, so
        # observation alone cannot detect it (round-3 review).
        self._resolved_roots = resolved_set

    def open_set_freshness(self) -> ObservationFreshness:
        """How current the board's open-PR set actually is.

        Reports the OLDEST root's observation, because the board mixes repos and
        the age shown has to be true for everything on it. ``observed_at`` is
        ``None`` until some root has been observed at least once.

        This is deliberately distinct from the header's "Live" chip, which
        tracks whether the browser's poll of *localhost* is succeeding
        (BOU-2193). A board can poll perfectly while showing hours-old GitHub
        state — that is the failure BOU-3095 reported.
        """

        # Read the cached watched set — never resolve here. This runs on the
        # request path for every partial poll (BOU-3095 PR #169 review round 4).
        known = set(self._watched_roots)
        if not known:
            # Before the first poll tick has populated the cache, fall back to
            # configuration, which is a stat rather than a git call.
            known = set(self._configured_roots()) or {self.repo_cwd}
        observed = [
            self._open_set_last_observed[root]
            for root in known
            if root in self._open_set_last_observed
        ]
        # "Watched but never observed" is the single completeness test, and it
        # covers both shapes the reviews found: a root that resolved before and
        # has since disappeared, and a configured root whose very first
        # resolution failed so it was never seen at all.
        unobserved = [root for root in known if root not in self._open_set_last_observed]
        # A root that resolved before and has since disappeared keeps a stale
        # observation timestamp, so "never observed" cannot catch it — compare
        # against what actually resolved on the last tick.
        dropped = (
            [root for root in known if root not in self._resolved_roots]
            if self._resolved_roots
            else []
        )
        reasons = [
            self._open_set_degraded_reason[root]
            for root in known
            if root in self._open_set_degraded_reason
        ]
        if not reasons and (unobserved or dropped):
            missing = len({*unobserved, *dropped})
            reasons.append(
                f"{missing} watched repo root(s) are not being refreshed and "
                "may be absent from the board"
            )
        return ObservationFreshness(
            # The oldest root bounds the whole board: the age shown has to be
            # true for everything on it.
            observed_at=min(observed) if observed else None,
            degraded_reason=reasons[0] if reasons else None,
            complete=not unobserved and not dropped,
        )

    def _note_open_set_unobserved(self, root: str | None, reason: str) -> None:
        """Record why this root's open set could not be observed.

        A successful observation clears it (see :meth:`_refresh_repo`). Without
        this, a daemon that starts while discovery is quota-denied never records
        a reason and the header renders a calm "PR data loading" forever —
        exactly the outage the indicator exists to expose.
        """

        self._open_set_degraded_reason[root] = reason

    def _merge_probe_activity(
        self, root: str | None, probe_prs: list[dict]
    ) -> None:
        """Carry the probe's ``updatedAt`` into the cached rich list.

        A cheap validation pass deliberately does not replace the cached list —
        the REST body has no ``reviewDecision`` and would erase GraphQL-only
        fields. But ``updatedAt`` is the change signal
        :meth:`_note_pr_activity` reads, and it is only meaningful if it is the
        current one, so merge exactly that field and nothing else.
        """

        cached = self._metadata_cache.get(root)
        if not cached:
            return
        fresh = {
            raw["number"]: raw["updatedAt"]
            for raw in probe_prs
            if isinstance(raw.get("number"), int)
            and isinstance(raw.get("updatedAt"), str)
            and raw["updatedAt"]
        }
        if not fresh:
            return
        for raw in cached:
            updated_at = fresh.get(raw.get("number"))
            if updated_at:
                raw["updatedAt"] = updated_at

    def _note_pr_activity(
        self,
        key: PRKey,
        observation_key: ObservationKey,
        raw: dict,
        now: datetime,
        *,
        newly_discovered: bool,
    ) -> None:
        """Re-plan the review slice when GitHub says the PR changed.

        ``review_reconciliation_interval`` is one hour per immutable head, so a
        comment posted or resolved without a push could not move the card's
        comment count for up to an hour (BOU-3095). ``updatedAt`` is the cheap
        change signal the open-PR list already carries: when it advances on an
        unchanged head, something happened that a review scan should see.

        This routes through the same ``handle_event`` path a review webhook
        would use, so the slice mapping and the debounce window stay in one
        place — the re-scan therefore lands on the next poll tick rather than
        this one. A PR whose ``updatedAt`` has not moved is left alone, so the
        hourly floor still bounds background spend on quiet PRs.

        Note where the value comes from: ``PR_SNAPSHOT_FIELDS`` does NOT request
        ``updatedAt``, so the rich ``gh pr list`` payload has none and
        :meth:`_cache_metadata` drops the merged value every time it replaces
        the cache. That is fine and deliberate — the REST probe is the source of
        this signal, and it supplies it exactly when it matters: the conditional
        response body contains every open PR's ``updated_at``, so a comment
        changes the body, changes the ETag, and the probe returns 200 rather
        than 304. ``_pr_last_updated_at`` survives the cache replacement, so the
        re-supplied value does not read as a spurious change. Do not "fix" the
        wipe by adding ``updatedAt`` to ``PR_SNAPSHOT_FIELDS`` without checking
        ``_SNAPSHOT_SERVABLE_FIELDS`` and the snapshot-shape tests.
        """

        updated_at = raw.get("updatedAt")
        if not isinstance(updated_at, str) or not updated_at:
            return
        previous = self._pr_last_updated_at.get(key)
        self._pr_last_updated_at[key] = updated_at
        if previous is not None and updated_at <= previous:
            return
        if previous is None and newly_discovered:
            # First sight of a brand-new PR: its INITIAL plan already requests
            # every slice, so there is nothing to invalidate. Just seed.
            return
        if previous is None:
            # A PR we already track, seeing its first timestamp. Because
            # PR_SNAPSHOT_FIELDS omits updatedAt, this is the normal state after
            # startup — and if the REST 200 that produced it was itself caused by
            # a comment, swallowing it loses the only signal that PR changed
            # (BOU-3095 PR #169 review). Later probes would see the same value
            # and do nothing. Be conservative and treat it as a change.
            self.log(
                f"Seeding review baseline for PR #{key[1]} from its first REST "
                "timestamp; re-planning review to avoid missing a change that "
                "predates it",
                pr_number=key[1],
                level="info",
            )
        self.observation_controller.handle_event(
            "pull_request_review",
            observation_key.repo,
            observation_key.number,
            observation_key.head_sha,
            now=now,
        )
        # Match the webhook path. Invalidating the slice without revoking its
        # authority leaves the previous REVIEW observation reportable, so a
        # quota-deferred re-read can keep a resolved thread showing as
        # actionable, or leave a newly-commented PR CLEAN (PR #169 review).
        self._revoke_event_blocker_authority(
            observation_key, "pull_request_review", None
        )

    def _list_validation_due(self, root: str | None, now: datetime) -> bool:
        """Whether the cheap conditional open-PR validator should run.

        This clock is deliberately separate from the rich relist's (BOU-3095).
        It is stamped on every attempt, successful or not — but the interval is
        short, so a failed probe costs one validation window rather than a full
        15-minute reconciliation interval in which nothing can be pruned.

        A root with no cached metadata is not validated here: there is nothing
        to validate, and ``_metadata_refresh_due`` already owns the cold-start
        fast retry.
        """

        if root not in self._metadata_cache:
            return False
        last_attempt = self._list_validation_last_attempt.get(root)
        return last_attempt is None or now >= last_attempt + LIST_VALIDATION_INTERVAL

    def _metadata_refresh_due(self, root: str | None, now: datetime, *, force: bool) -> bool:
        """Whether ``root`` needs a real, successful-capable metadata read."""

        if force:
            return True

        if not self._has_pending_opened(root, now):
            self._opened_event_retry_due.pop(root, None)
        event_due = self._metadata_event_due.get(root)
        opened_retry_due = self._opened_event_retry_due.get(root)
        if any(
            due_at is not None and now >= due_at
            for due_at in (event_due, opened_retry_due)
        ):
            return True
        if event_due is not None or opened_retry_due is not None:
            return False

        interval = self._metadata_interval()
        last_attempt = self._metadata_last_attempt.get(root)
        if root not in self._metadata_cache:
            # Cold start: nothing has ever been loaded for this root, so a
            # failed first attempt leaves the board blank rather than merely
            # stale. Retry on the next poll tick instead of sitting out the
            # full reconciliation interval. ``min`` keeps a deliberately short
            # configured interval from being lengthened by this floor.
            if last_attempt is None:
                return True
            retry = min(interval, METADATA_COLD_START_RETRY_INTERVAL)
            return now >= last_attempt + retry

        last_success = self._metadata_last_success.get(root)
        references = [
            timestamp
            for timestamp in (last_success, last_attempt)
            if timestamp is not None
        ]
        reference = max(references) if references else None
        return reference is None or now >= reference + interval

    def _has_pending_opened(self, root: str | None, now: datetime) -> bool:
        """Whether an undiscovered opened PR is still in its bounded fast retry."""

        pending = self._opened_event_prs.get(root)
        if not pending:
            return False
        for number in tuple(pending):
            expires_at = self._opened_event_expirations.get((root, number))
            if expires_at is not None and now >= expires_at:
                pending.discard(number)
                self._opened_event_expirations.pop((root, number), None)
        if not pending:
            self._opened_event_prs.pop(root, None)
            return False
        return True

    async def _cache_metadata(
        self, root: str | None, raw_prs: list[dict], now: datetime
    ) -> None:
        """Replace one root's metadata cache and its normalized repo mapping."""

        old_slugs = self._root_repo_slugs.get(root, set())
        for slug in old_slugs:
            if self._repo_roots_by_slug.get(slug) == root:
                self._repo_roots_by_slug.pop(slug, None)

        cache = [dict(raw) for raw in raw_prs if isinstance(raw, dict)]
        self._metadata_cache[root] = cache
        self._metadata_last_success[root] = now
        self._metadata_last_attempt[root] = now
        self._metadata_last_rich_success[root] = now

        # A successful rich list is the authoritative repository/head
        # projection.  It supersedes any temporary head identity learned from
        # a webhook while the cached list was stale.
        for raw in cache:
            number = raw.get("number")
            if not isinstance(number, int):
                continue
            repo = _normalize_repo(_repo_from_url(str(raw.get("url", ""))))
            key = (repo, number)
            override = self._event_head_overrides.get(key)
            listed_head = str(raw.get("headRefOid") or "")
            if not override or not listed_head:
                continue
            if listed_head == override:
                self._event_head_overrides.pop(key, None)
                self._event_head_override_observed_at.pop(key, None)
                continue
            # A rich list may briefly lag a synchronize delivery, while a
            # missed later delivery can make the retained override itself
            # stale. Resolve that ambiguity with the exact REST PR head: keep
            # failing closed when the event SHA is still current, but let a
            # newer authoritative listed/current SHA supersede it.
            latest_sha, _latest_date = await asyncio.to_thread(
                github_api.get_latest_commit_uncached, number, root
            )
            override_observed_at = self._event_head_override_observed_at.get(key)
            override_matured = (
                override_observed_at is not None
                and now >= override_observed_at + self._metadata_interval()
            )
            if latest_sha and latest_sha == listed_head and override_matured:
                self._event_head_overrides.pop(key, None)
                self._event_head_override_observed_at.pop(key, None)

        slugs = {
            _normalize_repo(_repo_from_url(str(raw.get("url", ""))))
            for raw in cache
        }
        slugs.discard("")
        if root not in self._root_repo_identity:
            identity = next(iter(slugs), "")
            if not identity:
                owner, repo_name = await asyncio.to_thread(
                    github_api.get_repo_info, root
                )
                identity = _normalize_repo(
                    f"{owner}/{repo_name}" if owner and repo_name else ""
                )
            if identity:
                self._root_repo_identity[root] = identity
        if identity := self._root_repo_identity.get(root):
            slugs.add(identity)
        self._root_repo_slugs[root] = slugs
        for slug in slugs:
            self._repo_roots_by_slug[slug] = root

        event_due = self._metadata_event_due.get(root)
        if event_due is not None and now >= event_due:
            self._metadata_event_due.pop(root, None)
        discovered = {
            raw["number"]
            for raw in cache
            if isinstance(raw.get("number"), int)
        }
        pending_opened = self._opened_event_prs.get(root)
        if pending_opened is not None:
            pending_opened.difference_update(discovered)
            for number in discovered:
                self._opened_event_expirations.pop((root, number), None)
            if not pending_opened:
                self._opened_event_prs.pop(root, None)
                pending_opened = None
        if pending_opened and self._has_pending_opened(root, now):
            self._opened_event_retry_due[root] = (
                now + METADATA_COLD_START_RETRY_INTERVAL
            )
        else:
            self._opened_event_retry_due.pop(root, None)

    def _known_event_root(self, repo: str) -> str | None | _EventRepoResolution:
        """Resolve an event repo without conflating a mapped ``None`` root."""

        normalized = _normalize_repo(repo)
        if normalized in self._repo_roots_by_slug:
            return self._repo_roots_by_slug[normalized]

        # A fixture or legacy caller may seed ``prs`` directly, bypassing the
        # metadata cache.  Resolve only an exact repo match; never fall back to
        # PR number alone, which could invalidate a sibling repository.
        roots = {
            self._pr_root.get(key)
            for key, pr in self.prs.items()
            if _normalize_repo(pr.repo) == normalized
        }
        if len(roots) == 1:
            return next(iter(roots))
        return _EventRepoResolution.UNKNOWN

    def _event_observation_target(
        self, root: str | None, repo: str, number: int
    ) -> tuple[PRKey, ObservationKey] | None:
        normalized = _normalize_repo(repo)
        for key, observation_key in self._observation_keys.items():
            if key[1] != number or self._pr_root.get(key) != root:
                continue
            if _normalize_repo(key[0]) == normalized:
                return key, observation_key
        return None

    def _plan_for_available_slices(
        self,
        plan: ObservationPlan | None,
        *,
        metadata_read: bool,
    ) -> ObservationPlan | None:
        """Drop an unobservable metadata slice without acknowledging it."""

        if plan is None or metadata_read or ObservationSlice.METADATA not in plan.slices:
            return plan
        if (
            plan.reason is ObservationReason.METADATA_RECONCILIATION
            and plan.key in self._mergeability_pending_keys
        ):
            # Cached GraphQL metadata cannot prove repository-wide additions or
            # removals, but it can retry the quota-admitted per-PR mergeability
            # prerequisite that left this immutable key unacknowledged.
            return plan
        slices = frozenset(
            observation_slice
            for observation_slice in plan.slices
            if observation_slice is not ObservationSlice.METADATA
        )
        if not slices:
            return None
        generations = frozenset(
            (observation_slice, generation)
            for observation_slice, generation in plan.invalidation_generations
            if observation_slice in slices
        )
        return ObservationPlan(plan.key, slices, plan.reason, generations)

    async def handle_github_event(
        self,
        event_name: str,
        repo: str,
        number: int,
        head_sha: str,
        action: str | None = None,
    ) -> None:
        """Apply a GitHub event after any in-flight refresh transaction.

        The webhook route deliberately hands this method the normalized event
        fields rather than mutating dashboard state itself.  Waiting on the
        same lock as ``refresh_prs`` guarantees a blocked refresh cannot land
        after the event and erase its invalidation.
        """

        normalized_event = event_name.strip().casefold()
        normalized_action = action.strip().casefold() if action is not None else None
        async with self._refresh_lock:
            root = self._known_event_root(repo)
            if root is _EventRepoResolution.UNKNOWN:
                self.log(
                    f"Ignoring event for unknown GitHub repo {repo!r}",
                    pr_number=number,
                    level="warn",
                )
                return

            is_pull_request = normalized_event == "pull_request"
            is_metadata_change = is_pull_request

            observation_target = self._event_observation_target(root, repo, number)
            if observation_target is None and not is_pull_request:
                self.log(
                    f"Ignoring event for untracked PR #{number} in {repo}",
                    pr_number=number,
                    level="info",
                )
                return

            if is_metadata_change:
                due_at = self.observation_controller.now() + EVENT_DEBOUNCE_WINDOW
                self._metadata_event_due[root] = due_at

            if observation_target is None:
                # A known repo can receive ``opened`` for a PR absent from the
                # cache.  The debounced metadata relist above will discover it;
                # there is no immutable key to invalidate yet.
                if (
                    is_pull_request
                    and normalized_action in {"opened", "reopened"}
                ):
                    event_now = self.observation_controller.now()
                    self._opened_event_prs.setdefault(root, set()).add(number)
                    self._opened_event_expirations[(root, number)] = (
                        event_now + OPENED_EVENT_DISCOVERY_WINDOW
                    )
                    # A previous missed terminal event may have expired this
                    # PR's fast-discovery window and moved the root back to the
                    # normal reconciliation cadence. A fresh open/reopen event
                    # starts a new bounded window rather than inheriting that
                    # stale long deadline.
                    self._metadata_event_due[root] = (
                        event_now + EVENT_DEBOUNCE_WINDOW
                    )
                    self._opened_event_retry_due[root] = (
                        event_now + EVENT_DEBOUNCE_WINDOW
                    )
                elif is_pull_request and normalized_action == "closed":
                    pending_opened = self._opened_event_prs.get(root)
                    if pending_opened is not None:
                        pending_opened.discard(number)
                        self._opened_event_expirations.pop(
                            (root, number), None
                        )
                        if not pending_opened:
                            self._opened_event_prs.pop(root, None)
                            self._opened_event_retry_due.pop(root, None)
                return

            tracked_key, observation_key = observation_target
            if is_pull_request and (
                not head_sha or head_sha == observation_key.head_sha
            ):
                self._metadata_unconfirmed_keys.add(observation_key)
            if (
                is_pull_request
                and normalized_action in _HEAD_INVALIDATION_ACTIONS
                and head_sha
                and head_sha != observation_key.head_sha
            ):
                self._event_head_overrides[tracked_key] = head_sha
                self._event_head_override_observed_at[tracked_key] = (
                    self.observation_controller.now()
                )

            self.observation_controller.handle_event(
                normalized_event,
                observation_key.repo,
                observation_key.number,
                head_sha,
                action=normalized_action,
                now=self.observation_controller.now(),
            )
            if not head_sha or head_sha == observation_key.head_sha:
                self._revoke_event_blocker_authority(
                    observation_key,
                    normalized_event,
                    normalized_action,
                )

    def _revoke_event_blocker_authority(
        self,
        key: ObservationKey,
        event_name: str,
        action: str | None,
    ) -> None:
        """Make event-invalidated blocker slices non-authoritative until read."""

        invalidated: set[ObservationSlice] = set()
        if event_name in _REVIEW_INVALIDATION_EVENTS:
            invalidated.add(ObservationSlice.REVIEW)
        elif event_name in _CI_INVALIDATION_EVENTS:
            invalidated.add(ObservationSlice.CI)
        elif event_name == "pull_request" and action in _HEAD_INVALIDATION_ACTIONS:
            invalidated.update({ObservationSlice.REVIEW, ObservationSlice.CI})
        if not invalidated:
            return
        observed = self._observed_blocker_slices.get(key)
        if observed is not None:
            observed.difference_update(invalidated)
        self._observed_blocker_keys.discard(key)

    async def handle_github_check_event(
        self,
        event_name: str,
        repo: str,
        head_sha: str,
        action: str | None = None,
    ) -> None:
        """Invalidate CI by immutable head when GitHub omits PR associations."""

        normalized_repo = _normalize_repo(repo)
        normalized_event = event_name.strip().casefold()
        normalized_action = action.strip().casefold() if action is not None else None
        async with self._refresh_lock:
            root = self._known_event_root(repo)
            if root is _EventRepoResolution.UNKNOWN:
                self.log(
                    f"Ignoring check event for unknown GitHub repo {repo!r}",
                    level="warn",
                )
                return

            matched = False
            for key, observation_key in self._observation_keys.items():
                if self._pr_root.get(key) != root:
                    continue
                if _normalize_repo(key[0]) != normalized_repo:
                    continue
                if observation_key.head_sha != head_sha:
                    continue
                matched = True
                self.observation_controller.handle_event(
                    normalized_event,
                    observation_key.repo,
                    observation_key.number,
                    observation_key.head_sha,
                    action=normalized_action,
                    now=self.observation_controller.now(),
                )
                self._revoke_event_blocker_authority(
                    observation_key,
                    normalized_event,
                    normalized_action,
                )
            if not matched:
                self.log(
                    f"Ignoring check event for untracked head {head_sha[:12]} in {repo}",
                    level="info",
                )

    def start(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())
            self.log(
                f"Orchestrator started — polling every {POLL_INTERVAL_SECONDS}s"
            )

    async def _poll_loop(self) -> None:
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await self.refresh_prs()
            except Exception as exc:
                self.log(f"Poll error: {exc}", level="error")
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0, POLL_INTERVAL_SECONDS - elapsed))

    async def refresh_prs(self, force: bool = False) -> list[PRData]:
        """Refresh cached PR metadata and due review/CI observations.

        The dashboard covers ``[anchor] + maintenance_repo_roots`` (BOU-1598):
        the same root list the maintenance loop honors. Each root is polled with
        that root as the ``github_api.*`` cwd, sequentially, with per-repo error
        isolation — a single repo's poll failing (gh error or exception) must not
        drop another repo's PRs. By default, successful metadata is reused until
        its 15-minute reconciliation is due; ``force=True`` bypasses that cache
        and refreshes every observation slice immediately.
        """
        async with self._refresh_lock:
            return await self._refresh_prs_locked(force=force)

    async def _refresh_prs_locked(self, *, force: bool = False) -> list[PRData]:
        """Run one refresh transaction while :attr:`_refresh_lock` is held."""
        now = self.observation_controller.now()

        # Runner-execution summary is a fleet-wide stat — poll it once (anchor),
        # not per-repo.
        if (
            self._weekly_runner_summary_polled_at is None
            or (now - self._weekly_runner_summary_polled_at).total_seconds() >= RUNNER_SUMMARY_REFRESH_SECONDS
        ):
            runner_summary = await asyncio.to_thread(
                github_api.get_weekly_runner_execution_summary, self.repo_cwd
            )
            if runner_summary is not None:
                self.weekly_runner_execution_summary = runner_summary
                self._weekly_runner_summary_polled_at = now
                github_api.save_runner_execution_summary_cache(runner_summary, now.isoformat())

        resolved_roots = self._repo_roots()
        # Recompute what we consider ourselves responsible for once per tick, so
        # rendering can read it without resolving anything (round-4 review).
        self._refresh_watched_roots(resolved_roots)
        for root in resolved_roots:
            try:
                await self._refresh_repo(root, now, force=force)
            except Exception as exc:
                # Per-repo isolation: one bad repo must not sink the dashboard.
                self.log(f"Poll error for repo root {root}: {exc}", level="error")

        active_observation_keys = set(self._observation_keys.values())
        self.observation_controller.prune(active_observation_keys)
        self._observed_blocker_keys.intersection_update(active_observation_keys)
        self._observed_blocker_slices = {
            key: slices
            for key, slices in self._observed_blocker_slices.items()
            if key in active_observation_keys
        }
        self._mergeability_pending_keys.intersection_update(
            active_observation_keys
        )
        self._mergeability_retry_after = {
            key: retry_after
            for key, retry_after in self._mergeability_retry_after.items()
            if key in active_observation_keys
        }
        self._metadata_unconfirmed_keys.intersection_update(
            active_observation_keys
        )

        return list(self.prs.values())

    async def _rich_metadata_list(
        self,
        root: str | None,
        now: datetime,
        *,
        force: bool,
        bootstrap: bool = True,
    ) -> tuple[list[dict] | None, bool]:
        """Run the expensive rich PR list only after quota admission."""

        context, decision = self._quota_allows(
            force=force, estimated_cost=METADATA_GRAPHQL_ESTIMATED_COST
        )
        if not decision.allowed:
            self.log(
                f"Deferring rich metadata for {root}: quota {decision.reason.value}",
                level="warn",
            )
            self._metadata_last_attempt[root] = now
            self._note_open_set_unobserved(
                root, f"rich metadata deferred: quota {decision.reason.value}"
            )
            return self._metadata_cache.get(root), False

        reservation = self.quota_ledger.reserve(
            context.caller,
            context.work_class,
            estimated_cost=context.estimated_cost,
        )
        if reservation is None:
            self.log(
                f"Deferring rich metadata for {root}: quota admission changed",
                level="warn",
            )
            self._metadata_last_attempt[root] = now
            self._note_open_set_unobserved(
                root, "rich metadata deferred: quota admission changed"
            )
            return self._metadata_cache.get(root), False

        list_error: Exception | None = None
        invalid_list_result = False
        try:
            listed_prs = await asyncio.to_thread(github_api.list_open_prs, root)
        except Exception as exc:  # noqa: BLE001 - preserve cached metadata
            listed_prs = None
            list_error = exc
        if listed_prs is not None and not isinstance(listed_prs, list):
            listed_prs = None
            invalid_list_result = True
            list_error = TypeError("list_open_prs returned a non-list result")
        self._metadata_last_attempt[root] = now
        if listed_prs is None:
            failure = github_api.last_list_open_prs_failure()
            completed_unparseable = (
                failure is not None
                and failure.reason in {"invalid-json", "not-a-list"}
            ) or invalid_list_result
            if completed_unparseable:
                # GitHub completed this GraphQL-backed command and consumed
                # quota even though its payload could not be projected. Charge
                # the conservative reservation once, then retain degradation
                # below because the observation itself was unusable.
                self._record_estimated_success(context, reservation)
            else:
                self._record_estimated_failure(reservation)
            self.quota_ledger.record_failure(
                reason="rich_metadata_unavailable",
                count_request=not completed_unparseable,
            )
            if failure is not None and failure.is_rate_limited:
                self.quota_ledger.record_backoff(
                    METADATA_GRAPHQL_FAILURE_BACKOFF,
                    reason="rich_metadata_rate_limited",
                )
            detail = f" — {failure.summary()}" if failure else ""
            if list_error is not None:
                detail = f" — {list_error}"
            self.log(
                f"Skipping metadata refresh for {root}: could not list open PRs"
                f" (GitHub API unavailable{detail})",
                level="error",
            )
            self._note_open_set_unobserved(
                root, f"could not list open PRs{detail}"
            )
            return self._metadata_cache.get(root), False

        raw_prs = [raw for raw in listed_prs if isinstance(raw, dict)]
        self._record_estimated_success(context, reservation)
        await self._cache_metadata(root, raw_prs, now)
        if bootstrap:
            await self._bootstrap_metadata_validator(root)
        self.observed_roots.add(root)
        return raw_prs, True

    async def _bootstrap_metadata_validator(self, root: str | None) -> None:
        """Capture a REST validator after the first rich discovery.

        ``gh pr list`` does not expose response headers, so the first rich
        discovery cannot itself provide an ETag.  A one-time body-bearing REST
        request captures the validator for the next 15-minute reconciliation;
        later probes can then receive a cheap 304.  Failure is harmless: the
        next scheduled probe is an ordinary 200 and will establish it.
        """

        if root in self._metadata_validators:
            return
        identity = self._root_repo_identity.get(root, "")
        if "/" not in identity:
            return
        # Avoid surprising old lightweight adapters that replace only the rich
        # list boundary. Production and explicit probe adapters still get the
        # validator bootstrap.
        if (
            github_api.list_open_prs.__module__ != github_api.__name__
            and github_api.probe_open_prs_rest.__module__ == github_api.__name__
        ):
            return
        owner, repo_name = identity.split("/", 1)
        try:
            probe = await asyncio.to_thread(
                github_api.probe_open_prs_rest,
                owner,
                repo_name,
                cwd=root,
            )
        except Exception:  # noqa: BLE001 - validator is an optimization
            return
        if probe.observable and (probe.etag or probe.last_modified):
            self._metadata_validators[root] = (probe.etag, probe.last_modified)
            self._metadata_validator_truncated[root] = probe.truncated

    def _rich_read(
        self, raw_prs: list[dict] | None, rich_read: bool
    ) -> _MetadataRead:
        """Wrap a rich-relist result, deriving its authoritative open set.

        A successful rich list is authoritative about which PRs are open; a
        failed or quota-denied one proves nothing and must not prune.
        """

        if rich_read and raw_prs is not None:
            return _MetadataRead(raw_prs, True, _open_numbers(raw_prs), observed=True)
        return _MetadataRead(raw_prs, rich_read, None)

    async def _read_metadata(
        self,
        root: str | None,
        now: datetime,
        *,
        force: bool,
        rich_due: bool = True,
    ) -> _MetadataRead:
        """Read metadata using rich discovery or a conditional REST validator.

        ``rich_due`` is whether the *expensive* clock says a GraphQL relist is
        owed. When it is False this call is a cheap validation pass on
        ``LIST_VALIDATION_INTERVAL``: it may prune from the probe's own
        authoritative open-PR list, but it must not spend GraphQL and must not
        advance the rich relist's clocks (BOU-3095).
        """

        if force or root not in self._metadata_cache:
            return self._rich_read(
                *await self._rich_metadata_list(root, now, force=force)
            )

        # Lightweight test/embedding adapters historically replace the rich
        # list boundary but do not provide a REST probe. Preserve that adapter
        # contract; production functions live in ``github_api`` and always use
        # the conditional path below.
        if (
            github_api.list_open_prs.__module__ != github_api.__name__
            and github_api.probe_open_prs_rest.__module__ == github_api.__name__
        ):
            if not rich_due:
                return _MetadataRead(self._metadata_cache[root], False, None)
            return self._rich_read(
                *await self._rich_metadata_list(root, now, force=force)
            )

        identity = self._root_repo_identity.get(root, "")
        if "/" not in identity:
            # A legacy fixture may have supplied metadata without a URL.  The
            # rich list is the only way to establish a REST repository target.
            if not rich_due:
                return _MetadataRead(self._metadata_cache[root], False, None)
            return self._rich_read(
                *await self._rich_metadata_list(root, now, force=force)
            )

        owner, repo_name = identity.split("/", 1)
        etag, last_modified = self._metadata_validators.get(root, (None, None))
        try:
            probe = await asyncio.to_thread(
                github_api.probe_open_prs_rest,
                owner,
                repo_name,
                etag=etag,
                last_modified=last_modified,
                cwd=root,
            )
        except Exception as exc:  # noqa: BLE001 - retain the last good cache
            probe = github_api.ConditionalPRListProbe(
                None, [], etag=etag, last_modified=last_modified, error=str(exc)
            )
        # The cheap validator has its own clock. Only a probe run because the
        # rich clock was owed may stamp that clock — otherwise a 60-second
        # validator would keep resetting the 15-minute reconciliation and the
        # rich relist would never come due (BOU-3095).
        self._list_validation_last_attempt[root] = now
        if rich_due:
            self._metadata_last_attempt[root] = now
        if probe.not_modified:
            self._metadata_validators[root] = (
                probe.etag or etag,
                probe.last_modified or last_modified,
            )
            # A 304 revalidates the representation the ETag was built from. If
            # that was a full first page, an older tracked PR outside it can
            # close without changing page 1 — so this confirms a page, not the
            # open set, and must not refresh the observation timestamp
            # (BOU-3095 PR #169 review round 4).
            validator_complete = not self._metadata_validator_truncated.get(root, False)
            last_rich = self._metadata_last_rich_success.get(root)
            if rich_due and (
                last_rich is None
                or now >= last_rich + RICH_METADATA_RECONCILIATION_INTERVAL
            ):
                # The REST validator says the open-PR representation is
                # unchanged, but it cannot validate GraphQL-only fields such
                # as reviewDecision or mergeability. Periodically refresh the
                # rich snapshot independently of that validator.
                return self._rich_read(
                    *await self._rich_metadata_list(
                        root, now, force=force, bootstrap=False
                    )
                )
            cached = self._metadata_cache[root]
            if not rich_due:
                # A cheap confirmation that nothing changed. It is not a
                # metadata reconciliation, so it must not advance the expensive
                # clocks — and it yields no authoritative open set, because only
                # a successful rich relist may drive pruning. Nothing to prune
                # here anyway: a 304 means the open list is byte-identical.
                return _MetadataRead(cached, False, None, observed=validator_complete)
            self._metadata_last_success[root] = now
            event_due = self._metadata_event_due.get(root)
            if event_due is not None and now >= event_due:
                self._metadata_event_due.pop(root, None)
            if self._has_pending_opened(root, now):
                self._opened_event_retry_due[root] = (
                    now + METADATA_COLD_START_RETRY_INTERVAL
                )
            else:
                self._opened_event_retry_due.pop(root, None)
            self.quota_ledger.record_cache_hit(
                QuotaCaller.OPERATOR if force else QuotaCaller.DASHBOARD,
                QuotaWorkClass.EXPLICIT_OPERATOR
                if force
                else QuotaWorkClass.BACKGROUND_OBSERVATION,
            )
            # A validator-confirmed 304 is an authoritative metadata
            # observation. The cached list is unchanged, but the controller
            # must acknowledge any metadata invalidation carried by this plan.
            # It still supplies no open set: pruning is reserved for a
            # successful rich relist, and a 304 has nothing to prune.
            cached = self._metadata_cache[root]
            return _MetadataRead(cached, True, None, observed=validator_complete)

        if probe.changed:
            # The 200 body is an authoritative, author-filtered open-PR list in
            # its own right. Derive the open set from it FIRST, so a merged PR
            # leaves the board even when the rich relist below is deferred or
            # quota-denied — that deferral is exactly how a merged PR used to
            # sit on the board for hours (BOU-3095).
            cached = self._metadata_cache[root]
            self._merge_probe_activity(root, probe.prs)
            if not rich_due:
                # The probe is a CHANGE DETECTOR, never an authority on removal.
                #
                # Three review rounds found three different ways for a
                # page-limited, author-filtered REST body to be wrong about
                # which PRs are open — an App-spelled author matching nothing,
                # a full first page hiding an older PR, an empty body during an
                # outage — and each one prunes real PRs off the board. Rather
                # than keep adding guards for individual instances, only a
                # successful rich relist may ever produce an authoritative open
                # set (BOU-3095 PR #169 review round 3). The probe's job is to
                # notice that something moved and get that relist scheduled
                # promptly, which is what keeps a merged PR clearing in about a
                # minute instead of fifteen.
                self._metadata_event_due[root] = now + EVENT_DEBOUNCE_WINDOW
                return _MetadataRead(cached, False, None)
            raw_prs, rich_read = await self._rich_metadata_list(
                root, now, force=force, bootstrap=False
            )
            if rich_read:
                self._metadata_validators[root] = (
                    probe.etag or etag,
                    probe.last_modified or last_modified,
                )
                # Remember whether the page this validator was built from was
                # complete: a later 304 revalidates only that page.
                self._metadata_validator_truncated[root] = probe.truncated
                # This is the tick on which the probe actually saw a change, and
                # the rich list carries no ``updatedAt`` at all. Dropping the
                # probe's timestamps here loses the activity signal precisely
                # when it exists — and the validator has just been advanced, so
                # the following probes come back 304 and the review slice is
                # never invalidated (BOU-3095 PR #169 review). Merge them into
                # the freshly cached list and return that, so ``_note_pr_activity``
                # sees them.
                self._merge_probe_activity(root, probe.prs)
                merged = self._metadata_cache.get(root)
                if merged is not None:
                    return _MetadataRead(merged, True, _open_numbers(merged), observed=True)
                return _MetadataRead(raw_prs, True, _open_numbers(raw_prs or []), observed=True)
            # The relist failed or was quota-denied. The probe detected a change
            # but cannot say what it was, so nothing may be pruned on it.
            return _MetadataRead(raw_prs, False, None)

        # Keep the old validator when the probe failed.  Reusing a new ETag
        # from a failed/partial response could turn a temporary outage into a
        # permanent 304 and leave the dashboard stale forever.
        detail = probe.error or "REST open-PR probe unavailable"
        self._open_set_degraded_reason[root] = detail
        self.log(
            f"Skipping metadata reconciliation for {root}: {detail}",
            level="warn",
        )
        # The raw cache remains authoritative for projection but cannot prove
        # pruning. The failure costs one validation window, not a full
        # reconciliation interval — ``_list_validation_last_attempt`` is what
        # was stamped above.
        return _MetadataRead(self._metadata_cache[root], False, None)

    async def _refresh_repo(
        self, root: str | None, now: datetime, *, force: bool = False
    ) -> None:
        """Poll and enrich every open PR for a single repo root.

        ``root`` is the cwd passed to every ``github_api.*`` call so the poll is
        scoped to that repo. Tracked PRs are pruned per-root: only PRs this root
        discovered are eligible to be pruned, so another repo's PRs (or a repo
        whose poll just failed) are never dropped.
        """
        metadata_due = self._metadata_refresh_due(root, now, force=force)
        # The cheap conditional validator runs on its own fast clock, so a
        # merged PR is pruned within a validation window instead of waiting out
        # the rich relist's reconciliation interval (BOU-3095).
        validation_due = not metadata_due and self._list_validation_due(root, now)
        raw_prs: list[dict] | None
        metadata_read = False
        open_numbers: set[int] | None = None
        observed = False
        if metadata_due or validation_due:
            read = await self._read_metadata(
                root, now, force=force, rich_due=metadata_due
            )
            raw_prs = read.raw_prs
            metadata_read = read.rich_read
            open_numbers = read.open_numbers
            observed = read.observed
            if raw_prs is None:
                return
            if metadata_due:
                event_due = self._metadata_event_due.get(root)
                if (
                    not metadata_read
                    and event_due is not None
                    and now >= event_due
                ):
                    self._metadata_event_due[root] = now + self._metadata_interval()
                if not metadata_read:
                    if self._has_pending_opened(root, now):
                        self._opened_event_retry_due[root] = (
                            now + METADATA_COLD_START_RETRY_INTERVAL
                        )
                    else:
                        self._opened_event_retry_due.pop(root, None)
        else:
            raw_prs = self._metadata_cache.get(root)
            if raw_prs is None:
                return

        # Prune merged/closed PRs FIRST, but only against a positively observed
        # open set. A cached list is observationally useful yet cannot prove
        # that a missing PR is closed, so it must never prune current state.
        if observed:
            self._open_set_last_observed[root] = now
            self._open_set_degraded_reason.pop(root, None)
        if open_numbers is not None:
            for key in list(self.prs.keys()):
                if self._pr_root.get(key) != root:
                    continue
                if key[1] not in open_numbers:
                    old = self.prs.pop(key)
                    self._pr_root.pop(key, None)
                    self._observation_keys.pop(key, None)
                    self._event_head_overrides.pop(key, None)
                    self._event_head_override_observed_at.pop(key, None)
                    self._pr_last_updated_at.pop(key, None)
                    self.log(
                        f"PR #{key[1]} closed/merged: {old.title}",
                        pr_number=key[1],
                        level="success",
                    )
            # The projection below rebuilds ``self.prs`` from ``raw_prs``. When
            # the open set came from the cheap validator the cached rich list is
            # still the pre-merge one, so without this the PR just pruned would
            # be re-discovered on the same tick. Narrow the cache to the
            # observed open set so it stays consistent until the next rich
            # relist replaces it wholesale.
            if any(
                isinstance(raw.get("number"), int)
                and raw["number"] not in open_numbers
                for raw in raw_prs
            ):
                raw_prs = [
                    raw
                    for raw in raw_prs
                    if isinstance(raw.get("number"), int)
                    and raw["number"] in open_numbers
                ]
                if root in self._metadata_cache:
                    self._metadata_cache[root] = raw_prs

        # Drop the previous tick's batch entries before planning. A CI-only
        # refresh must reach the live CI accessor rather than reusing a stale
        # full-observation cache entry.
        github_api.clear_pr_batch_cache()

        # Get-or-create every PR object up front (cheap, mutates the shared
        # ``self.prs`` map serially to avoid races), then enrich the independent
        # per-PR REST/GraphQL work CONCURRENTLY below (BOU-1637 #4). Each PR's
        # enrichment only touches its own ``PRData``, so gathering them is safe.
        to_enrich: list[tuple[PRData, dict, ObservationPlan | None]] = []
        for raw in raw_prs:
            num = raw.get("number")
            if not isinstance(num, int):
                continue

            repo = _normalize_repo(_repo_from_url(str(raw.get("url", ""))))
            key: PRKey = (repo, num)

            # Get or create PR data
            pr = self.prs.get(key)
            newly_discovered = pr is None
            if pr is None:
                pr = PRData(
                    number=num,
                    repo=repo,
                    title=raw.get("title", ""),
                    branch=raw.get("headRefName", ""),
                    base_branch=raw.get("baseRefName", "") or "main",
                    url=raw.get("url", ""),
                    is_draft=raw.get("isDraft", False),
                    created_at=raw.get("createdAt", ""),
                )
                self.prs[key] = pr
                self.log(f"Discovered PR #{num}: {pr.title}", pr_number=num)
            self._pr_root[key] = root

            # GitHub's bulk PR listing exposes the immutable head directly.
            # Keep that value as the observation identity: the latest-commit
            # accessor is a requested read, not the identity that decides
            # whether a plan is due.
            head_sha = self._event_head_overrides.get(key) or str(
                raw.get("headRefOid") or ""
            )
            if not head_sha:
                # Older test/fixture payloads and degraded REST fallbacks may
                # omit headRefOid. Keep those callers on a stable legacy key;
                # real GitHub list payloads always take the branch above.
                head_sha = f"legacy-head-{num}"
            observation_repo = pr.repo or root or "legacy-repo"
            observation_number = num
            if num <= 0:
                # ObservationKey models real GitHub PR identities, whose
                # numbers are positive. Keep malformed fixture/REST entries
                # isolated without letting one bad number abort this repo's
                # enrichment batch.
                observation_repo = f"{observation_repo}:legacy-{num}"
                observation_number = 1
            observation_key = ObservationKey(
                observation_repo, observation_number, head_sha
            )
            self._observation_keys[key] = observation_key
            self._note_pr_activity(
                key, observation_key, raw, now, newly_discovered=newly_discovered
            )
            plan = self.observation_controller.plan_for(
                observation_key.repo,
                observation_key.number,
                observation_key.head_sha,
                now=now,
                force=force,
            )
            plan = self._plan_for_available_slices(
                plan,
                metadata_read=metadata_read,
            )
            to_enrich.append((pr, raw, plan))

        # Prime the existing per-PR accessors from one immutable-head GraphQL
        # observation, but only when at least one PR needs BOTH review and CI.
        # Missing/paginated entries deliberately fall through to the original
        # per-PR accessors, preserving their correctness behavior.
        batch_numbers = sorted(
            {
                pr.number
                for pr, raw, plan in to_enrich
                if plan is not None
                and bool(raw.get("headRefOid"))
                and ObservationSlice.REVIEW in plan.slices
                and ObservationSlice.CI in plan.slices
            }
        )
        batch_denied_numbers: set[int] = set()
        batch_observed_numbers: set[int] = set()
        batch_fallback_numbers: set[int] = set()
        if batch_numbers:
            batch_context, batch_decision = self._quota_allows(
                force=force, estimated_cost=BATCH_GRAPHQL_ESTIMATED_COST
            )
            if not batch_decision.allowed:
                # A denied batch is an unobservable full read. Do not let the
                # per-PR fallback accessors issue unmetered GraphQL requests.
                batch_denied_numbers.update(batch_numbers)
                self.log(
                    f"Deferring review/CI batch for {root}: quota "
                    f"{batch_decision.reason.value}",
                    level="warn",
                )
            else:
                # Until the result proves otherwise, every requested PR needs
                # an explicitly accounted fallback. A partial/failed batch
                # must not silently reach the old unmetered GraphQL accessors.
                batch_fallback_numbers.update(batch_numbers)
                try:
                    identity = self._root_repo_identity.get(root)
                    if identity and "/" in identity:
                        owner, repo_name = identity.split("/", 1)
                    else:
                        owner, repo_name = await asyncio.to_thread(
                            github_api.get_repo_info, root
                        )
                    if owner and repo_name:
                        try:
                            entries = await asyncio.to_thread(
                                github_api.batch_fetch_pr_review_and_ci,
                                owner,
                                repo_name,
                                batch_numbers,
                                root,
                                quota_context=batch_context,
                            )
                        except TypeError as exc:
                            # Preserve the old four-argument adapter contract
                            # used by embedding callers and focused tests.
                            if "quota_context" not in str(exc):
                                raise
                            entries = await asyncio.to_thread(
                                github_api.batch_fetch_pr_review_and_ci,
                                owner,
                                repo_name,
                                batch_numbers,
                                root,
                            )
                        github_api.prime_pr_batch_cache(
                            f"{owner}/{repo_name}", entries, cwd=root
                        )
                        batch_observed_numbers.update(
                            number
                            for number in entries
                            if number in batch_numbers
                        )
                        batch_fallback_numbers.difference_update(
                            batch_observed_numbers
                        )
                        if getattr(entries, "denied", False):
                            denied_numbers = set(batch_numbers) - batch_observed_numbers
                            batch_denied_numbers.update(denied_numbers)
                            batch_fallback_numbers.difference_update(denied_numbers)
                except Exception as exc:  # noqa: BLE001
                    self.log(
                        f"Batch observation failed for repo root {root}: {exc}",
                        level="warn",
                    )

        # Enrich each PR concurrently, bounded by a modest semaphore so a large
        # pool doesn't hammer the gh API into a rate-limit wall. Per-PR error
        # isolation: one PR's failed enrichment must not drop the others.
        sem = asyncio.Semaphore(ENRICHMENT_CONCURRENCY)

        async def _guarded(
            pr: PRData, raw: dict, plan: ObservationPlan | None
        ) -> None:
            async with sem:
                await self._enrich_pr(
                    pr,
                    raw,
                    root,
                    now,
                    plan,
                    batch_denied=pr.number in batch_denied_numbers,
                    batch_observed=pr.number in batch_observed_numbers,
                    batch_fallback=pr.number in batch_fallback_numbers,
                    force=force,
                )

        results = await asyncio.gather(
            *(_guarded(pr, raw, plan) for pr, raw, plan in to_enrich),
            return_exceptions=True,
        )
        for (pr, _raw, _plan), result in zip(to_enrich, results):
            if isinstance(result, Exception):
                self.log(
                    f"Enrichment failed for PR #{pr.number}: {result}",
                    pr_number=pr.number,
                    level="error",
                )

    async def _enrich_pr(
        self,
        pr: PRData,
        raw: dict,
        root: str | None,
        now: datetime,
        plan: ObservationPlan | None = None,
        *,
        batch_denied: bool = False,
        batch_observed: bool = False,
        batch_fallback: bool = False,
        force: bool = False,
    ) -> None:
        """Enrich a single PR with its per-PR REST/GraphQL state.

        Only ``pr``'s own fields are mutated here, so multiple ``_enrich_pr``
        calls run concurrently without sharing state (BOU-1637 #4). ``root`` is
        the repo cwd passed to every ``github_api.*`` call so the enrichment is
        scoped to that PR's repo. ``plan`` controls the expensive review and CI
        reads; ``None`` refreshes cheap list metadata and projects status from
        the already-cached observation.
        """
        num = pr.number
        review_requested = (
            plan is not None and ObservationSlice.REVIEW in plan.slices
        )
        ci_requested = plan is not None and ObservationSlice.CI in plan.slices
        metadata_requested = (
            plan is not None and ObservationSlice.METADATA in plan.slices
        )
        full_observation_requested = (
            plan is not None
            and ObservationSlice.REVIEW in plan.slices
            and ObservationSlice.CI in plan.slices
        )

        # Update metadata
        pr.title = raw.get("title", pr.title)
        pr.branch = raw.get("headRefName", pr.branch) or pr.branch
        pr.url = raw.get("url", pr.url) or pr.url
        draft_value = raw.get("isDraft")
        if isinstance(draft_value, bool):
            pr.is_draft = draft_value
        pr.base_branch = raw.get("baseRefName", pr.base_branch) or pr.base_branch
        pr.review_decision = raw.get("reviewDecision", "") or "none"
        pr.labels = [
            label.get("name", "")
            for label in raw.get("labels", [])
            if isinstance(label, dict) and label.get("name")
        ]

        # GitHub computes mergeability lazily: a bulk `gh pr list` often
        # returns UNKNOWN/blank for both signals right after a push or a
        # base-branch move. A definite bulk value is authoritative; an
        # UNKNOWN/blank one is that async-compute window — keep the last-known
        # value rather than erasing a known conflict, then force a per-PR
        # re-fetch below (which also triggers the computation). Without this,
        # a CONFLICTING PR whose next bulk poll returns UNKNOWN would be reset
        # to "unknown" and, if the refetch then failed, slide back to Clean.
        bulk_merge_state = raw.get("mergeStateStatus") or ""
        bulk_mergeable = raw.get("mergeable") or ""
        metadata_mergeability_observed = True
        mergeability_key = (
            plan.key
            if plan is not None
            else self._observation_keys.get((pr.repo, num))
        )
        if bulk_merge_state and bulk_merge_state != "UNKNOWN":
            pr.merge_state = bulk_merge_state
        if bulk_mergeable and bulk_mergeable != "UNKNOWN":
            pr.mergeable = bulk_mergeable
        if (
            metadata_requested
            and mergeability_key is not None
            and bulk_merge_state
            and bulk_merge_state != "UNKNOWN"
            and bulk_mergeable
            and bulk_mergeable != "UNKNOWN"
        ):
            self._mergeability_pending_keys.discard(mergeability_key)
            self._mergeability_retry_after.pop(mergeability_key, None)

        # Find worktree — scoped to THIS repo's root so a same-named branch
        # in a sibling repo can't resolve a multi-repo PR to the wrong
        # checkout (BOU-1720). ``root`` is None only in the legacy
        # single-repo/no-cwd path, which restores the unscoped behavior.
        pr.worktree_path = find_worktree_for_branch(pr.branch, root=root)

        mergeability_fields_present = (
            "mergeStateStatus" in raw or "mergeable" in raw
        )
        if metadata_requested and mergeability_fields_present and (
            bulk_merge_state in ("", "UNKNOWN")
            or bulk_mergeable in ("", "UNKNOWN")
        ):
            fallback_attempted = False
            primed_mergeability = github_api.get_primed_mergeability(num, root)
            if primed_mergeability is not None:
                refetched_state, refetched_mergeable = primed_mergeability
            else:
                retry_after = (
                    self._mergeability_retry_after.get(mergeability_key)
                    if mergeability_key is not None
                    else None
                )
                if not force and retry_after is not None and now < retry_after:
                    refetched_state, refetched_mergeable = "", ""
                else:
                    fallback_attempted = True
                    refetched_state, refetched_mergeable = (
                        await self._mergeability_fallback(
                            num,
                            root,
                            force=force,
                        )
                    )
            metadata_mergeability_observed = bool(
                refetched_state
                and refetched_state.upper() != "UNKNOWN"
                and refetched_mergeable
                and refetched_mergeable.upper() != "UNKNOWN"
            )
            if mergeability_key is not None:
                if metadata_mergeability_observed:
                    self._mergeability_pending_keys.discard(mergeability_key)
                    self._mergeability_retry_after.pop(mergeability_key, None)
                else:
                    self._mergeability_pending_keys.add(mergeability_key)
                    if fallback_attempted:
                        self._mergeability_retry_after[mergeability_key] = (
                            now + METADATA_RECONCILIATION_INTERVAL
                        )
            # A successful refetch is authoritative — it also picks up a
            # conflict that has since been resolved. A failed refetch returns
            # ("","") and leaves the preserved last-known value intact.
            if refetched_state and refetched_state.upper() != "UNKNOWN":
                pr.merge_state = refetched_state
            if refetched_mergeable and refetched_mergeable.upper() != "UNKNOWN":
                pr.mergeable = refetched_mergeable

        # Stage requested observations before mutating blocker state. GitHub
        # boundary failures commonly look like an empty collection; the typed
        # results below distinguish that from an authoritative clean result.
        latest_result = None
        ci_result = None
        ci_from_rest = False
        ci_rest_head = ""
        cached_ci_head = pr.latest_commit_sha
        review_result = None
        latest_commit_date = pr.latest_commit_date
        plan_head_changed = False
        if full_observation_requested:
            plan_head = plan.key.head_sha if plan is not None else ""
            latest_value = await asyncio.to_thread(
                github_api.get_latest_commit, num, root
            )
            if (
                len(latest_value) >= 2
                and bool(latest_value[0])
                and bool(latest_value[1])
                and _observation_head_matches(
                    plan_head, str(latest_value[0])
                )
            ):
                latest_result = github_api.ObservationReadResult.observed(
                    (str(latest_value[0]), str(latest_value[1]))
                )
            else:
                plan_head_changed = bool(
                    len(latest_value) >= 1
                    and latest_value[0]
                    and plan_head
                    and not _observation_head_matches(
                        plan_head, str(latest_value[0])
                    )
                )
                latest_result = github_api.ObservationReadResult.unavailable(
                    "latest commit observation unavailable"
                )
            if latest_result.observable and latest_result.value is not None:
                _sha, latest_commit_date = latest_result.value
            if plan_head_changed:
                review_result = github_api.ObservationReadResult.unavailable(
                    "review observation unavailable: observation plan head "
                    "is no longer current"
                )
            elif batch_denied:
                review_result = github_api.ObservationReadResult.unavailable(
                    "review observation deferred: dashboard quota denied batch"
                )
            elif batch_observed:
                # The batch already paid for and populated the immutable-head
                # review cache. Consume that authoritative slice directly;
                # the post-response remaining value may now protect the
                # reserve, but it must not invalidate a successful read.
                review_result = await asyncio.to_thread(
                    github_api.scan_review_threads_observation,
                    num,
                    latest_commit_date,
                    root,
                )
            else:
                review_result = await self._review_fallback_observation(
                    num,
                    latest_commit_date,
                    root,
                    force=force,
                )
        elif review_requested:
            # Review-only events must not spend another latest-commit read. The
            # cached commit date is reusable only when it belongs to this
            # plan's immutable head. A partial head-change observation can
            # leave the previous head's date on ``pr``; refetch the prerequisite
            # before scanning rather than applying stale review history.
            plan_head = plan.key.head_sha if plan is not None else ""
            if (
                pr.latest_commit_date
                and pr.latest_commit_sha
                and _observation_head_matches(
                    plan_head, pr.latest_commit_sha
                )
            ):
                review_result = await self._review_fallback_observation(
                    num,
                    pr.latest_commit_date,
                    root,
                    force=force,
                )
            else:
                latest_value = await asyncio.to_thread(
                    github_api.get_latest_commit, num, root
                )
                if (
                    len(latest_value) >= 2
                    and bool(latest_value[0])
                    and bool(latest_value[1])
                    and _observation_head_matches(
                        plan_head, str(latest_value[0])
                    )
                ):
                    latest_result = github_api.ObservationReadResult.observed(
                        (str(latest_value[0]), str(latest_value[1]))
                    )
                    latest_commit_date = str(latest_value[1])
                    review_result = await self._review_fallback_observation(
                        num,
                        latest_commit_date,
                        root,
                        force=force,
                    )
                else:
                    latest_result = github_api.ObservationReadResult.unavailable(
                        "latest commit observation unavailable for review retry"
                    )
                    review_result = github_api.ObservationReadResult.unavailable(
                    "review observation unavailable: current-head latest "
                    "commit date missing"
                    )
        if ci_requested:
            if full_observation_requested and plan_head_changed:
                ci_result = github_api.ObservationReadResult.unavailable(
                    "CI observation unavailable: observation plan head is no "
                    "longer current"
                )
            elif (
                batch_denied
                or batch_fallback
                or (not review_requested and not full_observation_requested)
            ):
                # A pending/check-event-only poll, and the CI half of a denied
                # full batch, must stay on REST and use the cached immutable
                # head. It is intentionally outside the GraphQL background
                # budget, so a depleted review budget does not hide runner
                # progress or fall through to an unmetered ``gh pr checks``.
                # REST CI must be keyed to this refresh's immutable head. A
                # cached PR object can still carry the prior head while a
                # plan/raw metadata read has already advanced to a new one.
                # Prefer the plan identity, then a validated latest-commit
                # result, then raw metadata; consult the cached PR only for
                # legacy callers that have none of those current-head values.
                cached_head = ""
                if (
                    plan is not None
                    and plan.key.head_sha
                    and not plan.key.head_sha.startswith("legacy-head-")
                ):
                    cached_head = str(plan.key.head_sha)
                if (
                    not cached_head
                    and latest_result is not None
                    and latest_result.observable
                    and latest_result.value is not None
                ):
                    cached_head = str(latest_result.value[0] or "")
                if not cached_head:
                    cached_head = str(raw.get("headRefOid") or "")
                if not cached_head:
                    cached_head = pr.latest_commit_sha
                ci_from_rest = True
                ci_rest_head = cached_head
                ci_result = await asyncio.to_thread(
                    github_api.get_ci_checks_rest_observation,
                    cached_head,
                    root,
                    pr_number=num,
                )
            else:
                ci_result = await asyncio.to_thread(
                    github_api.get_ci_checks_observation, num, root
                )

        latest_observed = (
            latest_result is not None
            and latest_result.observable
            and latest_result.value is not None
        )
        ci_observed = (
            ci_result is not None
            and ci_result.observable
            and ci_result.value is not None
        )
        review_observed = (
            review_result is not None
            and review_result.observable
            and review_result.value is not None
            and (not full_observation_requested or latest_observed)
        )
        failed_requested_slice = (
            metadata_requested
            and (
                not metadata_mergeability_observed
                or (full_observation_requested and not latest_observed)
            )
        ) or (ci_requested and not ci_observed) or (
            review_requested and not review_observed
        )
        if failed_requested_slice:
            errors = [
                result.error or "unobservable read"
                for result in (latest_result, ci_result, review_result)
                if result is not None
                and (not result.observable or result.value is None)
            ]
            if (
                full_observation_requested
                and review_result is not None
                and review_result.observable
                and not latest_observed
            ):
                errors.append(
                    "review observation unavailable: latest commit prerequisite "
                    "unavailable"
                )
            if metadata_requested and not metadata_mergeability_observed:
                errors.append("mergeability observation unavailable")
            self.log(
                f"Preserving cached blockers for PR #{num}: {'; '.join(errors)}",
                pr_number=num,
                level="warn",
            )

        if latest_observed and latest_result is not None:
            previous_commit_sha = pr.latest_commit_sha
            sha, latest_commit_date = latest_result.value
            pr.latest_commit_sha = sha
            pr.latest_commit_date = latest_commit_date
            if previous_commit_sha and sha != previous_commit_sha:
                pr.no_push_comment_retry_count = 0

        if ci_observed and ci_result is not None:
            checks = list(ci_result.value)
            if (
                ci_from_rest
                and cached_ci_head
                and ci_rest_head == cached_ci_head
            ):
                observed_names = {check.name for check in checks}
                checks.extend(
                    check
                    for check in pr.ci_checks
                    if check.status in {"queued", "in_progress"}
                    and check.name not in observed_names
                )
            pr.ci_checks = checks
            ci_pending = any(
                c.status in {"queued", "in_progress"} for c in checks
            )
            # Queue diagnostics belong to the CI slice, not the full
            # observation. A PR that stays pending after its first full read
            # is polled every 30s with a CI-only plan.
            if ci_pending:
                queued_jobs, runner_pool_health, runner_execution_summary = (
                    await asyncio.to_thread(
                        github_api.get_workflow_queue_health, num, root
                    )
                )
                pr.queued_jobs = queued_jobs
                pr.runner_pool_health = runner_pool_health
                pr.runner_execution_summary = runner_execution_summary
            else:
                pr.queued_jobs = []
                pr.runner_pool_health = []
                pr.runner_execution_summary = (
                    pr.runner_execution_summary.__class__()
                )
            pr.failing_checks = [
                c.name
                for c in checks
                if c.conclusion == "failure"
                and not github_api._is_infra_check(c.name)
            ]

        if review_observed and review_result is not None:
            comments, decisions = review_result.value
            pr.review_comments = comments
            self._emit(
                "comment_scan",
                pr_number=num,
                repo=root,
                details={
                    "decisions": [d.model_dump() for d in decisions],
                    "picked": len(comments),
                },
            )
            if not comments:
                pr.no_push_comment_retry_count = 0
            current_comment_ids = {c.id for c in comments}
            new_comment_ids = current_comment_ids - pr.last_seen_comment_ids
            if new_comment_ids and num not in self._inflight_prs:
                if pr.agent_failure_reason:
                    self.log(
                        f"New unaddressed comment(s) on PR #{num} — "
                        "clearing stuck failure state",
                        pr_number=num,
                    )
                    pr.agent_failure_reason = None
                pr.no_push_comment_retry_count = 0
            pr.last_seen_comment_ids = current_comment_ids

        if plan is not None:
            observed_slices: set[ObservationSlice] = set()
            if metadata_requested and (
                metadata_mergeability_observed
                and (not full_observation_requested or latest_observed)
            ):
                observed_slices.add(ObservationSlice.METADATA)
            if ci_requested and ci_observed:
                observed_slices.add(ObservationSlice.CI)
            if review_requested and review_observed:
                observed_slices.add(ObservationSlice.REVIEW)
            if observed_slices:
                acknowledged_slices = frozenset(observed_slices)
                acknowledged_plan = ObservationPlan(
                    plan.key,
                    acknowledged_slices,
                    plan.reason,
                    frozenset(
                        (observation_slice, generation)
                        for observation_slice, generation in (
                            plan.invalidation_generations
                        )
                        if observation_slice in acknowledged_slices
                    ),
                )
                self.observation_controller.record_refresh(
                    acknowledged_plan,
                    ci_pending=(
                        any(
                            c.status in {"queued", "in_progress"}
                            for c in pr.ci_checks
                        )
                        if ObservationSlice.CI in acknowledged_slices
                        else False
                    ),
                )
                if ObservationSlice.METADATA in acknowledged_slices:
                    self._metadata_unconfirmed_keys.discard(plan.key)
                blocker_slices = observed_slices.intersection(
                    {ObservationSlice.REVIEW, ObservationSlice.CI}
                )
                if blocker_slices:
                    self._observed_blocker_slices.setdefault(
                        plan.key, set()
                    ).update(blocker_slices)
                if {
                    ObservationSlice.REVIEW,
                    ObservationSlice.CI,
                } <= self._observed_blocker_slices.get(plan.key, set()):
                    self._observed_blocker_keys.add(plan.key)

        # Read escalation marker (best-effort): if the maintenance loop has
        # repeatedly failed to fix this PR, flag it so the dashboard can surface
        # a badge and title-bar banner.
        try:
            from ._maintenance.stop_gate import _read_escalation_marker  # noqa: PLC0415
            esc = _read_escalation_marker(root or ".")
            if str(num) in esc:
                pr.escalated = True
                pr.escalated_reason = esc[str(num)].get("last_error")
            else:
                pr.escalated = False
                pr.escalated_reason = None
        except Exception:  # noqa: BLE001
            pass

        # Compute status
        #
        # ``observation_unavailable`` means THIS immutable head has never had a
        # successful review+CI read, so every blocker below came from a previous
        # head — the very push we could not observe may already have fixed it.
        # Reporting that stale verdict is not just cosmetic: CI_FAILING and the
        # comment statuses are auto-dispatch triggers, so maintenance would be
        # launched against work that no longer exists. Fail closed to
        # "unavailable" for every observation-derived status, not only CLEAN.
        # Statuses sourced elsewhere stay authoritative: a pending human
        # decision and a local agent failure are dashboard state. Merge
        # conflict normally comes from the metadata slice, but a positive
        # latest-head mismatch proves that metadata describes the old head too.
        current_observation_key = (
            plan.key
            if plan is not None
            else self._observation_keys.get((pr.repo, num))
        )
        raw_head = str(raw.get("headRefOid") or "")
        pending_event_head = self._event_head_overrides.get((pr.repo, num), "")
        metadata_head_unconfirmed = bool(
            pending_event_head and raw_head != pending_event_head
        )
        metadata_projection_unavailable = bool(
            metadata_head_unconfirmed
            or (metadata_requested and not metadata_mergeability_observed)
            or (
                current_observation_key is not None
                and (
                    current_observation_key in self._mergeability_pending_keys
                    or current_observation_key in self._metadata_unconfirmed_keys
                )
            )
        )
        observation_unavailable = bool(
            plan_head_changed
            or metadata_projection_unavailable
            or (
                current_observation_key is not None
                and current_observation_key not in self._observed_blocker_keys
            )
        )
        computed_status = self._compute_status(pr)
        pr.status = (
            PRStatus.OBSERVATION_UNAVAILABLE
            if (
                (plan_head_changed or metadata_projection_unavailable)
                and computed_status in _STALE_HEAD_DERIVED_STATUSES
            )
            or (
                observation_unavailable
                and computed_status in _OBSERVATION_DERIVED_STATUSES
            )
            else computed_status
        )
        pr.last_polled = now

        if not pr.worktree_path and pr.status == PRStatus.CLEAN:
            had_agent_state = (
                pr.activity_message is not None
                or pr.activity_source is not None
                or pr.agent_cli_name is not None
                or bool(pr.agent_output)
                or pr.agent_failure_reason is not None
            )
            pr.activity_message = None
            pr.activity_source = None
            pr.agent_cli_name = None
            pr.agent_output = []
            pr.agent_failure_reason = None
            if had_agent_state:
                self.log(
                    f"Cleared stale agent state for clean PR #{num} without a worktree",
                    pr_number=num,
                )

        # When a tracked PR transitions to CLEAN, clear its maintenance
        # display state so the card stops showing "Delegated". The worker
        # no longer writes a COMPLETE state; the dashboard owns this lifecycle.
        if pr.status == PRStatus.CLEAN and pr.maintenance is not None:
            # Best-effort close the tracked task if we know its id.
            if pr.maintenance.bead_id and pr.worktree_path:
                from .config import load as _load_config  # noqa: PLC0415
                from .tracker import get_tracker  # noqa: PLC0415

                get_tracker(_load_config(pr.worktree_path)).close_task(
                    pr.maintenance.bead_id, cwd=pr.worktree_path
                )
            # Release any dashboard-held coordinator claim now that the work
            # is done (a stale claim would otherwise sit until lease expiry).
            if pr.coordinator_claim:
                try:
                    coordinator.release_claim(
                        pr.coordinator_claim, DASHBOARD_OWNER_SESSION_ID, "completed"
                    )
                except Exception as exc:  # best-effort; lease bounds it anyway
                    self.log(
                        f"Could not release coordinator claim for #{num}: {exc}",
                        pr_number=num,
                        level="warn",
                    )
                pr.coordinator_claim = None
            pr.maintenance = None
            pr.activity_message = None
            pr.activity_source = None
            self.log(
                f"PR #{num} is clean — cleared delegated maintenance state",
                pr_number=num,
            )

        # Auto-dispatch for CI failures and unaddressed review comments.
        #
        # CI failures get priority when both are present — a CI fix commit
        # usually obsoletes stale comments, and the next poll will pick up
        # anything still outstanding as HAS_COMMENTS.
        can_dispatch = (
            num not in self._inflight_prs
            and pr.worktree_path
            and not pr.is_draft
            and not pr.agent_failure_reason
        )
        if can_dispatch:
            if pr.status in (
                PRStatus.MERGE_CONFLICT,
                PRStatus.CI_FAILING,
                PRStatus.CI_AND_COMMENTS,
                PRStatus.HAS_COMMENTS,
            ):
                # FIX 4: Reload on-disk maintenance state before evaluating the
                # already_queued guard. The `complete` CLI writes the state file in
                # a separate process, so pr.maintenance can be stale in-memory.
                # A COMPLETE/FAILED on-disk state must NOT suppress re-dispatch
                # when blockers reappear.
                if pr.worktree_path:
                    reloaded = maintenance.load_state(pr.worktree_path, pr.number)
                    if reloaded is not None:
                        pr.maintenance = reloaded

                coord_decision = coordinator.dispatch_decision_for_pr(pr)
                if coord_decision.state == "manual_intervention":
                    pr.activity_message = coord_decision.reason
                    pr.activity_source = "agent-coordinator"
                if coord_decision.should_dispatch:
                    asyncio.create_task(self.dispatch_pr_maintenance(pr))
                    self._emit(
                        "dispatch",
                        pr_number=num,
                        repo=root,
                        details={"status": str(pr.status), "failing_checks": pr.failing_checks},
                    )

        self._emit(
            "poll_tick",
            pr_number=num,
            repo=root,
            details={"status": str(pr.status), "comment_count": len(pr.review_comments)},
        )

    @staticmethod
    def _has_merge_conflict(pr: PRData) -> bool:
        """A conflict is signalled by EITHER GitHub field.

        `mergeStateStatus == DIRTY` and `mergeable == CONFLICTING` are computed
        from the same async pipeline but surface independently and at slightly
        different times; relying on DIRTY alone (the old behaviour) let a
        freshly-conflicting PR — whose mergeStateStatus was still UNKNOWN/UNSTABLE
        — slip into the Clean column.
        """
        return pr.merge_state == "DIRTY" or pr.mergeable == "CONFLICTING"

    def _compute_status(self, pr: PRData) -> PRStatus:
        # BOU-2402 (PR #110 review): highest precedence, and it must live HERE.
        # This method is what the board's own PRData objects get re-stamped with
        # on every enrichment pass; setting the status anywhere else (e.g. on the
        # transient PRData that `_maintenance/pr_state` builds) is overwritten
        # before a card is ever rendered, so no real card would acquire the
        # status and the "Needs Your Decision" column would stay permanently
        # empty while maintenance was in fact deferring.
        #
        # First because nothing below it can be acted on: a failing check or an
        # open comment on a PR whose architecture question is unanswered is not
        # the thing a human should be looking at.
        blocked = self._pending_decision_for(pr)
        if blocked is not None:
            pr.waiting_decision_id = blocked.request.decision_id
            pr.waiting_decision_question = blocked.request.question
            pr.waiting_decision_category = blocked.request.category
            pr.waiting_decision_runtime = blocked.request.requesting_runtime
            return PRStatus.WAITING_HUMAN_DECISION
        # Clear stale projections once the decision is answered or cancelled, so
        # a resolved question cannot keep rendering on the card.
        pr.waiting_decision_id = None
        pr.waiting_decision_question = None
        pr.waiting_decision_category = None
        pr.waiting_decision_runtime = None

        has_ci_failure = bool(pr.failing_checks)
        has_comments = bool(pr.review_comments)
        ci_pending = any(c.status in ("queued", "in_progress") for c in pr.ci_checks)
        has_conflict = self._has_merge_conflict(pr)
        has_blocking_issue = has_conflict or has_ci_failure or has_comments

        if pr.agent_failure_reason and has_blocking_issue:
            return PRStatus.AGENT_FAILED
        if has_conflict:
            return PRStatus.MERGE_CONFLICT

        if has_ci_failure and has_comments:
            return PRStatus.CI_AND_COMMENTS
        if has_ci_failure:
            return PRStatus.CI_FAILING
        if has_comments:
            return PRStatus.HAS_COMMENTS
        if ci_pending:
            return PRStatus.CI_PENDING
        return PRStatus.CLEAN

    def _pending_decision_for(self, pr: PRData):
        """The unresolved human decision gating ``pr``, if any. Never raises.

        Read on every status computation, so a coordinator/ledger problem must
        degrade to "no decision" rather than break the board.
        """
        try:
            return coordinator.pending_decision_for_pr(pr)
        except Exception:  # noqa: BLE001
            return None

    def _reserve_pr(self, pr_number: int) -> bool:
        if pr_number in self._inflight_prs:
            return False
        self._inflight_prs.add(pr_number)
        return True

    def _release_pr(self, pr_number: int) -> None:
        self._inflight_prs.discard(pr_number)

    async def dispatch_ci_fix(self, pr: PRData) -> None:
        """Auto-dispatch: queue unified PR maintenance for failing CI."""
        await self.dispatch_pr_maintenance(pr)

    async def dispatch_merge_conflict_fix(self, pr: PRData) -> None:
        """Auto-dispatch: queue unified PR maintenance for conflicts."""
        await self.dispatch_pr_maintenance(pr)

    async def dispatch_pr_maintenance(self, pr: PRData, guidance: str | None = None) -> None:
        """Queue PR maintenance for the local worktree agent."""
        # BOU-2040 (PR #109 review): the pending-decision gate lives HERE, in the
        # shared method, not only at the poll site. The dashboard's
        # /api/fix-comments and /api/retry-ci handlers reach this via
        # dispatch_comment_fix / dispatch_ci_fix without consulting
        # dispatch_decision_for_pr, so gating only the poll path would let a
        # button click queue an executor against an unresolved boundary.
        blocked = coordinator.decision_block_reason(pr)
        if blocked:
            pr.activity_message = blocked
            pr.activity_source = "agent-coordinator"
            self.log(blocked, pr_number=pr.number, level="warn")
            self._emit(
                "decision_wait",
                pr_number=pr.number,
                details={"reason": blocked, "source": "dispatch_pr_maintenance"},
            )
            return
        if not self._reserve_pr(pr.number):
            return
        try:
            if not pr.worktree_path:
                self.log(
                    f"No worktree for {pr.branch} — cannot maintain PR",
                    pr_number=pr.number,
                    level="warn",
                )
                return
            # Session precedence: never dispatch headless maintenance (nor claim)
            # for a PR whose worktree a LIVE in-session owner holds — its
            # .gaia/pr-watch.armed marker names a different session whose pid is
            # still alive. That in-session agent has full context and owns its PR;
            # the detached dashboard/loop only services UNOWNED worktrees. The
            # check is pid-robust (via markers) so a stale heartbeat — e.g. no
            # in-session waiter running — can't make a live session look dead and
            # let the dashboard grab the claim out from under it.
            from ._maintenance import markers  # noqa: PLC0415

            # Unioned with the claim side: from Stage 4 the marker is no longer
            # written, so a marker-only check degrades to "nobody owns this" and
            # the dashboard would dispatch into a worktree a live session is
            # editing — exactly what this gate exists to prevent. There is no
            # other guard on this path (BOU-2223 Stage 4).
            from ._maintenance.ownership_resolution import (  # noqa: PLC0415
                live_foreign_claim,
                ownership_unknown,
            )

            if markers._marker_live_foreign_pid(
                pr.worktree_path, DASHBOARD_OWNER_SESSION_ID
            ) or live_foreign_claim(
                pr.worktree_path,
                DASHBOARD_OWNER_SESSION_ID,
                kind="dashboard_dispatch_divergence",
            ):
                self.log(
                    f"Skipping headless maintenance for #{pr.number} — a live "
                    f"in-session owner holds the worktree",
                    pr_number=pr.number,
                )
                return
            # Fail closed (P1 adoption-theft defect): neither check above found
            # an owner, but that can mean "both sources agree unowned" (safe)
            # or "the claim store could not be read this tick" (NOT safe — we
            # simply don't know). Dispatching headless maintenance on the
            # latter risks editing a live session's worktree out from under it,
            # the exact failure this whole gate exists to prevent. See
            # ownership_resolution.ownership_unknown.
            if ownership_unknown(
                pr.worktree_path, kind="dashboard_dispatch_divergence"
            ):
                self.log(
                    f"Skipping headless maintenance for #{pr.number} — "
                    f"ownership state unresolvable (claim store unreadable, "
                    f"no local marker)",
                    pr_number=pr.number,
                )
                return
            blockers = maintenance.blockers_for_pr(pr)
            if not blockers:
                self.log(f"No PR maintenance blockers for #{pr.number}", pr_number=pr.number)
                return

            logs: dict[str, str] = {}
            if pr.failing_checks:
                # Fetch logs from the PR's own repo (its worktree) so multi-repo
                # PRs don't read the anchor repo's logs. Falls back to the anchor
                # cwd when no worktree is known.
                logs = await asyncio.to_thread(
                    github_api.get_failed_logs,
                    pr.latest_commit_sha,
                    pr.failing_checks,
                    pr.worktree_path or self.repo_cwd,
                )
            prompt = maintenance.build_maintenance_prompt(
                pr, failed_logs=logs, guidance=guidance
            )
            state = maintenance.queue_handoff(pr, prompt)
            pr.maintenance = maintenance.mark_state(state, MaintenanceStatus.QUEUED)
            pr.activity_message = "Delegated to local agent"
            pr.activity_source = "dashboard"
            pr.agent_failure_reason = None
            # Claim the PR in agent-coordinator so the next poll with unchanged
            # blockers sees an active claim and does NOT re-queue this same
            # maintenance every cycle (codex P1). The fingerprint is blocker-
            # derived, so when the blockers change the new work is unsuppressed;
            # the lease bounds a stale claim if the handed-off agent never runs.
            claim = coordinator.claim_pr(
                pr,
                session_id=DASHBOARD_OWNER_SESSION_ID,
                # BOU-2491: carry THIS `serve` process's own pid, not None.
                # `agent_coordinator.default_pid_is_live` treats pid=None as
                # unconditionally "live" — a claim with no pid can never be
                # pid-liveness-reaped, so a crashed dashboard's claim would
                # sit until lease expiry only, however long that is. The
                # dashboard process is the real liveness handle for its own
                # claim: once `serve` dies, this pid check reaps it promptly.
                pid=os.getpid(),
                agent=DASHBOARD_OWNER_SESSION_ID,
                lease_seconds=load_config(pr.worktree_path).lease_seconds,
                # Advisory: this claim suppresses re-queuing, it does NOT mean the
                # PR is being fixed. The dashboard has no executor (BOU-2491).
                # `dispatch_decision_for_pr` additionally reads `can_execute`
                # from this actor to make sure this claim never suppresses the
                # LOOP's real dispatch either.
                actor=MaintenanceActor.DASHBOARD_QUEUE,
            )
            pr.coordinator_claim = claim
            self.log(
                f"Queued PR maintenance for local agent: {', '.join(blockers)}",
                pr_number=pr.number,
            )
        finally:
            self._release_pr(pr.number)

    async def dispatch_comment_fix(self, pr_number: int, guidance: str | None = None) -> None:
        """Auto- or human-triggered: queue unified PR maintenance."""
        pr = self.get_pr(pr_number)
        if not pr:
            self.log(f"PR #{pr_number} not found", level="error")
            return
        if not pr.worktree_path:
            self.log(f"No worktree for {pr.branch}", pr_number=pr_number, level="warn")
            return
        if not pr.review_comments and not pr.failing_checks and not self._has_merge_conflict(pr):
            self.log(f"No PR maintenance blockers on PR #{pr_number}", pr_number=pr_number)
            return
        await self.dispatch_pr_maintenance(pr, guidance=guidance)
