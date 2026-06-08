"""Durable PR maintenance handoff state for the dashboard."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import session_registry
from .agents import discover_primary_feature_pipeline_agents
from .models import AgentProcess, MaintenanceState, MaintenanceStatus, PRData, ReviewComment


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def state_path(worktree_path: str, pr_number: int) -> Path:
    return Path(worktree_path) / ".gaia" / "pr-maintenance" / f"pr-{pr_number}.json"


def handoff_path(worktree_path: str) -> Path:
    return Path(worktree_path) / "PIPELINE_HANDOFF.md"


def pr_url(pr_number: int | str, fallback_url: str | None = None) -> str:
    return fallback_url or f"https://github.com/Boundless-Studios/gaia-free/pull/{pr_number}"


def pr_markdown_link(pr_number: int | str, fallback_url: str | None = None) -> str:
    return f"[PR #{pr_number}]({pr_url(pr_number, fallback_url)})"


def blockers_for_pr(pr: PRData) -> list[str]:
    blockers: list[str] = []
    if pr.merge_state == "DIRTY" or pr.status.value == "merge_conflict":
        blockers.append("merge_conflict")
    if pr.failing_checks:
        blockers.append("ci_failure")
    if pr.review_comments:
        blockers.append("review_comments")
    return blockers


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
        f"Run `bd ready --label {_branch_label(state.branch)}` and handle the PR maintenance bead.\n"
        "Resolve review comments, CI failures, and merge conflicts before pushing.\n\n"
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
    for state in session_registry.active_sessions_for_worktree(worktree_path):
        if state.cli not in {"claude", "codex"}:
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
        "Use the project feature-pipeline conventions for PR maintenance, but do not start a new feature pipeline.",
        "This is delegated focused work on an existing PR.",
        "Do NOT create a new branch or PR. Commit and push to the existing branch.",
    ]

    if pr.merge_state == "DIRTY" or pr.status.value == "merge_conflict":
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
            f"After pushing, run `python3 -m pr_dashboard.maintenance_check complete` "
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


def ensure_maintenance_bead(state: MaintenanceState, pr: PRData, prompt: str) -> str | None:
    if state.bead_id:
        return state.bead_id

    title = f"Address PR #{pr.number} maintenance blockers"
    label = _branch_label(pr.branch)
    existing = _find_existing_bead(label, title, state.worktree_path)
    if existing:
        return existing

    description = (
        f"Handle maintenance blockers for {pr_markdown_link(pr.number, pr.url)} "
        f"on branch `{pr.branch}`.\n\n"
        "## Acceptance Criteria\n"
        "- [ ] Review comments listed in the dashboard prompt are addressed\n"
        "- [ ] Failing CI checks listed in the dashboard prompt pass after a pushed commit\n"
        "- [ ] Merge conflicts are resolved when present\n"
        "- [ ] Existing branch is pushed; no new PR is created\n"
    )
    design = (
        f"Dashboard handoff: {state_path(state.worktree_path, pr.number)}\n"
        f"Pipeline handoff: {handoff_path(state.worktree_path)}\n"
        "Use the dashboard prompt embedded in PIPELINE_HANDOFF.md."
    )
    notes = (
        "Plan: PR dashboard maintenance handoff\n"
        f"Branch: {pr.branch}\n"
        f"PR: {pr_markdown_link(pr.number, pr.url)}\n"
        f"Latest commit: {pr.latest_commit_sha or 'unknown'}\n"
        f"Prompt:\n{prompt}"
    )
    cmd = [
        "bd",
        "create",
        title,
        "--type",
        "task",
        "--priority",
        "1",
        "--labels",
        label,
        "--description",
        description,
        "--design",
        design,
        "--notes",
        notes,
        "--json",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=state.worktree_path,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return None
    return payload.get("id") if isinstance(payload, dict) else None


def _find_existing_bead(label: str, title: str, cwd: str) -> str | None:
    try:
        proc = subprocess.run(
            ["bd", "list", "--label", label, "--json"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        return None
    if not isinstance(payload, list):
        return None
    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("title") == title and item.get("status") in {"open", "in_progress"}:
            bead_id = item.get("id")
            return bead_id if isinstance(bead_id, str) else None
    return None


def _branch_label(branch: str) -> str:
    for prefix in ("feature/", "fix/", "chore/", "hotfix/", "release/"):
        if branch.startswith(prefix):
            branch = branch[len(prefix):]
            break
    return branch.replace("/", "-")
