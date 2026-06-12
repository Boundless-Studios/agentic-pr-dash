"""Orchestrator — PR state machine + queue-only dispatch.

Polls GitHub every 60s, updates PR state, and queues maintenance work for
CI failures, merge conflicts, and unaddressed review comments (bot + human).
Work is delegated to the local worktree agent; the dashboard never spawns
a standalone background subprocess.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from . import agents, coordinator, github_api, maintenance, session_registry
from .config import load as load_config
from .models import CICheck, EventEntry, MaintenanceStatus, PRData, PRStatus, RunnerExecutionSummary
from .worktrees import discover_worktrees, find_worktree_for_branch

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
DASHBOARD_OWNER_SESSION_ID = "agentic-pr-dash-dashboard"
# Each poll enriches every open PR with several REST + GraphQL calls. At 15s
# this comfortably exceeded GitHub's hourly API limit for even a handful of
# PRs, starving the API to zero and causing refresh failures. 60s keeps the
# steady-state burn well under budget while staying responsive enough for a
# PR babysitter.
POLL_INTERVAL_SECONDS = 60


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
    def __init__(self, repo_cwd: str | None = None):
        self.repo_cwd = repo_cwd
        self.prs: dict[int, PRData] = {}
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

    def start(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())
            self.log("Orchestrator started — polling every 15s")

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.refresh_prs()
            except Exception as exc:
                self.log(f"Poll error: {exc}", level="error")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def refresh_prs(self) -> list[PRData]:
        """Fetch all open PRs and update state."""
        raw_prs = await asyncio.to_thread(github_api.list_open_prs, self.repo_cwd)
        now = datetime.now(timezone.utc)

        # A None result means the GitHub API call failed (rate-limited or
        # unreachable) rather than "no open PRs". Skip the whole cycle so a
        # transient failure can neither prune every tracked PR nor, by
        # aborting later mid-enrichment, leave merged PRs pinned on the board.
        if raw_prs is None:
            self.log(
                "Skipping refresh: could not list open PRs (GitHub API unavailable)",
                level="error",
            )
            return list(self.prs.values())

        # Prune merged/closed PRs FIRST, straight from the cheap open-PR list,
        # so a failure in the per-PR enrichment below can never leave a merged
        # PR pinned — the bug this guards against. (Previously the prune was
        # the last step and was skipped whenever enrichment raised.)
        open_numbers = {
            raw["number"] for raw in raw_prs if isinstance(raw.get("number"), int)
        }
        for num in list(self.prs.keys()):
            if num not in open_numbers:
                old = self.prs.pop(num)
                self.log(
                    f"PR #{num} closed/merged: {old.title}",
                    pr_number=num,
                    level="success",
                )

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

        for raw in raw_prs:
            num = raw.get("number")
            if not isinstance(num, int):
                continue

            # Get or create PR data
            pr = self.prs.get(num)
            if pr is None:
                pr = PRData(
                    number=num,
                    title=raw.get("title", ""),
                    branch=raw.get("headRefName", ""),
                    base_branch=raw.get("baseRefName", "") or "main",
                    url=raw.get("url", ""),
                    is_draft=raw.get("isDraft", False),
                    created_at=raw.get("createdAt", ""),
                )
                self.prs[num] = pr
                self.log(f"Discovered PR #{num}: {pr.title}", pr_number=num)

            # Update metadata
            pr.title = raw.get("title", pr.title)
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
            if bulk_merge_state and bulk_merge_state != "UNKNOWN":
                pr.merge_state = bulk_merge_state
            if bulk_mergeable and bulk_mergeable != "UNKNOWN":
                pr.mergeable = bulk_mergeable

            # Find worktree
            pr.worktree_path = find_worktree_for_branch(pr.branch)

            if bulk_merge_state in ("", "UNKNOWN") or bulk_mergeable in ("", "UNKNOWN"):
                refetched_state, refetched_mergeable = await asyncio.to_thread(
                    github_api.get_mergeability, num, self.repo_cwd
                )
                # A successful refetch is authoritative — it also picks up a
                # conflict that has since been resolved. A failed refetch returns
                # ("","") and leaves the preserved last-known value intact.
                if refetched_state:
                    pr.merge_state = refetched_state
                if refetched_mergeable:
                    pr.mergeable = refetched_mergeable

            # Get latest commit info
            previous_commit_sha = pr.latest_commit_sha
            sha, date = await asyncio.to_thread(
                github_api.get_latest_commit, num, self.repo_cwd
            )
            pr.latest_commit_sha = sha
            pr.latest_commit_date = date
            if previous_commit_sha and sha and sha != previous_commit_sha:
                pr.no_push_comment_retry_count = 0

            # Get CI checks
            checks = await asyncio.to_thread(
                github_api.get_ci_checks, num, self.repo_cwd
            )
            pr.ci_checks = checks
            if any(c.status in {"queued", "in_progress"} for c in checks):
                queued_jobs, runner_pool_health, runner_execution_summary = await asyncio.to_thread(
                    github_api.get_workflow_queue_health, num, self.repo_cwd
                )
                pr.queued_jobs = queued_jobs
                pr.runner_pool_health = runner_pool_health
                pr.runner_execution_summary = runner_execution_summary
            else:
                pr.queued_jobs = []
                pr.runner_pool_health = []
                pr.runner_execution_summary = pr.runner_execution_summary.__class__()

            # Compute failing checks (code only, not infra)
            pr.failing_checks = [
                c.name for c in checks
                if c.conclusion == "failure" and not github_api._is_infra_check(c.name)
            ]

            # Get unaddressed comments (filtered by commit date!)
            comments = await asyncio.to_thread(
                github_api.get_unaddressed_comments, num, date, self.repo_cwd
            )
            pr.review_comments = comments

            if not pr.review_comments:
                pr.no_push_comment_retry_count = 0

            # New unaddressed comment IDs unblock a stuck AGENT_FAILED PR.
            # Otherwise agent_failure_reason only clears on a fresh commit,
            # which means fresh review feedback can't re-trigger auto-dispatch.
            current_comment_ids = {c.id for c in comments}
            new_comment_ids = current_comment_ids - pr.last_seen_comment_ids
            if new_comment_ids and num not in self._inflight_prs:
                if pr.agent_failure_reason:
                    self.log(
                        f"New unaddressed comment(s) on PR #{num} — clearing stuck failure state",
                        pr_number=num,
                    )
                    pr.agent_failure_reason = None
                pr.no_push_comment_retry_count = 0
            pr.last_seen_comment_ids = current_comment_ids

            # Compute status
            pr.status = self._compute_status(pr)
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
                if pr.coordinator_claim_id:
                    try:
                        coordinator.release_claim_id(
                            pr.coordinator_claim_id, DASHBOARD_OWNER_SESSION_ID, "completed"
                        )
                    except Exception as exc:  # best-effort; lease bounds it anyway
                        self.log(
                            f"Could not release coordinator claim for #{num}: {exc}",
                            pr_number=num,
                            level="warn",
                        )
                    pr.coordinator_claim_id = None
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

        return list(self.prs.values())

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
            blockers = maintenance.blockers_for_pr(pr)
            if not blockers:
                self.log(f"No PR maintenance blockers for #{pr.number}", pr_number=pr.number)
                return

            logs: dict[str, str] = {}
            if pr.failing_checks:
                logs = await asyncio.to_thread(
                    github_api.get_failed_logs,
                    pr.latest_commit_sha,
                    pr.failing_checks,
                    self.repo_cwd,
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
                pid=None,
                agent="agentic-pr-dash-dashboard",
                lease_seconds=load_config(pr.worktree_path).lease_seconds,
            )
            pr.coordinator_claim_id = claim.claim_id if claim else None
            self.log(
                f"Queued PR maintenance for local agent: {', '.join(blockers)}",
                pr_number=pr.number,
            )
        finally:
            self._release_pr(pr.number)

    async def dispatch_comment_fix(self, pr_number: int, guidance: str | None = None) -> None:
        """Auto- or human-triggered: queue unified PR maintenance."""
        pr = self.prs.get(pr_number)
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
