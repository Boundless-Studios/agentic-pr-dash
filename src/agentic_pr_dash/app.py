"""FastAPI app — PR Dashboard with HTMX live updates."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
import platform
from pathlib import Path
import subprocess
import time

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .agents import discover_active_agents
from .config import load as load_config
from .iterm import focus_or_open_worktree
from .models import (
    AgentProcess,
    CICheck,
    MaintenanceState,
    MaintenanceStatus,
    PRData,
    PRStatus,
    QueuedWorkflowJob,
    ReviewComment,
    RunnerExecutionSummary,
    WorktreeCard,
    worktree_started_at,
)
from .orchestrator import Orchestrator
from .runner_monitor import get_runner_fleet_load
from . import session_registry
from .worktrees import discover_worktrees, get_main_repo_root

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
CONTEXT_CACHE_TTL_SECONDS = 3.0
_dashboard_context_cache: dict[tuple[bool, str], tuple[float, dict[str, object]]] = {}
_dashboard_context_tasks: dict[tuple[bool, str], asyncio.Task[dict[str, object]]] = {}


def _asset_version() -> str:
    """Short fingerprint of the static bundle so changed JS/CSS load under a
    fresh URL. Busts both the browser HTTP cache and the service-worker cache
    key, which otherwise pin a long-lived dashboard tab to stale code."""
    import hashlib

    h = hashlib.sha1()
    for name in ("app.js", "style.css"):
        path = BASE_DIR / "static" / name
        try:
            h.update(path.read_bytes())
        except OSError:
            continue
    return h.hexdigest()[:8]

orchestrator = Orchestrator(repo_cwd=get_main_repo_root())
BABYSIT_STATUS_PATH = Path.home() / ".claude" / "babysit-prs-status.json"
ZERO_COMMIT_STALE_SECS = 86400
AGENT_STALE_SECS = 3 * 86400
OTHER_STALE_SECS = 7 * 86400


@asynccontextmanager
async def lifespan(app: FastAPI):
    orchestrator.log("Dashboard starting — background PR polling")
    orchestrator.start()
    yield


app = FastAPI(title="PR Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/sw.js")
async def service_worker():
    """Serve SW from root so it controls the entire origin scope."""
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript")



# -- Kanban columns --

KANBAN_COLUMNS = [
    {
        "id": "needs_attention",
        "title": "Needs Attention",
        "statuses": {PRStatus.CI_FAILING, PRStatus.HAS_COMMENTS, PRStatus.CI_AND_COMMENTS,
                     PRStatus.MERGE_CONFLICT, PRStatus.AGENT_FAILED},
    },
    {
        "id": "in_progress",
        "title": "Agent Working",
        "statuses": {PRStatus.AGENT_WORKING},
    },
    {
        "id": "no_pr",
        "title": "No PR",
        "statuses": {PRStatus.NO_PR},
    },
    {
        "id": "pending",
        "title": "CI Pending",
        "statuses": {PRStatus.CI_PENDING},
    },
    {
        "id": "done",
        "title": "Clean",
        "statuses": {PRStatus.CLEAN},
    },
]


def build_columns(cards: list[WorktreeCard]) -> list[dict]:
    columns = []
    for col in KANBAN_COLUMNS:
        column_cards = [card for card in cards if card.status in col["statuses"]]
        columns.append(
            {
                "id": col["id"],
                "title": col["title"],
                "cards": column_cards,
                "count": len(column_cards),
            }
        )
    return columns


def _is_agent_worktree(worktree: dict) -> bool:
    worktree_name = Path(worktree.get("path") or "").name
    branch = worktree.get("branch") or ""
    return worktree_name.startswith(("worktree-agent-", "agent-")) or branch.startswith(("worktree-agent-", "agent-"))


def _load_babysit_activity() -> tuple[dict[int, str], dict[str, str]]:
    try:
        data = json.loads(BABYSIT_STATUS_PATH.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}, {}

    entries = data.get("prs_with_comments")
    if not isinstance(entries, list):
        return {}, {}

    by_number: dict[int, str] = {}
    by_branch: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        action = str(entry.get("action") or "").strip()
        if not action:
            continue
        message = f"Babysitter: {action}"

        number = entry.get("number")
        if isinstance(number, int):
            by_number[number] = message

        branch = str(entry.get("branch") or "").strip()
        if branch:
            by_branch[branch] = message

    return by_number, by_branch


# Turn-lifecycle activity: the agent-activity hook stamps
# the state-dir's agent-activity.json with the real turn state. A session counts as
# actively working only when it's been in a turn past a short debounce — so a
# few-second `/loop` watch tick doesn't register as work, but real validation /
# fixing does. CPU% can't make this distinction (idle REPLs + the watch loop's
# own ticks both sit above the CPU floor).
_ACTIVITY_DEBOUNCE_SECONDS = 20    # a turn must last this long to read as "working"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _agent_activity_state(worktree_path: str | None) -> str:
    """Aggregate per-session turn state from the state-dir's agent-activity.json:

      "working" — a session is in a sustained turn: state busy, past the debounce,
                  AND its owning pid is still alive.
      "idle"    — sessions exist but none are actively working (idle, sub-debounce
                  quick ticks, or busy records whose owning process is gone).
      "none"    — no activity data; caller falls back to CPU detection.

    Liveness is keyed on the owner PID (ungated), not CPU (PR #1918 review): an
    agent inside a long Bash/Playwright/test call sits at ~0% CPU while its
    non-agent subprocess runs, but its process is alive — so a busy stamp with a
    live pid is real work, while an orphaned stamp from a killed session (dead
    pid) is ignored regardless of age. Per-session aggregation: any one live busy
    session wins, so a second session's Stop can't mask another's live turn.
    """
    if not worktree_path:
        return "none"
    state_dir = load_config(worktree_path).state_dir_for(worktree_path)
    path = str(state_dir / "agent-activity.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return "none"
    sessions = data.get("sessions")
    if not isinstance(sessions, dict) or not sessions:
        return "none"
    now = datetime.now(timezone.utc)
    for rec in sessions.values():
        if not isinstance(rec, dict) or rec.get("state") != "busy":
            continue
        busy_since = _parse_iso(rec.get("busy_since"))
        if busy_since is None:
            continue
        if (now - busy_since).total_seconds() < _ACTIVITY_DEBOUNCE_SECONDS:
            continue  # quick sub-debounce tick (a watch poll) — not yet real work
        pid = rec.get("pid")
        if isinstance(pid, int) and session_registry.pid_is_live(pid):
            return "working"
        # busy stamp whose owner pid is gone → orphan; ignore it
    return "idle"


def _resolve_agent_working(worktree_path: str | None, live: bool) -> bool:
    """Combine turn-state with live-process presence (PR #1918 review):

      * "working" → True (the owning pid was already verified alive).
      * "idle"    → False even if a process lingers (the idle /loop watch case —
        the whole point: don't show working between turns).
      * "none"    → defer to CPU-discovered presence (sessions without the hook).
    """
    state = _agent_activity_state(worktree_path)
    if state == "working":
        return True
    if state == "idle":
        return False
    return live


def _card_activity_message(
    pr: PRData | None, active_agents: list[AgentProcess], agent_working: bool = True
) -> tuple[str | None, str | None]:
    if pr and pr.activity_message:
        return pr.activity_message, pr.activity_source

    if not pr:
        return (f"{active_agents[0].label} working" if active_agents else None), ("local" if active_agents else None)

    babysit_by_number, babysit_by_branch = _load_babysit_activity()
    message = babysit_by_number.get(pr.number) or babysit_by_branch.get(pr.branch)
    if message:
        return message, "babysit"

    if not active_agents:
        return None, None
    # A session that's present but not in an active turn is watching (an idle
    # /pr-maintenance-check loop), not working.
    verb = "working" if agent_working else "watching"
    return f"{active_agents[0].label} {verb}", "local"


def _format_last_updated(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M")


def _now_epoch() -> int:
    return int(datetime.now().timestamp())


def _run_text(cmd: list[str], *, cwd: str | None = None, timeout: int = 10) -> str | None:
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _is_main_or_protected_worktree(worktree: dict) -> bool:
    path = worktree.get("path") or ""
    branch = worktree.get("branch") or ""
    if branch in {"", "main", "master", "detached"}:
        return True
    try:
        return Path(path).resolve() == Path(get_main_repo_root()).resolve()
    except OSError:
        return path == get_main_repo_root()


def _branch_pr_state(branch: str) -> str | None:
    if not branch:
        return None
    output = _run_text(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            "1",
            "--json",
            "number,state",
            "--template",
            "{{range .}}{{.state}}{{end}}",
        ],
        cwd=get_main_repo_root(),
    )
    return output or None


def _worktree_is_dirty(path: str) -> bool:
    output = _run_text(["git", "-C", path, "status", "--porcelain"])
    return bool(output)


def _worktree_branch_stale_reason(path: str, branch: str) -> str | None:
    main_repo = get_main_repo_root()
    fork_point = _run_text(["git", "-C", main_repo, "merge-base", branch, "origin/main"])
    if not fork_point:
        fork_point = _run_text(["git", "-C", main_repo, "merge-base", branch, "main"])
    if not fork_point:
        return None

    raw_count = _run_text(["git", "-C", main_repo, "rev-list", "--count", f"{fork_point}..{branch}"])
    try:
        commit_count = int(raw_count or "0")
    except ValueError:
        return None

    now = _now_epoch()
    if commit_count == 0:
        stat_args = ["stat", "-f", "%B", path] if platform.system() == "Darwin" else ["stat", "-c", "%W", path]
        raw_birth = _run_text(stat_args)
        try:
            birth_epoch = int(raw_birth or "0")
        except ValueError:
            birth_epoch = 0
        age_secs = now - birth_epoch if birth_epoch > 0 else ZERO_COMMIT_STALE_SECS
        if age_secs >= ZERO_COMMIT_STALE_SECS:
            return "orphan with no commits beyond main"
        return None

    raw_last = _run_text(["git", "-C", main_repo, "log", "-1", "--format=%ct", branch])
    try:
        last_epoch = int(raw_last or "0")
    except ValueError:
        return None
    threshold = AGENT_STALE_SECS if Path(path).name.startswith(("worktree-agent-", "agent-")) or branch.startswith("worktree-agent-") else OTHER_STALE_SECS
    age_secs = now - last_epoch
    if age_secs >= threshold:
        return f"stale orphan ({age_secs // 86400}d old, no PR)"
    return None


def _selected_worktree_cleanup_reason(worktree: dict, active_agents: list[AgentProcess]) -> tuple[bool, str]:
    path = worktree.get("path") or ""
    branch = worktree.get("branch") or ""
    if _is_main_or_protected_worktree(worktree):
        return False, "protected worktree"
    if active_agents:
        return False, "active agent detected"
    if _worktree_is_dirty(path):
        return False, "local changes present"

    pr_state = _branch_pr_state(branch)
    if pr_state == "OPEN":
        return False, "open PR exists"
    if pr_state in {"MERGED", "CLOSED"}:
        return True, f"{pr_state.lower()} PR branch"

    stale_reason = _worktree_branch_stale_reason(path, branch)
    if stale_reason:
        return True, stale_reason
    return False, "selected worktree is not stale enough"


def build_worktree_cards(show_agent_worktrees: bool = False) -> tuple[list[WorktreeCard], int, int]:
    worktrees = discover_worktrees()
    active_agents_by_path = discover_active_agents([wt["path"] for wt in worktrees])
    runtime_summary = session_registry.summarize_sessions()
    prs = sorted(orchestrator.prs.values(), key=lambda pr: (pr.number, pr.title), reverse=True)
    hidden_worktree_paths = {
        wt["path"]
        for wt in worktrees
        if not show_agent_worktrees and _is_agent_worktree(wt)
    }
    visible_worktrees = [wt for wt in worktrees if wt["path"] not in hidden_worktree_paths]

    cards: list[WorktreeCard] = []
    seen_pr_numbers: set[int] = set()
    prs_by_worktree = {
        pr.worktree_path: pr
        for pr in prs
        if pr.worktree_path
    }

    for worktree in visible_worktrees:
        pr = prs_by_worktree.get(worktree["path"])
        if pr:
            seen_pr_numbers.add(pr.number)
        cards.append(
            _build_card_for_worktree(
                worktree,
                pr,
                active_agents_by_path.get(worktree["path"], []),
                runtime_summary.by_worktree.get(worktree["path"]),
            )
        )

    for pr in prs:
        if pr.number in seen_pr_numbers:
            continue
        active_agents = active_agents_by_path.get(pr.worktree_path or "", [])
        # Every open PR gets a card, even a clean one whose head branch isn't
        # the currently-checked-out branch of any worktree. This is the common
        # case when several PRs are spun off a single shared parent worktree:
        # only one branch can be checked out at a time, so the others have no
        # worktree match and would otherwise vanish from the board entirely
        # (a clean open PR is "ready to merge" — exactly what should stay
        # visible). Closed/merged PRs are already pruned in refresh_prs.
        cards.append(
            _build_unassigned_pr_card(
                pr,
                active_agents=active_agents,
                worktree_hidden=bool(pr.worktree_path and pr.worktree_path in hidden_worktree_paths),
            )
        )

    cards.sort(key=_card_sort_key)
    return cards, len(visible_worktrees), len(hidden_worktree_paths)


ACTIVE_MAINTENANCE_STATES = {
    MaintenanceStatus.QUEUED,
    MaintenanceStatus.SIGNALED,
    MaintenanceStatus.RUNNING,
    MaintenanceStatus.WAITING_FOR_PUSH,
}


def _card_status(pr: PRData | None, has_active_agents: bool) -> PRStatus:
    if pr:
        maintenance_is_active = (
            pr.maintenance is not None
            and pr.maintenance.state in ACTIVE_MAINTENANCE_STATES
        )
        if has_active_agents or maintenance_is_active:
            return PRStatus.AGENT_WORKING
        if pr.status == PRStatus.CLEAN and pr.review_comments:
            return PRStatus.HAS_COMMENTS
        return pr.status
    if has_active_agents:
        return PRStatus.AGENT_WORKING
    return PRStatus.NO_PR


def _build_card_for_worktree(
    worktree: dict,
    pr: PRData | None,
    active_agents: list[AgentProcess],
    runtime_session: session_registry.RuntimeSessionState | None = None,
) -> WorktreeCard:
    fallback_agents = active_agents or _fallback_dashboard_agent(pr)
    # Prefer the turn-activity signal (real "in a turn" state) when the worktree
    # has an activity stamp; fall back to CPU-discovered active_agents otherwise.
    agent_working = _resolve_agent_working(worktree.get("path"), bool(fallback_agents))
    status = _card_status(pr, agent_working)
    activity_message, activity_source = _card_activity_message(pr, fallback_agents, agent_working)
    cleanup_candidate = False
    if pr is None:
        cleanup_candidate, _ = _selected_worktree_cleanup_reason(worktree, fallback_agents)

    # Prefer the PR's creation timestamp as "started_at"; fall back to the
    # worktree directory's birth/ctime when no PR (or PR has no created_at).
    _pr_created_at = (pr.created_at if pr else "") or ""
    if not _pr_created_at:
        _wt_dt = worktree_started_at(worktree.get("path") or "")
        if _wt_dt is not None:
            _pr_created_at = _wt_dt.isoformat().replace("+00:00", "Z")

    return WorktreeCard(
        id=f"worktree:{worktree['path']}",
        worktree_name=Path(worktree["path"]).name,
        worktree_path=worktree["path"],
        branch=worktree.get("branch") or "",
        environment_name=worktree.get("environment_name"),
        backend_port=worktree.get("backend_port"),
        frontend_port=worktree.get("frontend_port"),
        slot=worktree.get("slot"),
        pr_number=pr.number if pr else None,
        pr_title=pr.title if pr else None,
        pr_url=pr.url if pr else None,
        is_draft=pr.is_draft if pr else False,
        status=status,
        ci_checks=pr.ci_checks if pr else [],
        queued_jobs=pr.queued_jobs if pr else [],
        runner_pool_health=pr.runner_pool_health if pr else [],
        runner_execution_summary=pr.runner_execution_summary if pr else RunnerExecutionSummary(),
        failing_checks=pr.failing_checks if pr else [],
        review_comments=pr.review_comments if pr else [],
        merge_state=pr.merge_state if pr else "unknown",
        review_decision=pr.review_decision if pr else "none",
        latest_commit_sha=pr.latest_commit_sha if pr else "",
        latest_commit_date=pr.latest_commit_date if pr else "",
        last_updated_label=_format_last_updated(pr.latest_commit_date if pr else None),
        active_agents=fallback_agents,
        activity_message=activity_message,
        activity_source=activity_source,
        agent_failure_reason=pr.agent_failure_reason if pr else None,
        agent_session_id=pr.agent_session_id if pr else None,
        agent_output=pr.agent_output if pr else [],
        last_polled=pr.last_polled if pr else None,
        last_agent_dispatch=pr.last_agent_dispatch if pr else None,
        maintenance=pr.maintenance if pr else None,
        cleanup_candidate=cleanup_candidate,
        runtime_session_id=runtime_session.session_id if runtime_session else None,
        docker_mode=runtime_session.docker_mode if runtime_session else None,
        docker_daemon_name=runtime_session.docker_daemon_name if runtime_session else None,
        container_names=runtime_session.container_names if runtime_session else [],
        runtime_warnings=[runtime_session.warning] if runtime_session and runtime_session.warning else [],
        pr_created_at=_pr_created_at,
    )


def _build_unassigned_pr_card(
    pr: PRData,
    active_agents: list[AgentProcess] | None = None,
    worktree_hidden: bool = False,
) -> WorktreeCard:
    fallback_agents = active_agents or _fallback_dashboard_agent(pr)
    # Hidden agent worktrees surface as unassigned cards — use the PR's own
    # worktree_path for the turn-activity signal (fallback to CPU active_agents).
    agent_working = _resolve_agent_working(pr.worktree_path, bool(fallback_agents))
    status = _card_status(pr, agent_working)
    activity_message, activity_source = _card_activity_message(pr, fallback_agents, agent_working)

    return WorktreeCard(
        id=f"pr:{pr.number}",
        worktree_name="Agent worktree hidden" if worktree_hidden else "No worktree",
        worktree_path=None,
        worktree_hidden=worktree_hidden,
        branch=pr.branch,
        pr_number=pr.number,
        pr_title=pr.title,
        pr_url=pr.url,
        is_draft=pr.is_draft,
        status=status,
        ci_checks=pr.ci_checks,
        queued_jobs=pr.queued_jobs,
        runner_pool_health=pr.runner_pool_health,
        runner_execution_summary=pr.runner_execution_summary,
        failing_checks=pr.failing_checks,
        review_comments=pr.review_comments,
        merge_state=pr.merge_state,
        review_decision=pr.review_decision,
        latest_commit_sha=pr.latest_commit_sha,
        latest_commit_date=pr.latest_commit_date,
        last_updated_label=_format_last_updated(pr.latest_commit_date),
        active_agents=fallback_agents,
        activity_message=activity_message,
        activity_source=activity_source,
        agent_failure_reason=pr.agent_failure_reason,
        agent_session_id=pr.agent_session_id,
        agent_output=pr.agent_output,
        last_polled=pr.last_polled,
        last_agent_dispatch=pr.last_agent_dispatch,
        maintenance=pr.maintenance,
        pr_created_at=pr.created_at,
    )


def _fallback_dashboard_agent(pr: PRData | None) -> list[AgentProcess]:
    if not pr or pr.number not in orchestrator._inflight_prs:
        return []
    cli_name = pr.agent_cli_name or "codex"
    return [AgentProcess(pid=0, cli_name=cli_name, label=cli_name.capitalize())]


def _card_sort_key(card: WorktreeCard) -> tuple[int, int, str]:
    return (
        0 if card.pr_number else 1,
        -(card.pr_number or 0),
        card.worktree_name.lower(),
    )


def _show_agent_worktrees(request: Request) -> bool:
    return request.query_params.get("show_agents", "").lower() in {"1", "true", "yes", "on"}


def _bug_bash_ready_count(cards: list[WorktreeCard]) -> int:
    """Count PRs opened by the bug-bash loop that are sitting in the Clean column.

    These are the finished, validated, ready-to-merge PRs the human only has to
    review — surfaced as a banner in the dashboard title bar.
    """
    count = 0
    for card in cards:
        if card.status != PRStatus.CLEAN or not card.pr_number:
            continue
        pr = orchestrator.prs.get(card.pr_number)
        if pr and "bug-bash" in pr.labels:
            count += 1
    return count


def dashboard_context(show_agent_worktrees: bool = False, active_tab: str = "board") -> dict[str, object]:
    cards, worktree_count, hidden_agent_worktree_count = build_worktree_cards(show_agent_worktrees=show_agent_worktrees)
    runner_summary = orchestrator.weekly_runner_execution_summary
    runner_issues = _runner_issues(cards)
    running_github_jobs = _running_github_jobs(cards)
    desktop_docker_instances = _desktop_docker_instances(cards)
    return {
        "columns": build_columns(cards),
        "runner_summary": runner_summary,
        "runner_summary_label": _runner_summary_label(runner_summary, len(runner_issues)),
        "runner_issues": runner_issues,
        "running_github_jobs": running_github_jobs,
        "desktop_docker_instances": desktop_docker_instances,
        "desktop_docker_container_count": sum(len(item["container_names"]) for item in desktop_docker_instances),
        "events": orchestrator.events[:50],
        "pr_count": len(orchestrator.prs),
        "worktree_count": worktree_count,
        "hidden_agent_worktree_count": hidden_agent_worktree_count,
        "bug_bash_ready_count": _bug_bash_ready_count(cards),
        "show_agent_worktrees": show_agent_worktrees,
        "active_tab": active_tab if active_tab in {"board", "runner_issues"} else "board",
        "board_tab_url": "/?tab=board&show_agents=1" if show_agent_worktrees else "/?tab=board",
        "runner_issues_tab_url": "/?tab=runner_issues&show_agents=1" if show_agent_worktrees else "/?tab=runner_issues",
        "board_partial_url": "/partials/board?show_agents=1" if show_agent_worktrees else "/partials/board",
        "runner_issues_partial_url": "/partials/runner-issues?show_agents=1" if show_agent_worktrees else "/partials/runner-issues",
        "asset_version": _asset_version(),
    }


def runner_fleet_context() -> dict[str, object]:
    return {"runner_fleet": get_runner_fleet_load(cwd=get_main_repo_root())}


def _runner_issues_from_prs(hidden_worktree_paths: set[str] | None = None) -> list[dict[str, object]]:
    hidden_worktree_paths = hidden_worktree_paths or set()
    issues: list[dict[str, object]] = []
    for pr in sorted(orchestrator.prs.values(), key=lambda item: item.number, reverse=True):
        if pr.worktree_path and pr.worktree_path in hidden_worktree_paths:
            continue
        for job in pr.queued_jobs:
            if not job.warning:
                continue
            issues.append(
                {
                    "card": {
                        "pr_url": pr.url,
                        "pr_number": pr.number,
                        "pr_title": pr.title,
                        "branch": pr.branch,
                    },
                    "job": job,
                    "warning": job.warning,
                }
            )
    return issues


def _running_github_jobs_from_prs(hidden_worktree_paths: set[str] | None = None) -> list[dict[str, object]]:
    hidden_worktree_paths = hidden_worktree_paths or set()
    jobs: list[dict[str, object]] = []
    for pr in sorted(orchestrator.prs.values(), key=lambda item: item.number, reverse=True):
        if pr.worktree_path and pr.worktree_path in hidden_worktree_paths:
            continue
        for job in pr.queued_jobs:
            if job.status != "in_progress":
                continue
            jobs.append(
                {
                    "card": {
                        "pr_url": pr.url,
                        "pr_number": pr.number,
                        "pr_title": pr.title,
                        "branch": pr.branch,
                    },
                    "job": job,
                }
            )
    return jobs


def _desktop_docker_instances_from_sessions() -> list[dict[str, object]]:
    summary = session_registry.summarize_sessions()
    instances: list[dict[str, object]] = []
    seen: set[str] = set()
    for state in summary.recent:
        if state.is_terminal or state.docker_mode != "remote" or not state.container_names:
            continue
        key = state.worktree_path or state.branch or state.session_id
        if key in seen:
            continue
        seen.add(key)
        instances.append(
            {
                "branch": state.branch or Path(state.worktree_path or "").name or "unknown",
                "worktree_name": Path(state.worktree_path or "").name,
                "worktree_path": state.worktree_path,
                "pr_number": state.pr_number,
                "docker_daemon_name": state.docker_daemon_name or "remote desktop",
                "container_names": state.container_names,
            }
        )
    return instances


def runner_dashboard_context(show_agent_worktrees: bool = False, active_tab: str = "runner_issues") -> dict[str, object]:
    worktrees = discover_worktrees()
    hidden_worktree_paths = {
        worktree["path"]
        for worktree in worktrees
        if not show_agent_worktrees and _is_agent_worktree(worktree)
    }
    hidden_agent_worktree_count = len(hidden_worktree_paths)
    visible_worktree_count = len(worktrees) - hidden_agent_worktree_count
    runner_summary = orchestrator.weekly_runner_execution_summary
    runner_issues = _runner_issues_from_prs(hidden_worktree_paths)
    running_github_jobs = _running_github_jobs_from_prs(hidden_worktree_paths)
    desktop_docker_instances = _desktop_docker_instances_from_sessions()
    return {
        "columns": [],
        "runner_summary": runner_summary,
        "runner_summary_label": _runner_summary_label(runner_summary, len(runner_issues)),
        "runner_issues": runner_issues,
        "running_github_jobs": running_github_jobs,
        "desktop_docker_instances": desktop_docker_instances,
        "desktop_docker_container_count": sum(len(item["container_names"]) for item in desktop_docker_instances),
        "events": orchestrator.events[:50],
        "pr_count": len(orchestrator.prs),
        "worktree_count": visible_worktree_count,
        "hidden_agent_worktree_count": hidden_agent_worktree_count,
        "bug_bash_ready_count": sum(
            1 for pr in orchestrator.prs.values()
            if pr.status == PRStatus.CLEAN and "bug-bash" in pr.labels
        ),
        "show_agent_worktrees": show_agent_worktrees,
        "active_tab": active_tab if active_tab == "runner_issues" else "runner_issues",
        "board_tab_url": "/?tab=board&show_agents=1" if show_agent_worktrees else "/?tab=board",
        "runner_issues_tab_url": "/?tab=runner_issues&show_agents=1" if show_agent_worktrees else "/?tab=runner_issues",
        "board_partial_url": "/partials/board?show_agents=1" if show_agent_worktrees else "/partials/board",
        "runner_issues_partial_url": "/partials/runner-issues?show_agents=1" if show_agent_worktrees else "/partials/runner-issues",
        "asset_version": _asset_version(),
    }


def _canonical_dashboard_tab(active_tab: str) -> str:
    return active_tab if active_tab in {"board", "runner_issues"} else "board"


async def _dashboard_context_async(
    show_agent_worktrees: bool = False,
    active_tab: str = "board",
) -> dict[str, object]:
    active_tab = _canonical_dashboard_tab(active_tab)
    key = (show_agent_worktrees, active_tab)
    now = time.monotonic()
    cached = _dashboard_context_cache.get(key)
    if cached and now - cached[0] <= CONTEXT_CACHE_TTL_SECONDS:
        return cached[1]

    task = _dashboard_context_tasks.get(key)
    if task is None or task.done():
        context_func = runner_dashboard_context if active_tab == "runner_issues" else dashboard_context
        task = asyncio.create_task(asyncio.to_thread(
            context_func,
            show_agent_worktrees=show_agent_worktrees,
            active_tab=active_tab,
        ))
        _dashboard_context_tasks[key] = task

    context = await task
    if _dashboard_context_tasks.get(key) is task:
        _dashboard_context_tasks.pop(key, None)
    _dashboard_context_cache[key] = (time.monotonic(), context)
    return context


def _runner_execution_summary(cards: list[WorktreeCard]) -> RunnerExecutionSummary:
    summary = RunnerExecutionSummary()
    for card in cards:
        summary.desktop_count += card.runner_execution_summary.desktop_count
        summary.github_hosted_count += card.runner_execution_summary.github_hosted_count
        summary.unknown_count += card.runner_execution_summary.unknown_count
        summary.desktop_seconds += card.runner_execution_summary.desktop_seconds
        summary.github_hosted_seconds += card.runner_execution_summary.github_hosted_seconds
        summary.unknown_seconds += card.runner_execution_summary.unknown_seconds
    return summary


def _desktop_docker_instances(cards: list[WorktreeCard]) -> list[dict[str, object]]:
    instances: list[dict[str, object]] = []
    for card in cards:
        if card.docker_mode != "remote" or not card.container_names:
            continue
        instances.append(
            {
                "branch": card.branch,
                "worktree_name": card.worktree_name,
                "worktree_path": card.worktree_path,
                "pr_number": card.pr_number,
                "docker_daemon_name": card.docker_daemon_name or "remote desktop",
                "container_names": card.container_names,
            }
        )
    return instances


def _runner_issues(cards: list[WorktreeCard]) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    for card in cards:
        for job in card.queued_jobs:
            if not job.warning:
                continue
            issues.append(
                {
                    "card": card,
                    "job": job,
                    "warning": job.warning,
                }
            )
    return issues


def _running_github_jobs(cards: list[WorktreeCard]) -> list[dict[str, object]]:
    jobs: list[dict[str, object]] = []
    for card in cards:
        for job in card.queued_jobs:
            if job.status != "in_progress":
                continue
            jobs.append(
                {
                    "card": card,
                    "job": job,
                }
            )
    return jobs


def _format_duration(seconds: float) -> str:
    """Compact wall-clock duration: '45m', '2h', or '2h30m'."""
    total_minutes = int(round(seconds / 60))
    if total_minutes < 60:
        return f"{total_minutes}m"
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"


def _runner_summary_label(summary: RunnerExecutionSummary, issue_count: int) -> str:
    issue_label = "issue" if issue_count == 1 else "issues"
    if summary.total_count == 0:
        return f"CI runners, past 7 days: no run data · {issue_count} {issue_label}"
    return (
        f"CI runners, past 7 days: "
        f"{summary.desktop_count} desktop ({_format_duration(summary.desktop_seconds)}) · "
        f"{summary.github_hosted_count} GitHub-hosted ({_format_duration(summary.github_hosted_seconds)}) · "
        f"{issue_count} {issue_label}"
    )


def _proof_fixture_cards(scenario: str) -> list[WorktreeCard]:
    """Illustrative demo data for `/proof/pr-dashboard-fixture/<scenario>`.

    Project-agnostic sample PRs used for screenshots and a no-network preview of
    the dashboard. `baseline` shows a single clean PR; `diagnostic` shows the
    full review loop in motion across three board columns.
    """
    if scenario == "baseline":
        return [
            WorktreeCard(
                id="demo-clean",
                worktree_name="checkout-retry",
                worktree_path="/home/dev/worktrees/checkout-retry",
                branch="feature/checkout-retry",
                environment_name="checkout-retry",
                frontend_port="3001",
                backend_port="8010",
                slot="1",
                pr_number=412,
                pr_title="Retry failed checkout webhooks",
                pr_url="https://github.com/octocat/hello-world/pull/412",
                status=PRStatus.CLEAN,
                ci_checks=[
                    CICheck(name="unit", status="completed", conclusion="success"),
                    CICheck(name="e2e", status="completed", conclusion="success"),
                ],
                latest_commit_sha="a1b2c3d",
                latest_commit_date="2026-05-25T15:00:00Z",
                last_updated_label="just now",
            )
        ]

    if scenario == "diagnostic":
        return [
            # Column 1 — Needs Attention: failing CI + an unaddressed review comment.
            WorktreeCard(
                id="demo-attention",
                worktree_name="rate-limiter",
                worktree_path="/home/dev/worktrees/rate-limiter",
                branch="feature/rate-limiter",
                environment_name="rate-limiter",
                frontend_port="3002",
                backend_port="8020",
                slot="2",
                pr_number=418,
                pr_title="Add token-bucket rate limiting",
                pr_url="https://github.com/octocat/hello-world/pull/418",
                status=PRStatus.CI_AND_COMMENTS,
                ci_checks=[
                    CICheck(name="unit", status="completed", conclusion="failure"),
                    CICheck(name="lint", status="completed", conclusion="success"),
                ],
                failing_checks=["unit"],
                review_comments=[
                    ReviewComment(
                        id=501,
                        author="alice",
                        body="This races under concurrent refills — guard the bucket with a lock.",
                        path="src/limiter.py",
                        line=88,
                        created_at="2026-05-25T15:10:00Z",
                    )
                ],
                latest_commit_sha="d4e5f6a",
                latest_commit_date="2026-05-25T15:08:00Z",
                last_updated_label="2m ago",
            ),
            # Column 2 — Agent Working: an agent holds the lease and is fixing it.
            WorktreeCard(
                id="demo-working",
                worktree_name="search-pagination",
                worktree_path="/home/dev/worktrees/search-pagination",
                branch="feature/search-pagination",
                environment_name="search-pagination",
                frontend_port="3003",
                backend_port="8030",
                slot="3",
                pr_number=421,
                pr_title="Cursor-based search pagination",
                pr_url="https://github.com/octocat/hello-world/pull/421",
                status=PRStatus.AGENT_WORKING,
                ci_checks=[CICheck(name="unit", status="completed", conclusion="failure")],
                failing_checks=["unit"],
                review_comments=[
                    ReviewComment(
                        id=512,
                        author="bob",
                        body="Off-by-one on the last page — add a test for the empty tail.",
                        path="src/search.py",
                        line=140,
                        created_at="2026-05-25T15:11:00Z",
                    )
                ],
                active_agents=[AgentProcess(pid=4242, cli_name="claude", label="Claude")],
                activity_message="Addressing review comment + failing unit test",
                activity_source="dashboard",
                maintenance=MaintenanceState(
                    pr_number=421,
                    branch="feature/search-pagination",
                    worktree_path="/home/dev/worktrees/search-pagination",
                    state=MaintenanceStatus.RUNNING,
                    blockers=["ci failing", "review comments"],
                    failing_checks=["unit"],
                    review_comment_ids=[512],
                    last_heartbeat_at=datetime(2026, 5, 25, 15, 12, 0),
                    last_progress_at=datetime(2026, 5, 25, 15, 13, 0),
                    output_tail=["reproduced empty-tail bug", "adding regression test", "running unit"],
                ),
                runtime_session_id="sess-search-pagination",
                latest_commit_sha="b7c8d9e",
                latest_commit_date="2026-05-25T15:09:00Z",
                last_updated_label="30s ago",
            ),
            # Column 3 — Clean: ready to merge.
            WorktreeCard(
                id="demo-clean",
                worktree_name="checkout-retry",
                worktree_path="/home/dev/worktrees/checkout-retry",
                branch="feature/checkout-retry",
                environment_name="checkout-retry",
                frontend_port="3001",
                backend_port="8010",
                slot="1",
                pr_number=412,
                pr_title="Retry failed checkout webhooks",
                pr_url="https://github.com/octocat/hello-world/pull/412",
                status=PRStatus.CLEAN,
                ci_checks=[
                    CICheck(name="unit", status="completed", conclusion="success"),
                    CICheck(name="e2e", status="completed", conclusion="success"),
                ],
                latest_commit_sha="a1b2c3d",
                latest_commit_date="2026-05-25T15:00:00Z",
                last_updated_label="just now",
            ),
        ]

    return []


def _proof_fixture_context(scenario: str, active_tab: str = "board") -> dict[str, object]:
    cards = _proof_fixture_cards(scenario)
    runner_summary = _runner_execution_summary(cards)
    runner_issues = _runner_issues(cards)
    running_github_jobs = _running_github_jobs(cards)
    desktop_docker_instances = _desktop_docker_instances(cards)
    active_tab = active_tab if active_tab in {"board", "runner_issues"} else "board"
    return {
        "columns": build_columns(cards),
        "runner_summary": runner_summary,
        "runner_summary_label": _runner_summary_label(runner_summary, len(runner_issues)),
        "runner_issues": runner_issues,
        "running_github_jobs": running_github_jobs,
        "desktop_docker_instances": desktop_docker_instances,
        "desktop_docker_container_count": sum(
            len(instance["container_names"]) for instance in desktop_docker_instances
        ),
        "events": [],
        "pr_count": sum(1 for card in cards if card.pr_number),
        "worktree_count": len({card.worktree_path for card in cards if card.worktree_path}),
        "hidden_agent_worktree_count": 0,
        "bug_bash_ready_count": _bug_bash_ready_count(cards),
        "show_agent_worktrees": True,
        "active_tab": active_tab,
        "board_tab_url": f"/proof/pr-dashboard-fixture/{scenario}?tab=board",
        "runner_issues_tab_url": f"/proof/pr-dashboard-fixture/{scenario}?tab=runner_issues",
        "board_partial_url": f"/proof/pr-dashboard-fixture/{scenario}/board",
        "runner_issues_partial_url": f"/proof/pr-dashboard-fixture/{scenario}/runner-issues",
        "asset_version": _asset_version(),
    }


# -- Jinja2 filters --

def status_label(status: PRStatus) -> str:
    return {
        PRStatus.CLEAN: "OK",
        PRStatus.NO_PR: "No PR",
        PRStatus.CI_PENDING: "CI Pending",
        PRStatus.CI_FAILING: "CI Failing",
        PRStatus.HAS_COMMENTS: "Comments",
        PRStatus.CI_AND_COMMENTS: "CI + Comments",
        PRStatus.MERGE_CONFLICT: "Conflicts",
        PRStatus.AGENT_WORKING: "Agent Working",
        PRStatus.AGENT_FAILED: "Agent Failed",
    }.get(status, "Unknown")


templates.env.filters["status_label"] = status_label


# -- Routes --

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    show_agent_worktrees = _show_agent_worktrees(request)
    active_tab = str(request.query_params.get("tab") or "board")
    context = await _dashboard_context_async(
        show_agent_worktrees=show_agent_worktrees,
        active_tab=active_tab,
    )
    context.update(await asyncio.to_thread(runner_fleet_context))
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=context,
    )


@app.get("/proof/pr-dashboard-fixture/{scenario}", response_class=HTMLResponse)
async def pr_dashboard_proof_fixture(request: Request, scenario: str):
    if scenario not in {"baseline", "diagnostic"}:
        return Response("Unknown PR-dashboard proof fixture", status_code=404)
    active_tab = str(request.query_params.get("tab") or "board")
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=_proof_fixture_context(scenario, active_tab=active_tab),
    )


@app.get("/proof/pr-dashboard-fixture/{scenario}/board", response_class=HTMLResponse)
async def pr_dashboard_proof_fixture_board(request: Request, scenario: str):
    if scenario not in {"baseline", "diagnostic"}:
        return Response("Unknown PR-dashboard proof fixture", status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="partials/board.html",
        context=_proof_fixture_context(scenario),
    )


@app.get("/proof/pr-dashboard-fixture/{scenario}/runner-issues", response_class=HTMLResponse)
async def pr_dashboard_proof_fixture_runner_issues(request: Request, scenario: str):
    if scenario not in {"baseline", "diagnostic"}:
        return Response("Unknown PR-dashboard proof fixture", status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="partials/runner_issues.html",
        context=_proof_fixture_context(scenario, active_tab="runner_issues"),
    )


@app.get("/partials/board", response_class=HTMLResponse)
async def board_partial(request: Request):
    show_agent_worktrees = _show_agent_worktrees(request)
    return templates.TemplateResponse(
        request=request,
        name="partials/board.html",
        context=await _dashboard_context_async(show_agent_worktrees=show_agent_worktrees),
    )


@app.get("/partials/runner-issues", response_class=HTMLResponse)
async def runner_issues_partial(request: Request):
    show_agent_worktrees = _show_agent_worktrees(request)
    return templates.TemplateResponse(
        request=request,
        name="partials/runner_issues.html",
        context=await _dashboard_context_async(
            show_agent_worktrees=show_agent_worktrees,
            active_tab="runner_issues",
        ),
    )


@app.get("/partials/bug-bash-banner", response_class=HTMLResponse)
async def bug_bash_banner_partial(request: Request):
    show_agent_worktrees = _show_agent_worktrees(request)
    return templates.TemplateResponse(
        request=request,
        name="partials/bug_bash_banner.html",
        context=await _dashboard_context_async(show_agent_worktrees=show_agent_worktrees),
    )


@app.get("/partials/runner-fleet", response_class=HTMLResponse)
async def runner_fleet_partial(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="partials/runner_fleet.html",
        context=await asyncio.to_thread(runner_fleet_context),
    )


@app.get("/partials/event-log", response_class=HTMLResponse)
async def event_log_partial(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="partials/event_log.html",
        context={"events": orchestrator.events[:50]},
    )


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _dispatch_response(request: Request, html: str) -> HTMLResponse | RedirectResponse:
    """Return inline HTML for HTMX requests, redirect for plain form POSTs."""
    if _is_htmx(request):
        return HTMLResponse(html)
    return RedirectResponse(url="/", status_code=303)


def _cleanup_response(request: Request, html: str, status_code: int) -> HTMLResponse | RedirectResponse:
    if _is_htmx(request) or status_code >= 400:
        return HTMLResponse(html, status_code=status_code)
    return RedirectResponse(url="/", status_code=303)


@app.post("/api/fix-comments/{pr_number}")
async def fix_comments(pr_number: int, request: Request):
    import asyncio
    pr = orchestrator.prs.get(pr_number)
    if not pr:
        return _dispatch_response(request, '<span class="card-agent-status" style="color:var(--danger)">PR not found</span>')
    if not pr.worktree_path:
        return _dispatch_response(request, '<span class="card-agent-status" style="color:var(--danger)">No worktree</span>')
    if not pr.review_comments:
        return _dispatch_response(request, '<span class="card-agent-status" style="color:var(--warn)">No comments to address</span>')
    if pr.number in orchestrator._inflight_prs:
        return _dispatch_response(request, '<span class="card-agent-status">Agent already working...</span>')

    form = await request.form()
    guidance = str(form.get("guidance", "")).strip() or None
    asyncio.create_task(orchestrator.dispatch_comment_fix(pr_number, guidance=guidance))
    return _dispatch_response(request, f'<span class="card-agent-status">Dispatched for #{pr_number}...</span>')


@app.post("/api/retry-ci/{pr_number}")
async def retry_ci(pr_number: int, request: Request):
    import asyncio
    pr = orchestrator.prs.get(pr_number)
    if not pr:
        return _dispatch_response(request, '<span class="card-agent-status" style="color:var(--danger)">PR not found</span>')
    if not pr.worktree_path:
        return _dispatch_response(request, '<span class="card-agent-status" style="color:var(--danger)">No worktree</span>')
    if pr.number in orchestrator._inflight_prs:
        return _dispatch_response(request, '<span class="card-agent-status">Agent already working...</span>')

    pr.status = PRStatus.CI_FAILING
    asyncio.create_task(orchestrator.dispatch_ci_fix(pr))
    return _dispatch_response(request, f'<span class="card-agent-status">Dispatched for #{pr_number}...</span>')


@app.post("/api/refresh")
async def force_refresh():
    await orchestrator.refresh_prs()
    return RedirectResponse(url="/", status_code=303)


@app.post("/api/cleanup-worktree")
async def cleanup_worktree(request: Request, path: str = Form(...)):
    if not path:
        return _cleanup_response(request, '<span class="card-warning">Missing worktree path</span>', 400)

    worktree = next((wt for wt in discover_worktrees() if wt.get("path") == path), None)
    if not worktree:
        return _cleanup_response(request, '<span class="card-warning">Unknown worktree</span>', 400)

    open_pr = next((pr for pr in orchestrator.prs.values() if pr.worktree_path == path), None)
    if open_pr:
        return _cleanup_response(
            request,
            f'<span class="card-warning">Cannot cleanup: open PR #{open_pr.number}</span>',
            409,
        )

    active_agents = discover_active_agents([path]).get(path, [])
    if active_agents:
        return _cleanup_response(request, '<span class="card-warning">Cannot cleanup: active agent detected</span>', 409)

    eligible, reason = _selected_worktree_cleanup_reason(worktree, active_agents)
    if not eligible:
        return _cleanup_response(request, f'<span class="card-warning">Cannot cleanup: {reason}</span>', 409)

    try:
        result = subprocess.run(
            ["git", "-C", get_main_repo_root(), "worktree", "remove", "--force", path],
            cwd=None,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        orchestrator.log(f"Cleanup failed for {Path(path).name}: {exc}", level="error")
        return _cleanup_response(request, '<span class="card-warning">Cleanup failed</span>', 500)

    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        detail = output.splitlines()[-1] if output else f"exit {result.returncode}"
        orchestrator.log(f"Cleanup failed for {Path(path).name}: {detail}", level="error")
        return _cleanup_response(request, '<span class="card-warning">Cleanup failed</span>', 500)

    if Path(path).exists():
        orchestrator.log(f"Cleanup failed for {Path(path).name}: selected worktree still exists", level="error")
        return _cleanup_response(request, '<span class="card-warning">Cleanup did not remove selected worktree</span>', 500)

    orchestrator.log(f"Cleanup requested for {Path(path).name}: {reason}")
    return _cleanup_response(request, '<span class="card-clean-status">Cleanup requested</span>', 200)


@app.post("/api/focus-worktree")
async def focus_worktree(path: str = Form(...)):
    if not path:
        return Response(status_code=400)
    if focus_or_open_worktree(path):
        return Response(status_code=204)
    return Response(status_code=404)
