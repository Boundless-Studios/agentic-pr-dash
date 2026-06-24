"""Durable PR maintenance handoff state for the dashboard."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import session_registry
from .agents import discover_primary_feature_pipeline_agents
from .config import load as load_config
from .models import AgentProcess, MaintenanceState, MaintenanceStatus, PRData
from .tracker import get_tracker

HANDOFF_FILENAME = "MAINTENANCE_HANDOFF.md"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def state_path(worktree_path: str, pr_number: int) -> Path:
    return load_config(worktree_path).maintenance_dir_for(worktree_path) / f"pr-{pr_number}.json"


def handoff_path(worktree_path: str) -> Path:
    return Path(worktree_path) / HANDOFF_FILENAME


def pr_url(pr_number: int | str, fallback_url: str | None = None, *, cwd: str | None = None) -> str:
    if fallback_url:
        return fallback_url
    repo = load_config(cwd).resolved_repo(Path(cwd) if cwd else None) if cwd else load_config().resolved_repo()
    return f"https://github.com/{repo}/pull/{pr_number}" if repo else f"#{pr_number}"


def pr_markdown_link(pr_number: int | str, fallback_url: str | None = None) -> str:
    return f"[PR #{pr_number}]({pr_url(pr_number, fallback_url)})"


def blockers_for_pr(pr: PRData) -> list[str]:
    blockers: list[str] = []
    if pr.merge_state == "DIRTY" or pr.mergeable == "CONFLICTING" or pr.status.value == "merge_conflict":
        blockers.append("merge_conflict")
    if pr.failing_checks:
        blockers.append("ci_failure")
    if pr.review_comments:
        blockers.append("review_comments")
    return blockers


def watch_pending_for_pr(pr: PRData) -> bool:
    """True when required CI is still running and there are no actionable blockers.

    A PR is watch-pending when:
    - ci_watch_pending is True (a required check is still queued/in_progress), AND
    - there are no current actionable blockers (merge conflict, failing CI, review
      comments) — i.e. nothing for an executor to fix right now, just waiting on CI.
    """
    return bool(pr.ci_watch_pending) and not blockers_for_pr(pr)


def build_maintenance_state(
    *,
    pr_number: int,
    branch: str,
    worktree_path: str,
    blockers: list[str],
    state: MaintenanceStatus = MaintenanceStatus.QUEUED,
    bead_id: str | None = None,
    failing_checks: list[str] | None = None,
    review_comment_ids: list[int] | None = None,
    failure_reason: str | None = None,
) -> MaintenanceState:
    timestamp = now_utc()
    return MaintenanceState(
        pr_number=pr_number,
        branch=branch,
        worktree_path=worktree_path,
        state=state,
        blockers=blockers,
        failing_checks=failing_checks or [],
        review_comment_ids=review_comment_ids or [],
        bead_id=bead_id,
        last_signal_at=timestamp,
        last_heartbeat_at=timestamp,
        last_progress_at=timestamp,
        failure_reason=failure_reason,
    )


def save_state(state: MaintenanceState) -> None:
    path = state_path(state.worktree_path, state.pr_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_state(worktree_path: str, pr_number: int) -> MaintenanceState | None:
    path = state_path(worktree_path, pr_number)
    if not path.exists():
        return None
    try:
        return MaintenanceState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def prune_state(worktree_path: str, pr_number: int) -> bool:
    """Delete the persisted ``pr-<n>.json`` maintenance state for a PR (BOU-1637).

    A maintenance state file lingers under ``.agentic-pr-dash/maintenance/`` after
    its PR merges/closes — nothing ever removed it, so the directory accumulated a
    file per PR forever. The reconcile/refresh path already knows which PRs closed;
    it calls this to drop their state. Best-effort: a missing file is a no-op and
    returns False. Returns True only when a file was actually removed.
    """
    path = state_path(worktree_path, pr_number)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def queue_handoff(pr: PRData, prompt: str) -> MaintenanceState:
    if not pr.worktree_path:
        raise ValueError("PR has no worktree path")

    existing = load_state(pr.worktree_path, pr.number)
    state = build_maintenance_state(
        pr_number=pr.number,
        branch=pr.branch,
        worktree_path=pr.worktree_path,
        blockers=blockers_for_pr(pr),
        bead_id=existing.bead_id if existing else None,
        failing_checks=list(pr.failing_checks),
        review_comment_ids=[comment.id for comment in pr.review_comments],
    )
    state.bead_id = ensure_maintenance_bead(state, pr, prompt)
    save_state(state)
    write_pipeline_handoff(state, prompt)
    return state


def write_pipeline_handoff(state: MaintenanceState, prompt: str) -> None:
    path = handoff_path(state.worktree_path)
    body = (
        "# PR Dashboard Maintenance Handoff\n\n"
        f"PR: {pr_markdown_link(state.pr_number)}\n"
        f"Branch: `{state.branch}`\n"
        f"Bead: `{state.bead_id or 'not-created'}`\n"
        f"State: `{state.state.value}`\n"
        f"Blockers: {', '.join(state.blockers) or 'none'}\n\n"
        "## Resume Instructions\n"
        "Handle this PR's maintenance: resolve review comments, CI failures, and "
        "merge conflicts before pushing.\n\n"
        "## Dashboard Prompt\n"
        f"{prompt}\n"
    )
    path.write_text(body, encoding="utf-8")


def mark_state(
    state: MaintenanceState,
    status: MaintenanceStatus,
    *,
    failure_reason: str | None = None,
    output_tail: list[str] | None = None,
) -> MaintenanceState:
    timestamp = now_utc()
    state.state = status
    state.last_heartbeat_at = timestamp
    state.last_progress_at = timestamp
    state.failure_reason = failure_reason
    if output_tail is not None:
        state.output_tail = output_tail[-20:]
    save_state(state)
    return state



def discover_active_primary_feature_pipeline_agents(worktree_path: str) -> list[AgentProcess]:
    """Live, independent sessions that own a worktree — so the dashboard defers
    to them instead of spawning a competing maintenance agent.

    Combines two signals so a genuine session is never missed and the
    dashboard's own automation is never mistaken for one:

      1. The process-table detector (tightened to real `/feature-pipeline`
         invocations) — catches hand-launched interactive sessions that never
         registered a session event.
      2. The session registry (authoritative self-reported launches), gated on
         pid-liveness + a staleness TTL and filtered to the dashboard's own
         `pr-dashboard` automation — catches launcher-driven sessions even when
         their command line doesn't carry a `/feature-pipeline` token (e.g. a
         `codex exec` worker).
    """
    by_pid: dict[int, AgentProcess] = {
        agent.pid: agent
        for agent in discover_primary_feature_pipeline_agents([worktree_path]).get(worktree_path, [])
    }
    discovery_names = set(load_config(worktree_path).discovery_names)
    for state in session_registry.active_sessions_for_worktree(worktree_path):
        if state.cli not in discovery_names:
            continue
        if state.pid is None or state.pid in by_pid:
            continue
        by_pid[state.pid] = AgentProcess(
            pid=state.pid,
            cli_name=state.cli,
            label=state.cli.capitalize(),
            command=f"session:{state.session_id} ({state.launch_source})",
        )
    return sorted(by_pid.values(), key=lambda agent: agent.pid)


def discover_active_primary_claude(worktree_path: str) -> list[AgentProcess]:
    return discover_active_primary_feature_pipeline_agents(worktree_path)


def build_maintenance_prompt(
    pr: PRData,
    *,
    failed_logs: dict[str, str] | None = None,
    guidance: str | None = None,
) -> str:
    """Build the delegated PR-maintenance prompt."""
    sections = [
        f"{pr_markdown_link(pr.number, pr.url)} ({pr.branch}) needs PR maintenance.",
        "",
        _conventions_preamble(pr.worktree_path),
        "This is delegated focused work on an existing PR.",
        "Do NOT create a new branch or PR. Commit and push to the existing branch.",
    ]

    if pr.merge_state == "DIRTY" or pr.mergeable == "CONFLICTING" or pr.status.value == "merge_conflict":
        base_branch = pr.base_branch or "main"
        sections.extend([
            "",
            f"## Merge Conflicts",
            f"This PR has merge conflicts against `{base_branch}`.",
            f"Fetch `origin/{base_branch}`, merge it into the current branch, resolve conflicts, test, commit, and push.",
            "Do not use `git reset --hard` and do not discard unrelated local changes.",
        ])

    if pr.failing_checks:
        failing = ", ".join(pr.failing_checks)
        sections.extend([
            "",
            "## CI Failures",
            f"This PR has {len(pr.failing_checks)} failing CI check(s): {failing}.",
            "Fix the failures and run the narrowest relevant local tests before pushing.",
        ])
        for check_name, log_tail in (failed_logs or {}).items():
            sections.extend([
                "",
                f"--- Failed CI log: {check_name} ---",
                log_tail,
                "--- End ---",
            ])

    if pr.review_comments:
        sections.extend([
            "",
            "## Review Comments",
            "Address each review comment below, commit, and push.",
            "After pushing, run `agentic-pr-dash complete` "
            "to post completion replies and resolve the threads.",
        ])
        for comment in pr.review_comments:
            loc = f" on `{comment.path}:{comment.line}`" if comment.path else ""
            sections.extend([
                "",
                f"### Comment {comment.id} by @{comment.author}{loc}",
                comment.body,
            ])

    if guidance:
        sections.extend([
            "",
            "## Additional Guidance From Developer",
            guidance,
        ])

    return "\n".join(sections).strip() + "\n"


def _conventions_preamble(cwd: str | None = None) -> str:
    """Opening line(s) of the maintenance prompt.

    Projects override the wording via ``prompt_template`` (an inline string or a
    file path) in config. The default is tool-neutral — it does not assume any
    particular workflow ("feature-pipeline", etc.).
    """
    cfg = load_config(cwd) if cwd else load_config()
    if cfg.prompt_template:
        candidate = Path(cfg.prompt_template)
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        return cfg.prompt_template.strip()
    return (
        "Follow this project's contribution conventions for PR maintenance, but "
        "do not start new feature work."
    )


def ensure_maintenance_bead(state: MaintenanceState, pr: PRData, prompt: str) -> str | None:
    """Open (or find) a tracked task for this PR via the configured tracker.

    With the default ``none`` tracker this is a no-op and returns ``None`` — the
    maintenance flow works purely off PR state. A project that wants a durable
    work-ledger configures ``tracker = "beads" | "github-issues"``.
    """
    if state.bead_id:
        return state.bead_id

    tracker = get_tracker(load_config(state.worktree_path))
    title = f"Address PR #{pr.number} maintenance blockers"
    body = (
        f"Handle maintenance blockers for {pr_markdown_link(pr.number, pr.url)} "
        f"on branch `{pr.branch}`.\n\n"
        "## Acceptance Criteria\n"
        "- [ ] Review comments in the maintenance prompt are addressed\n"
        "- [ ] Failing CI checks pass after a pushed commit\n"
        "- [ ] Merge conflicts are resolved when present\n"
        "- [ ] Existing branch is pushed; no new PR is created\n\n"
        f"Latest commit: {pr.latest_commit_sha or 'unknown'}\n\n"
        f"{prompt}"
    )
    return tracker.open_task(pr=pr.number, branch=pr.branch, title=title, body=body, cwd=state.worktree_path)


def _branch_label(branch: str) -> str:
    for prefix in ("feature/", "fix/", "chore/", "hotfix/", "release/"):
        if branch.startswith(prefix):
            branch = branch[len(prefix):]
            break
    return branch.replace("/", "-")
