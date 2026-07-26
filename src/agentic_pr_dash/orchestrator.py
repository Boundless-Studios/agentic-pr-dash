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
from .maintenance_check import _resolve_maintenance_roots
from .models import CICheck, EventEntry, MaintenanceStatus, PRData, PRStatus, RunnerExecutionSummary
from .observability import ObservabilityEvent, get_event_store
from .worktrees import discover_worktrees, find_worktree_for_branch

# Composite key for the tracked-PR map. The dashboard aggregates PRs across any
# number of repos (anchor + ``maintenance_repo_roots``), so the PR number alone
# is not unique — same-number PRs in two repos must be distinct entries.
PRKey = tuple[str, int]


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

# Per-PR enrichment (mergeability / latest commit / CI checks / queue health /
# unaddressed comments) is independent across PRs, so each poll enriches them
# concurrently (BOU-1637 #4) instead of awaiting one PR's gh calls before the
# next. The semaphore bounds in-flight enrichments so a large PR pool doesn't
# burst the gh API into a rate-limit wall.
ENRICHMENT_CONCURRENCY = 6


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
        # Keyed by ``(repo, number)`` so PRs aggregated from multiple repos
        # (anchor + ``maintenance_repo_roots``) never collide on PR number.
        self.prs: dict[PRKey, PRData] = {}
        # Which repo-root discovered each tracked PR, so a per-root poll prunes
        # only its OWN merged/closed PRs (even when that root returns zero PRs)
        # and never drops another repo's PRs on a transient failure.
        self._pr_root: dict[PRKey, str | None] = {}
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
    ) -> None:
        """Best-effort observability event emission. Never raises."""
        try:
            cwd = repo if repo is not None else self.repo_cwd
            event = ObservabilityEvent(
                ts=datetime.now(timezone.utc),
                repo=cwd,
                pr_number=pr_number,
                kind=kind,
                session_id=None,
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
        """Fetch all open PRs across every configured repo and update state.

        The dashboard covers ``[anchor] + maintenance_repo_roots`` (BOU-1598):
        the same root list the maintenance loop honors. Each root is polled with
        that root as the ``github_api.*`` cwd, sequentially, with per-repo error
        isolation — a single repo's poll failing (gh error or exception) must not
        drop another repo's PRs. With no extra roots configured this reduces to
        exactly today's single-repo behavior.
        """
        now = datetime.now(timezone.utc)

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

        for root in self._repo_roots():
            try:
                await self._refresh_repo(root, now)
            except Exception as exc:
                # Per-repo isolation: one bad repo must not sink the dashboard.
                self.log(f"Poll error for repo root {root}: {exc}", level="error")

        return list(self.prs.values())

    async def _refresh_repo(self, root: str | None, now: datetime) -> None:
        """Poll and enrich every open PR for a single repo root.

        ``root`` is the cwd passed to every ``github_api.*`` call so the poll is
        scoped to that repo. Tracked PRs are pruned per-root: only PRs this root
        discovered are eligible to be pruned, so another repo's PRs (or a repo
        whose poll just failed) are never dropped.
        """
        raw_prs = await asyncio.to_thread(github_api.list_open_prs, root)

        # A None result means the GitHub API call failed (rate-limited or
        # unreachable) rather than "no open PRs". Skip THIS repo's cycle so a
        # transient failure can neither prune its tracked PRs nor, by aborting
        # mid-enrichment, leave merged PRs pinned on the board. Other repos are
        # untouched (per-repo isolation).
        if raw_prs is None:
            # Name the failure class (auth vs network vs rate-limit) instead of
            # the bare "unavailable" — during BOU-1987 the undifferentiated
            # message hid an expired-token 401 behind a network-looking outage.
            failure = github_api.last_list_open_prs_failure()
            detail = f" — {failure.summary()}" if failure else ""
            self.log(
                f"Skipping refresh for {root}: could not list open PRs "
                f"(GitHub API unavailable{detail})",
                level="error",
            )
            return

        # Prune merged/closed PRs FIRST, straight from the cheap open-PR list,
        # so a failure in the per-PR enrichment below can never leave a merged
        # PR pinned. Scope the prune to PRs THIS root discovered so a sibling
        # repo's PRs are never collaterally dropped.
        open_numbers = {
            raw["number"] for raw in raw_prs if isinstance(raw.get("number"), int)
        }
        for key in list(self.prs.keys()):
            if self._pr_root.get(key) != root:
                continue
            if key[1] not in open_numbers:
                old = self.prs.pop(key)
                self._pr_root.pop(key, None)
                self.log(
                    f"PR #{key[1]} closed/merged: {old.title}",
                    pr_number=key[1],
                    level="success",
                )

        # Get-or-create every PR object up front (cheap, mutates the shared
        # ``self.prs`` map serially to avoid races), then enrich the independent
        # per-PR REST/GraphQL work CONCURRENTLY below (BOU-1637 #4). Each PR's
        # enrichment only touches its own ``PRData``, so gathering them is safe.
        to_enrich: list[tuple[PRData, dict]] = []
        for raw in raw_prs:
            num = raw.get("number")
            if not isinstance(num, int):
                continue

            repo = _repo_from_url(raw.get("url", ""))
            key: PRKey = (repo, num)

            # Get or create PR data
            pr = self.prs.get(key)
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
            to_enrich.append((pr, raw))

        # Enrich each PR concurrently, bounded by a modest semaphore so a large
        # pool doesn't hammer the gh API into a rate-limit wall. Per-PR error
        # isolation: one PR's failed enrichment must not drop the others.
        sem = asyncio.Semaphore(ENRICHMENT_CONCURRENCY)

        async def _guarded(pr: PRData, raw: dict) -> None:
            async with sem:
                await self._enrich_pr(pr, raw, root, now)

        results = await asyncio.gather(
            *(_guarded(pr, raw) for pr, raw in to_enrich),
            return_exceptions=True,
        )
        for (pr, _raw), result in zip(to_enrich, results):
            if isinstance(result, Exception):
                self.log(
                    f"Enrichment failed for PR #{pr.number}: {result}",
                    pr_number=pr.number,
                    level="error",
                )

    async def _enrich_pr(
        self, pr: PRData, raw: dict, root: str | None, now: datetime
    ) -> None:
        """Enrich a single PR with its per-PR REST/GraphQL state.

        Only ``pr``'s own fields are mutated here, so multiple ``_enrich_pr``
        calls run concurrently without sharing state (BOU-1637 #4). ``root`` is
        the repo cwd passed to every ``github_api.*`` call so the enrichment is
        scoped to that PR's repo.
        """
        num = pr.number

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

        # Find worktree — scoped to THIS repo's root so a same-named branch
        # in a sibling repo can't resolve a multi-repo PR to the wrong
        # checkout (BOU-1720). ``root`` is None only in the legacy
        # single-repo/no-cwd path, which restores the unscoped behavior.
        pr.worktree_path = find_worktree_for_branch(pr.branch, root=root)

        if bulk_merge_state in ("", "UNKNOWN") or bulk_mergeable in ("", "UNKNOWN"):
            refetched_state, refetched_mergeable = await asyncio.to_thread(
                github_api.get_mergeability, num, root
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
            github_api.get_latest_commit, num, root
        )
        pr.latest_commit_sha = sha
        pr.latest_commit_date = date
        if previous_commit_sha and sha and sha != previous_commit_sha:
            pr.no_push_comment_retry_count = 0

        # Get CI checks
        checks = await asyncio.to_thread(
            github_api.get_ci_checks, num, root
        )
        pr.ci_checks = checks
        if any(c.status in {"queued", "in_progress"} for c in checks):
            queued_jobs, runner_pool_health, runner_execution_summary = await asyncio.to_thread(
                github_api.get_workflow_queue_health, num, root
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

        # Get unaddressed comments (filtered by commit date!) with per-thread decisions
        comments, decisions = await asyncio.to_thread(
            github_api.scan_review_threads, num, date, root
        )
        pr.review_comments = comments
        self._emit(
            "comment_scan",
            pr_number=num,
            repo=root,
            details={"decisions": [d.model_dump() for d in decisions], "picked": len(comments)},
        )

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
                pid=None,
                agent="agentic-pr-dash-dashboard",
                lease_seconds=load_config(pr.worktree_path).lease_seconds,
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
