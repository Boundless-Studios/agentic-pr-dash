"""Pydantic models for PR dashboard state."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel


class PRStatus(str, Enum):
    CLEAN = "clean"
    NO_PR = "no_pr"
    CI_FAILING = "ci_failing"
    CI_PENDING = "ci_pending"
    HAS_COMMENTS = "has_comments"
    CI_AND_COMMENTS = "ci_and_comments"
    MERGE_CONFLICT = "merge_conflict"
    AGENT_WORKING = "agent_working"
    AGENT_FAILED = "agent_failed"


class MaintenanceStatus(str, Enum):
    QUEUED = "queued"
    SIGNALED = "signaled"
    RUNNING = "running"
    WAITING_FOR_PUSH = "waiting_for_push"
    COMPLETE = "complete"
    STALE = "stale"
    FAILED = "failed"


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
    last_polled: datetime | None = None
    last_agent_dispatch: datetime | None = None
    maintenance: MaintenanceState | None = None
    cleanup_candidate: bool = False
    runtime_session_id: str | None = None
    agent_name: str | None = None
    docker_mode: str | None = None
    docker_daemon_name: str | None = None
    container_names: list[str] = []
    runtime_warnings: list[str] = []
    pr_created_at: str = ""

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
    def agent_state(self) -> str:
        """Single canonical state string for the card, in priority order:
        failed > working > queued > awaiting_fixes > ci_failing > ci_pending
        > merge_conflict > no_pr > clean.
        """
        # --- failed ---
        if (
            self.agent_failure_reason
            or self.status == PRStatus.AGENT_FAILED
            or (self.maintenance is not None and self.maintenance.state == MaintenanceStatus.FAILED)
        ):
            return "failed"

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
        if self.status == PRStatus.CI_PENDING:
            return "ci_pending"
        if self.status == PRStatus.MERGE_CONFLICT:
            return "merge_conflict"
        if self.status == PRStatus.NO_PR:
            return "no_pr"
        return "clean"

    @property
    def agent_state_label(self) -> str:
        """Human-readable label for agent_state."""
        return {
            "failed": "Failed",
            "working": "Agent Working",
            "queued": "Queued",
            "awaiting_fixes": "Awaiting Fixes",
            "ci_failing": "CI Failing",
            "ci_pending": "CI Pending",
            "merge_conflict": "Merge Conflict",
            "no_pr": "No PR",
            "clean": "Clean",
        }.get(self.agent_state, "Unknown")

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
