"""Shared data model for PR maintenance, dashboard cards, and runner state.

GitHub responses, worktree metadata, ownership markers, maintenance handoff
state, CI checks, and runner queues all converge here before being rendered or
passed between subsystems. Keep external parsing out of this file; models should
describe already-normalized state and expose small derived labels/properties.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class PRStatus(str, Enum):
    CLEAN = "clean"
    OBSERVATION_UNAVAILABLE = "observation_unavailable"
    NO_PR = "no_pr"
    CI_FAILING = "ci_failing"
    CI_PENDING = "ci_pending"
    HAS_COMMENTS = "has_comments"
    CI_AND_COMMENTS = "ci_and_comments"
    MERGE_CONFLICT = "merge_conflict"
    AGENT_WORKING = "agent_working"
    # A live session that is not actually working — awaiting user input, an
    # external check, or winding down. Liveness alone never means AGENT_WORKING
    # (BOU-2365).
    AGENT_WAITING = "agent_waiting"
    # Blocked on a human answering an architecture/product decision (BOU-2040).
    # Distinct from AGENT_WAITING: nothing is running and nothing will run until
    # a person acts, so this is the one waiting state that is actionable BY the
    # viewer rather than by the agent.
    WAITING_HUMAN_DECISION = "waiting_human_decision"
    # The deliverable is merged/closed and the worktree is reclaimable, even if
    # the chat process is still alive.
    READY_CLEANUP = "ready_cleanup"
    AGENT_FAILED = "agent_failed"


class MaintenanceStatus(str, Enum):
    QUEUED = "queued"
    SIGNALED = "signaled"
    RUNNING = "running"
    WAITING_FOR_PUSH = "waiting_for_push"
    COMPLETE = "complete"
    STALE = "stale"
    FAILED = "failed"


class MaintenanceActor(str, Enum):
    """Who acted on a PR — and, via :data:`EXECUTING_ACTORS`, with what authority.

    Five surfaces in this package are all called "PR maintenance", but they differ
    on the one axis that matters to a reader trying to explain a commit they did
    not write: *can this component write code and push?* Before BOU-2490 that axis
    was represented nowhere — the dashboard's "queued a work order" and the loop's
    "ran ``codex --full-auto`` and pushed" both emitted ``kind="dispatch"`` with a
    null ``session_id``, under the same log prefix, against the same ownership
    ledger. A session that found unexplained commits had to guess, and guessed
    that a daemon had taken over its work.

    This enum is the single definition of that vocabulary. Everything downstream
    (event log, ownership claim metadata, log prefixes) reads it rather than
    re-deriving capability from an ad-hoc string.
    """

    #: Blocks the session's Stop and asks *it* to fix. Writes nothing itself.
    STOP_GATE = "stop-gate"
    #: The in-session agent (``/pr-maintenance-check``). Writes code.
    SESSION = "session"
    #: Detached per-session feedback waiter. Wakes the session; writes nothing.
    WAITER = "waiter"
    #: Dashboard poll/button. Queues a handoff and claims the PR; writes no code.
    DASHBOARD_QUEUE = "dashboard-queue"
    #: The detached maintenance loop. Runs the configured executor: writes code,
    #: commits, merges main, pushes.
    LOOP_EXECUTOR = "loop-executor"


#: Actors that can produce commits. Everything else is advisory, and an advisory
#: owner must never be mistaken for coverage (see BOU-2491).
EXECUTING_ACTORS = frozenset({MaintenanceActor.SESSION, MaintenanceActor.LOOP_EXECUTOR})


def can_execute(actor: "MaintenanceActor | str | None") -> bool:
    """True when ``actor`` is capable of writing code and pushing.

    Accepts the raw string form too, since claim metadata round-trips through
    ``dict[str, str]`` and event rows through JSON. An unknown or missing actor
    is treated as **non-executing**: the conservative answer, because the caller
    asking this question is deciding whether someone else has the PR covered, and
    wrongly assuming coverage strands a red PR (BOU-1789).
    """
    if actor is None:
        return False
    try:
        return MaintenanceActor(actor) in EXECUTING_ACTORS
    except ValueError:
        return False


class ClaimHandle(BaseModel):
    """Fenced coordinator ownership required for every claim mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str = Field(min_length=1)
    lease_epoch: int = Field(ge=0)


class CICheck(BaseModel):
    name: str
    status: str  # queued, in_progress, completed
    conclusion: str | None = None  # success, failure, cancelled, timed_out


class QueuedWorkflowJob(BaseModel):
    name: str
    status: str
    labels: list[str] = []
    queued_at: str | None = None
    queue_seconds: int | None = None
    runner_pool: str = "unknown"
    matching_online_runner_count: int | None = None
    warning: str | None = None

    @property
    def queue_age_label(self) -> str:
        if self.queue_seconds is None:
            return "unknown"
        minutes = max(0, self.queue_seconds) // 60
        seconds = max(0, self.queue_seconds) % 60
        if minutes >= 60:
            hours = minutes // 60
            rem = minutes % 60
            return f"{hours}h {rem}m"
        if minutes:
            return f"{minutes}m {seconds:02d}s"
        return f"{seconds}s"


class RunnerPoolHealth(BaseModel):
    pool: str
    total_count: int = 0
    online_count: int = 0
    busy_count: int = 0


class RunnerExecutionSummary(BaseModel):
    desktop_count: int = 0
    github_hosted_count: int = 0
    unknown_count: int = 0
    # Wall-clock job runtime (completed_at - started_at) per pool, in seconds.
    # Counts over-weight trivial/skipped hosted jobs; time shows where compute
    # actually goes. Pre-#1714 cache files lack these keys and are rejected on
    # load (see github_api.load_runner_execution_summary_cache) rather than
    # rendered as a misleading "0m".
    desktop_seconds: float = 0.0
    github_hosted_seconds: float = 0.0
    unknown_seconds: float = 0.0

    @property
    def total_count(self) -> int:
        return self.desktop_count + self.github_hosted_count + self.unknown_count

    @property
    def total_seconds(self) -> float:
        return self.desktop_seconds + self.github_hosted_seconds + self.unknown_seconds

    @property
    def local_count(self) -> int:
        return self.desktop_count

    @property
    def remote_count(self) -> int:
        return self.github_hosted_count

    @property
    def desktop_percent(self) -> int:
        if self.total_count == 0:
            return 0
        return round((self.desktop_count / self.total_count) * 100)

    @property
    def github_hosted_percent(self) -> int:
        if self.total_count == 0:
            return 0
        return round((self.github_hosted_count / self.total_count) * 100)

    @property
    def local_percent(self) -> int:
        return self.desktop_percent

    @property
    def remote_percent(self) -> int:
        return self.github_hosted_percent


class ReviewComment(BaseModel):
    id: int
    author: str
    body: str
    path: str | None = None
    line: int | None = None
    created_at: str
    is_inline: bool = True
    thread_id: str | None = None


class ThreadDecision(BaseModel):
    thread_id: str | None = None
    author: str
    created_at: str
    age_seconds: float | None = None
    decision: str  # one of: PICKED, SKIP_RESOLVED, SKIP_DEFERRED, SKIP_OUTDATED, SKIP_ADDRESSED, SKIP_CLAIMED_ACTIVE, SKIP_DATE_FILTER, SKIP_HUMAN_RESOLVED
    marker_state: str | None = None
    claim_age_seconds: float | None = None


class MaintenanceState(BaseModel):
    pr_number: int
    branch: str
    worktree_path: str
    state: MaintenanceStatus = MaintenanceStatus.QUEUED
    blockers: list[str] = []
    failing_checks: list[str] = []
    review_comment_ids: list[int] = []
    bead_id: str | None = None
    last_signal_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_progress_at: datetime | None = None
    failure_reason: str | None = None
    output_tail: list[str] = []


class PRData(BaseModel):
    number: int
    author: str = ""
    # ``owner/name`` of the GitHub repo this PR belongs to. The dashboard can
    # aggregate PRs across multiple repos (anchor + ``maintenance_repo_roots``),
    # so the PR number alone is not unique — same-number PRs in two repos must
    # not collide. Empty string for the legacy single-repo path where the repo
    # is implicit. Derived from the PR url (``github.com/<owner>/<name>/pull/N``).
    repo: str = ""
    title: str
    branch: str
    base_branch: str = "main"
    url: str
    is_draft: bool = False
    created_at: str = ""
    labels: list[str] = []
    status: PRStatus = PRStatus.CLEAN
    ci_checks: list[CICheck] = []
    queued_jobs: list[QueuedWorkflowJob] = []
    runner_pool_health: list[RunnerPoolHealth] = []
    runner_execution_summary: RunnerExecutionSummary = RunnerExecutionSummary()
    failing_checks: list[str] = []
    review_comments: list[ReviewComment] = []
    merge_state: str = "unknown"
    mergeable: str = "unknown"
    review_decision: str = "none"
    latest_commit_sha: str = ""
    latest_commit_date: str = ""
    worktree_path: str | None = None
    agent_session_id: str | None = None
    agent_cli_name: str | None = None
    activity_message: str | None = None
    activity_source: str | None = None
    agent_failure_reason: str | None = None
    no_push_comment_retry_count: int = 0
    last_seen_comment_ids: set[int] = set()
    agent_output: list[str] = []
    last_polled: datetime | None = None
    last_agent_dispatch: datetime | None = None
    maintenance: MaintenanceState | None = None
    # Fenced agent-coordinator claim held after dashboard handoff. Both values
    # must survive until release; a bare claim id cannot reject stale owners.
    coordinator_claim: ClaimHandle | None = None
    # True when at least one required CI check is still queued/in_progress.
    # Set by _check_worktree for non-draft PRs via github_api.required_checks_pending.
    ci_watch_pending: bool = False
    # The unresolved human decision gating this PR, if any (BOU-2040/BOU-2402).
    # Carried on the PR so a viewer can see WHAT is being asked without going to
    # the coordinator ledger. Populated by _check_worktree; the ledger stays the
    # source of truth.
    waiting_decision_id: str | None = None
    waiting_decision_question: str | None = None
    waiting_decision_category: str | None = None
    waiting_decision_runtime: str | None = None
    # True when this PR has been escalated due to repeated executor failures.
    escalated: bool = False
    escalated_reason: str | None = None


class AgentProcess(BaseModel):
    pid: int
    cli_name: str
    label: str
    command: str = ""


class WorktreeCard(BaseModel):
    id: str
    worktree_name: str
    worktree_path: str | None = None
    worktree_hidden: bool = False
    branch: str
    environment_name: str | None = None
    backend_port: str | None = None
    frontend_port: str | None = None
    slot: str | None = None
    pr_number: int | None = None
    pr_title: str | None = None
    pr_url: str | None = None
    is_draft: bool = False
    status: PRStatus = PRStatus.CLEAN
    ci_checks: list[CICheck] = []
    queued_jobs: list[QueuedWorkflowJob] = []
    runner_pool_health: list[RunnerPoolHealth] = []
    runner_execution_summary: RunnerExecutionSummary = RunnerExecutionSummary()
    failing_checks: list[str] = []
    review_comments: list[ReviewComment] = []
    merge_state: str = "unknown"
    review_decision: str = "none"
    latest_commit_sha: str = ""
    latest_commit_date: str = ""
    last_updated_label: str | None = None
    active_agents: list[AgentProcess] = []
    activity_message: str | None = None
    activity_source: str | None = None
    agent_failure_reason: str | None = None
    agent_session_id: str | None = None
    agent_output: list[str] = []
    # Tri-state session activity: "working", "waiting", or "none". Kept
    # separate from `status` so the column stays driven by what the PR needs
    # while the state chip still tells the truth about the session (BOU-2365).
    session_activity: str = "none"
    # Why a live session is idle, set alongside session_activity == "waiting":
    # "user input", "external checks", or "winding down".
    waiting_reason: str | None = None
    # The unresolved human decision gating this card (BOU-2402). Projected from
    # PRData so the template can render the actual question — without these the
    # viewer sees a "Needs Your Decision" chip and has no way to learn what is
    # being asked short of reading the coordinator ledger.
    waiting_decision_id: str | None = None
    waiting_decision_question: str | None = None
    waiting_decision_category: str | None = None
    waiting_decision_runtime: str | None = None
    last_polled: datetime | None = None
    last_agent_dispatch: datetime | None = None
    maintenance: MaintenanceState | None = None
    cleanup_candidate: bool = False
    escalated: bool = False
    escalated_reason: str | None = None
    runtime_session_id: str | None = None
    runtime_chain_id: str | None = None
    runtime_generation: int | None = None
    supervisor_state: str | None = None
    context_percent: float | None = None
    context_tokens: int | None = None
    window_tokens: int | None = None
    cumulative_tokens: int | None = None
    context_confidence: str | None = None
    runtime_quiescence: str | None = None
    runtime_active_turns: int = 0
    runtime_active_tools: int = 0
    runtime_active_subagents: int = 0
    runtime_active_critical_sections: int = 0
    runtime_checkpoint_fingerprint: str | None = None
    runtime_outbox_depth: int = 0
    runtime_status_stale: bool = False
    agent_name: str | None = None
    docker_mode: str | None = None
    docker_daemon_name: str | None = None
    container_names: list[str] = []
    runtime_warnings: list[str] = []
    pr_created_at: str = ""
    # Ownership / observability fields (populated best-effort by _ownership_for_card)
    owner_session_id: str | None = None
    owner_pid: int | None = None
    owner_pid_alive: bool | None = None
    armed_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    loop_state: str | None = None
    thread_decisions: list[ThreadDecision] = Field(default_factory=list)

    @property
    def started_at(self) -> datetime | None:
        """Parse pr_created_at (GitHub ISO8601) into a UTC datetime, or None."""
        if not self.pr_created_at:
            return None
        try:
            return datetime.fromisoformat(self.pr_created_at.replace("Z", "+00:00"))
        except ValueError:
            return None

    @property
    def started_at_label(self) -> str:
        """Relative started_at, e.g. '1d 5h ago', or '' when unset."""
        dt = self.started_at
        if dt is None:
            return ""
        return humanize_relative(dt)

    @property
    def supervisor_state_label(self) -> str:
        return (self.supervisor_state or "").replace("_", " ").title()

    @property
    def context_usage_label(self) -> str | None:
        values: list[str] = []
        if self.context_tokens is not None and self.window_tokens is not None:
            values.append(f"{self.context_tokens:,} / {self.window_tokens:,}")
        if self.context_percent is not None:
            values.append(f"{self.context_percent:.1f}%")
        return f"Context {' · '.join(values)}" if values else None

    @property
    def cumulative_usage_label(self) -> str | None:
        if self.cumulative_tokens is None:
            return None
        return f"Cumulative {self.cumulative_tokens:,} tokens"

    @property
    def runtime_activity_label(self) -> str | None:
        if not self.runtime_quiescence and not any(
            (
                self.runtime_active_turns,
                self.runtime_active_tools,
                self.runtime_active_subagents,
                self.runtime_active_critical_sections,
            )
        ):
            return None
        turn = "turn" if self.runtime_active_turns == 1 else "turns"
        tool = "tool" if self.runtime_active_tools == 1 else "tools"
        subagent = (
            "subagent" if self.runtime_active_subagents == 1 else "subagents"
        )
        return (
            f"{self.runtime_active_turns} {turn} · "
            f"{self.runtime_active_tools} {tool} · "
            f"{self.runtime_active_subagents} {subagent} · "
            f"{self.runtime_active_critical_sections} critical"
        )

    @property
    def agent_state(self) -> str:
        """Single canonical state string for the card, in priority order:

        ``failed > ready_cleanup > queued > working > awaiting_fixes >
        ci_failing > merge_conflict > waiting > ci_pending > no_pr > clean``

        The three activity states are deliberately distinct (BOU-2365):

        * ``working`` — the agent is coding, testing, or remediating feedback.
        * ``waiting`` — a live session that is idle: awaiting user input, an
          external check, or winding down. Process/heartbeat liveness alone
          resolves here, never to ``working``. It outranks ``ci_pending`` and
          ``clean`` so an idle session is always visible, but never outranks an
          actionable PR state — a red PR still reads ``ci_failing``.
        * ``ready_cleanup`` — the PR is merged/closed and the worktree is
          reclaimable, even when the chat process is still alive. It outranks
          ``waiting`` so a lingering conversation cannot hide a finished
          worktree, but not ``working`` — an agent genuinely mid-turn on a
          stale branch still reads as working.
        """
        # --- failed ---
        if (
            self.agent_failure_reason
            or self.status == PRStatus.AGENT_FAILED
            or (self.maintenance is not None and self.maintenance.state == MaintenanceStatus.FAILED)
        ):
            return "failed"

        # --- terminal: the deliverable is done, the worktree is reclaimable ---
        if self.status == PRStatus.READY_CLEANUP:
            return "ready_cleanup"

        # --- blocked on a human answer ---
        # Above the maintenance signals on purpose (BOU-2402, PR #110 review):
        # a stale QUEUED/RUNNING maintenance record must not paint this card
        # "working" when nothing is running and nothing will run until a person
        # answers. Without this case the status fell through every branch below
        # and returned "clean" — a Clean chip on a blocked PR, which is worse
        # than the invisibility this was meant to fix.
        if self.status == PRStatus.WAITING_HUMAN_DECISION:
            return "needs_decision"

        # --- maintenance signals override status-based states ---
        if self.maintenance is not None:
            m = self.maintenance.state
            if m in (MaintenanceStatus.QUEUED, MaintenanceStatus.SIGNALED):
                return "queued"
            if m in (MaintenanceStatus.RUNNING, MaintenanceStatus.WAITING_FOR_PUSH):
                return "working"

        # --- status-based ---
        if self.status == PRStatus.AGENT_WORKING:
            return "working"
        if self.status in (PRStatus.HAS_COMMENTS, PRStatus.CI_AND_COMMENTS):
            return "awaiting_fixes"
        if self.status == PRStatus.CI_FAILING:
            return "ci_failing"
        if self.status == PRStatus.MERGE_CONFLICT:
            return "merge_conflict"
        if self.status == PRStatus.OBSERVATION_UNAVAILABLE:
            return "observation_unavailable"

        # --- an idle live session outranks the passive PR states ---
        if self.status == PRStatus.AGENT_WAITING or self.session_activity == "waiting":
            return "waiting"

        if self.status == PRStatus.CI_PENDING:
            return "ci_pending"
        if self.status == PRStatus.NO_PR:
            return "no_pr"
        return "clean"

    @property
    def agent_state_label(self) -> str:
        """Human-readable label for agent_state."""
        return {
            "failed": "Failed",
            "needs_decision": "Needs Your Decision",
            "working": "Agent Working",
            "waiting": "Waiting",
            "ready_cleanup": "Ready / Cleanup",
            "queued": "Queued",
            "awaiting_fixes": "Awaiting Fixes",
            "ci_failing": "CI Failing",
            "ci_pending": "CI Pending",
            "merge_conflict": "Merge Conflict",
            "observation_unavailable": "GitHub Unavailable",
            "no_pr": "No PR",
            "clean": "Clean",
        }.get(self.agent_state, "Unknown")

    @property
    def state_chip_label(self) -> str:
        """Text for the card's single state badge.

        ``agent_state_label`` for every state, extended to
        ``Waiting · user input`` when a waiting reason is known. It always
        starts with ``agent_state_label`` so the single-badge card contract
        still renders the canonical label.
        """
        label = self.agent_state_label
        if self.agent_state == "waiting" and self.waiting_reason:
            return f"{label} · {self.waiting_reason}"
        return label

    @property
    def runner_issue_count(self) -> int:
        return sum(1 for job in self.queued_jobs if job.warning)

    @property
    def runner_indicator_label(self) -> str | None:
        if self.queued_jobs:
            matching_counts = [
                job.matching_online_runner_count
                for job in self.queued_jobs
                if job.matching_online_runner_count is not None
            ]
            if matching_counts:
                return f"Runner: {len(self.queued_jobs)} queued · {min(matching_counts)} match"
            return f"Runner: {len(self.queued_jobs)} queued"
        if self.runner_execution_summary.total_count:
            return (
                f"Runner: {self.runner_execution_summary.desktop_count} desktop · "
                f"{self.runner_execution_summary.github_hosted_count} GitHub"
            )
        return None

    @property
    def runner_indicator_status(self) -> str:
        if self.runner_issue_count:
            return "warning"
        if self.queued_jobs:
            return "pending"
        return "ok"

    @property
    def search_text(self) -> str:
        # Identity/state fields only. The live filter (static/app.js) matches
        # this attribute PLUS the card's textContent — which already includes
        # the collapsed <details> diagnostics — so diagnostic noise (comment
        # bodies, session ids, output tails) must NOT be duplicated here or it
        # leaks into the DOM outside the details region (BOU-1551).
        parts: list[object] = [
            self.id,
            self.worktree_name,
            self.worktree_path,
            self.branch,
            self.environment_name,
            self.backend_port,
            self.frontend_port,
            self.slot,
            self.pr_number,
            self.pr_title,
            self.pr_url,
            self.status.value,
            self.status.value.replace("_", " "),
            self.agent_name,
            self.agent_state,
            self.agent_state_label,
            # "none" is not a searchable fact — it would match any query for it.
            self.session_activity if self.session_activity != "none" else None,
            self.waiting_reason,
            self.started_at_label,
            self.merge_state,
            self.review_decision,
            self.latest_commit_sha,
            self.last_updated_label,
            self.docker_mode,
            self.docker_daemon_name,
        ]
        if self.is_draft:
            parts.append("draft")
        if self.worktree_hidden:
            parts.append("agent worktree hidden")
        if self.cleanup_candidate:
            parts.append("cleanup candidate")

        return " ".join(str(part) for part in parts if part not in (None, ""))


class EventEntry(BaseModel):
    timestamp: datetime
    pr_number: int | None = None
    message: str
    level: str = "info"  # info, warn, error, success


def worktree_started_at(path: str) -> datetime | None:
    """Return the creation time of a worktree directory as a UTC datetime.

    Prefers st_birthtime (macOS/BSD) when available; falls back to st_ctime.
    Returns None when the path is missing or stat raises.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    try:
        # macOS / BSD: st_birthtime is the true creation time.
        birth = getattr(stat, "st_birthtime", None)
        if birth:
            return datetime.fromtimestamp(birth, tz=timezone.utc)
    except (AttributeError, OSError, ValueError):
        pass
    try:
        return datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc)
    except (OSError, ValueError):
        return None


def humanize_relative(dt: datetime, now: datetime | None = None) -> str:
    """Compact relative age of ``dt``, e.g. '1d 5h ago', '1m 30s ago', '12s ago'.

    Shows the two largest non-zero units (day+hour, hour+minute, minute+second)
    so the label stays scannable. Future or sub-second deltas render 'just now'.
    A naive ``dt`` is assumed to be UTC. Pass ``now`` for deterministic tests.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    seconds = int((reference - dt).total_seconds())
    if seconds < 1:
        return "just now"

    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    if days:
        return f"{days}d {hours}h ago"
    if hours:
        return f"{hours}h {minutes}m ago"
    if minutes:
        return f"{minutes}m {secs}s ago"
    return f"{secs}s ago"
