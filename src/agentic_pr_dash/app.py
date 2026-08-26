"""FastAPI app — PR Dashboard with HTMX live updates."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape
import json
import os
from pathlib import Path
import subprocess
import threading
import time

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .agents import ProcessScanUnavailable, discover_active_agents, worktree_occupants
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
    humanize_relative,
    worktree_started_at,
)
from . import orchestrator as _orchestrator_module
from .orchestrator import Orchestrator
from .quota import QuotaDecisionReason, QuotaTelemetry
from .runner_monitor import get_cached_runner_fleet_load
from .webhook import MAX_WEBHOOK_BODY_BYTES, GithubWebhookIngress, WebhookRejected
from . import session_registry
from . import worktrees as _worktrees
from .worktrees import (
    _now_epoch as _worktree_now_epoch,
    discover_worktrees,
    get_main_repo_root,
    remove_worktree,
    selected_worktree_cleanup_reason,
)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Pin the template set to the one this process booted with (BOU-2217). Jinja defaults
# to auto_reload=True, but Python classes never reload — so an in-place snapshot
# reinstall paired a NEW board.html against the OLD WorktreeCard and every render 500'd
# with UndefinedError until someone restarted the daemon. Consistency beats freshness
# here: install-agent-ops-tools.sh already bounces the pr-dashboard daemon after
# installing, which is what makes new templates live.
templates.env.auto_reload = False
# Local card discovery scans process cwd state plus session/ownership records.
# It is deliberately decoupled from the 5s HTMX paint cadence: PR state is
# refreshed by the orchestrator, while rebuilding this local snapshot on every
# browser poll multiplies work by the number of open tabs.
CONTEXT_CACHE_TTL_SECONDS = 30.0
_dashboard_context_cache: dict[tuple[bool, str], tuple[float, dict[str, object]]] = {}
_dashboard_context_tasks: dict[tuple[bool, str], asyncio.Task[dict[str, object]]] = {}
_dashboard_context_generation = 0
_DASHBOARD_CONTEXT_STALE_AT = float("-inf")
_OWNERSHIP_CACHE_TTL_SECONDS = 60.0
_ownership_card_cache: dict[tuple[str | None, int | None, str], tuple[float, dict]] = {}
_ownership_card_cache_lock = threading.Lock()
_OWNERSHIP_SNAPSHOT_UNAVAILABLE = object()


def _invalidate_dashboard_context() -> None:
    global _dashboard_context_generation
    _dashboard_context_generation += 1
    # A worker dispatched through ``asyncio.to_thread`` cannot be stopped by
    # dropping its Task reference. Clearing the task map orphaned that scan and
    # let every webhook start another one, keeping the board on its cold
    # skeleton while duplicate process/session scans consumed CPU. Preserve
    # both the displayable snapshot and the single in-flight worker; timestamp
    # entries as stale so one follow-up build runs after the worker settles.
    for key, (_timestamp, context) in list(_dashboard_context_cache.items()):
        _dashboard_context_cache[key] = (_DASHBOARD_CONTEXT_STALE_AT, context)
    with _ownership_card_cache_lock:
        _ownership_card_cache.clear()


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


def _stale_after_seconds() -> float:
    """Age past which the board should say its data is old.

    Three validation windows: one missed probe is noise, three is a pattern.
    Derived rather than hardcoded, because ``APD_LIST_VALIDATION_INTERVAL_S`` is
    tunable — a fixed 180s would call healthy data stale two minutes before the
    next probe was even due at a five-minute interval, and would sit through
    eighteen missed probes at a ten-second one (BOU-3095 PR #169 review round 4).
    """

    return 3 * _orchestrator_module.LIST_VALIDATION_INTERVAL.total_seconds()


def _format_observation_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _observation_context() -> dict[str, object]:
    """Age of the board's GitHub observation, for the header indicator.

    Deliberately not derived from any PR's ``updatedAt``: that value travels
    inside the payload, so a stale board quotes a stale timestamp and reads as
    plausibly recent. This is the age of the observation itself.
    """

    freshness = orchestrator.open_set_freshness()
    age = freshness.age_seconds(orchestrator.observation_controller.now())
    if age is None:
        # Never observed. That is "still starting up" only until an attempt has
        # actually failed — a daemon that starts while discovery is quota-denied
        # would otherwise show a calm, non-stale "loading" forever, which is
        # precisely the outage this indicator exists to expose (BOU-3095
        # PR #169 review).
        reason = freshness.degraded_reason
        if reason:
            return {
                "known": False,
                "stale": True,
                "label": "PR data unavailable",
                "detail": reason,
            }
        return {
            "known": False,
            "stale": False,
            "label": "PR data loading",
            "detail": "no GitHub observation yet",
        }
    stale = age > _stale_after_seconds() or not freshness.complete
    label = f"PR data {_format_observation_age(age)} old"
    if not freshness.complete:
        label = f"{label} (partial)"
    detail = freshness.degraded_reason or (
        "some watched repositories have not been observed yet"
        if not freshness.complete
        else "GitHub open-PR set observed this recently"
    )
    return {"known": True, "stale": stale, "label": label, "detail": detail}


def _open_set_is_complete() -> bool:
    """Whether every watched repo root's open set has been observed."""

    freshness = orchestrator.open_set_freshness()
    return freshness.observed_at is not None and freshness.complete


def _with_header_oob(
    ctx: dict[str, object], *, board_oob: bool = False
) -> dict[str, object]:
    """Context for an HTMX partial that must refresh the shared header.

    Every polled partial needs this: the header lives outside each tab's swap
    target, so a tab that omits it renders the freshness indicator once and then
    freezes it — which reads as "current" right through an outage (BOU-3095).

    Always COPIES. ``_dashboard_context_async`` hands back the cached dict, and
    setting these flags on it in place leaks them into later full-page renders,
    which then emit a duplicate slot id — and htmx swaps the first match.

    ``observation`` is recomputed so the swapped-in value reflects this request
    rather than whenever the context was last built.
    """

    extra: dict[str, object] = {
        "observation_oob": True,
        "observation": _observation_context(),
    }
    if board_oob:
        extra["board_oob"] = True
    return {**ctx, **extra}


def _quota_context(telemetry: QuotaTelemetry) -> dict[str, object]:
    latest = telemetry.latest
    degraded_reason = telemetry.degraded_reason
    if isinstance(degraded_reason, QuotaDecisionReason):
        degraded_reason_value: str | None = degraded_reason.value
    else:
        degraded_reason_value = degraded_reason
    last_decision = telemetry.last_decision
    return {
        "observed": latest is not None,
        "label": (
            f"GitHub {latest.remaining:,} / {latest.limit:,}"
            if latest is not None
            else "GitHub quota unobserved"
        ),
        "latest_cost": latest.cost if latest is not None else None,
        "remaining": latest.remaining if latest is not None else None,
        "limit": latest.limit if latest is not None else None,
        "reset_at": latest.reset_at.isoformat() if latest is not None else None,
        "observed_at": latest.observed_at.isoformat() if latest is not None else None,
        "latest_caller": (
            latest.caller.value if latest is not None and latest.caller is not None else None
        ),
        "latest_work_class": (
            latest.work_class.value
            if latest is not None and latest.work_class is not None
            else None
        ),
        "rolling_cost_by_caller": {
            caller.value: cost
            for caller, cost in telemetry.rolling_cost_by_caller.items()
        },
        "rolling_cost_by_work_class": {
            work_class.value: cost
            for work_class, cost in telemetry.rolling_cost_by_work_class.items()
        },
        "request_count": telemetry.request_count,
        "cache_hit_count": telemetry.cache_hit_count,
        "total_request_count": telemetry.total_request_count,
        "cache_hit_rate": telemetry.cache_hit_rate,
        "background_hourly_spend": telemetry.background_hourly_spend,
        "background_hourly_budget": telemetry.background_hourly_budget,
        "maintenance_reserve": telemetry.maintenance_reserve,
        "backoff_active": telemetry.backoff_active,
        "backoff_until": (
            telemetry.backoff_until.isoformat()
            if telemetry.backoff_until is not None
            else None
        ),
        "backoff_reason": telemetry.backoff_reason,
        "degraded": telemetry.degraded,
        "degraded_reason": degraded_reason_value,
        "last_decision": (
            {
                "allowed": last_decision.allowed,
                "reason": last_decision.reason.value,
                "degraded": last_decision.degraded,
            }
            if last_decision is not None
            else None
        ),
        "last_denial_reason": (
            telemetry.last_denial_reason.value
            if telemetry.last_denial_reason is not None
            else None
        ),
    }

orchestrator = Orchestrator(repo_cwd=get_main_repo_root())
webhook_ingress = GithubWebhookIngress(orchestrator, _invalidate_dashboard_context)
BABYSIT_STATUS_PATH = Path.home() / ".claude" / "babysit-prs-status.json"
ZERO_COMMIT_STALE_SECS = 86400
AGENT_STALE_SECS = 3 * 86400
OTHER_STALE_SECS = 7 * 86400


@asynccontextmanager
async def lifespan(app: FastAPI):
    orchestrator.log("Dashboard starting — background PR polling")
    orchestrator.start()
    try:
        yield
    finally:
        await webhook_ingress.shutdown()


app = FastAPI(title="PR Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/sw.js")
async def service_worker():
    """Serve SW from root so it controls the entire origin scope."""
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript")



# -- Kanban columns --

# BOU-2431: five columns, sized to fit without horizontal scrolling. Three
# distinctions were dropped because they cost a column each and paid nothing:
#   * "Needs Your Decision" merged INTO "Needs Attention" — both mean "this
#     PR is stuck until a human touches it", and the split doubled the number
#     of places to look. The BOU-2402 signal is not lost: decision cards sort
#     FIRST inside the column and still render their question via the
#     `decision-wait` block, which is the surface that actually tells the
#     viewer what they're blocking.
#   * "Waiting" merged into "Agent Working" — an agent waiting on its own poll
#     is still an agent's job, not the viewer's.
#   * "No PR" left the board entirely for its own tab (see NO_PR_TAB): a
#     worktree with no PR is not PR work, and it crowded out the columns that
#     are.
KANBAN_COLUMNS = [
    {
        "id": "needs_attention",
        "title": "Needs Attention",
        "statuses": {PRStatus.WAITING_HUMAN_DECISION, PRStatus.CI_FAILING, PRStatus.HAS_COMMENTS,
                     PRStatus.CI_AND_COMMENTS, PRStatus.MERGE_CONFLICT, PRStatus.AGENT_FAILED,
                     PRStatus.OBSERVATION_UNAVAILABLE},
    },
    {
        "id": "in_progress",
        "title": "Agent Working",
        "statuses": {PRStatus.AGENT_WORKING, PRStatus.AGENT_WAITING},
    },
    {
        "id": "pending",
        "title": "CI Pending",
        "statuses": {PRStatus.CI_PENDING},
    },
    {
        "id": "ready_cleanup",
        "title": "Ready / Cleanup",
        "statuses": {PRStatus.READY_CLEANUP},
    },
    {
        "id": "done",
        "title": "Clean",
        "statuses": {PRStatus.CLEAN},
    },
]


# Statuses that no longer have a column: they belong to the no-PR tab instead.
NO_PR_TAB_STATUSES = {PRStatus.NO_PR}

VALID_DASHBOARD_TABS = {"board", "runner_issues", "worktrees"}


def no_pr_cards(cards: list[WorktreeCard]) -> list[WorktreeCard]:
    """Worktrees with no PR — the contents of the Worktrees tab (BOU-2431)."""
    return [card for card in cards if card.status in NO_PR_TAB_STATUSES]


def _needs_attention_sort_key(card: WorktreeCard) -> int:
    """Decision-blocked cards sort first within Needs Attention (BOU-2402)."""
    return 0 if card.status == PRStatus.WAITING_HUMAN_DECISION else 1


def build_columns(cards: list[WorktreeCard]) -> list[dict]:
    columns = []
    for col in KANBAN_COLUMNS:
        column_cards = [card for card in cards if card.status in col["statuses"]]
        if col["id"] == "needs_attention":
            # Stable sort: within each group the existing card order is kept.
            column_cards.sort(key=_needs_attention_sort_key)
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
_HARNESS_STATUS_STALE_SECONDS = 90
# Rotation machinery that is genuinely mid-flight: short-lived, and reaping or
# re-dispatching against it would be wrong, so it counts as work.
_HARNESS_ACTIVE_TRANSITION_STATES = {
    "checkpointing",
    "checkpointed",
    "fencing",
    "fenced",
    "claiming",
    "launching",
    "awaiting_ack",
}
# Wind-down and blocked phases. `draining` in particular means "waiting for the
# session to fall idle before rotating" — and it is also where a session whose
# hooks broke parks — so it is never active coding (BOU-2365).
_HARNESS_WAITING_STATES = {
    "draining",
    "stopping",
    "stopped",
    "blocked",
}
_DEFERRED_STOP_DIR = ".agent-activity-deferred-stops"


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


def _legacy_agent_activity_state(worktree_path: str | None) -> str:
    """Aggregate per-session turn state from the state-dir's agent-activity.json:

      "working" — a session is in a sustained turn: state busy, past the debounce,
                  AND its owning pid is still alive.
      "waiting" — sessions exist but none are actively working (idle, sub-debounce
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
    deferred_sequences: dict[str, int] = {}
    deferred_dir = state_dir / _DEFERRED_STOP_DIR
    try:
        deferred_paths = list(deferred_dir.glob("*.json"))
    except OSError:
        deferred_paths = []
    for deferred_path in deferred_paths:
        try:
            deferred = json.loads(deferred_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        session_id = deferred.get("session_id")
        record = deferred.get("record")
        if not isinstance(session_id, str) or not isinstance(record, dict):
            continue
        sequence = record.get("sequence")
        if record.get("state") == "idle" and isinstance(sequence, int):
            deferred_sequences[session_id] = max(
                sequence, deferred_sequences.get(session_id, 0)
            )
    now = datetime.now(timezone.utc)
    for session_id, rec in sessions.items():
        if not isinstance(rec, dict) or rec.get("state") != "busy":
            continue
        sequence = rec.get("sequence")
        deferred_sequence = deferred_sequences.get(session_id)
        if deferred_sequence is not None and (
            not isinstance(sequence, int) or deferred_sequence >= sequence
        ):
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
    return "waiting"


def _harness_activity_state(
    runtime_session: session_registry.RuntimeSessionState | None,
    *,
    now: datetime | None = None,
) -> str:
    """Return fresh canonical activity, or ``none`` to preserve fallbacks.

    One of ``working`` (real work or rotation mid-flight), ``waiting`` (live but
    idle, winding down, or blocked) or ``none`` (no usable signal).
    """
    if runtime_session is None or runtime_session.is_terminal:
        return "none"
    if not _harness_report_is_fresh(runtime_session, now=now):
        return "none"
    if any(
        (
            runtime_session.active_turns,
            runtime_session.active_tools,
            runtime_session.active_subagents,
            runtime_session.active_critical_sections,
        )
    ):
        return "working"
    if runtime_session.supervisor_state in _HARNESS_ACTIVE_TRANSITION_STATES:
        return "working"
    if runtime_session.supervisor_state in _HARNESS_WAITING_STATES:
        return "waiting"
    if runtime_session.quiescence == "busy":
        return "working"
    if runtime_session.quiescence == "idle":
        return "waiting"
    return "none"


def _harness_report_is_fresh(
    runtime_session: session_registry.RuntimeSessionState,
    *,
    now: datetime | None = None,
) -> bool:
    reported_at = _parse_iso(runtime_session.harness_reported_at)
    if reported_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    return (current - reported_at).total_seconds() <= _HARNESS_STATUS_STALE_SECONDS


def _runtime_session_is_live(
    runtime_session: session_registry.RuntimeSessionState,
) -> bool:
    if runtime_session.is_terminal:
        return False
    if not runtime_session.harness_reported_at:
        return True
    if _harness_report_is_fresh(runtime_session):
        return True
    return session_registry.pid_is_live(runtime_session.pid)


def _runtime_status_is_stale(
    runtime_session: session_registry.RuntimeSessionState,
) -> bool:
    return bool(
        runtime_session.harness_reported_at
        and not runtime_session.is_terminal
        and not _harness_report_is_fresh(runtime_session)
    )


def _agent_activity_state(
    worktree_path: str | None,
    runtime_session: session_registry.RuntimeSessionState | None = None,
) -> str:
    canonical = _harness_activity_state(runtime_session)
    if canonical != "none":
        return canonical
    return _legacy_agent_activity_state(worktree_path)


def _resolve_agent_activity(
    worktree_path: str | None,
    live: bool,
    runtime_session: session_registry.RuntimeSessionState | None = None,
) -> str:
    """Combine turn-state with live-process presence (PR #1918 review):

      * "working" → the owning pid was already verified alive and in a turn.
      * "waiting" → a session is present but between turns (the idle /loop watch
        case — the whole point: don't show working between turns).
      * "none"    → no session at all.

    With no activity signal, a bare live process resolves to ``waiting``, never
    ``working`` (BOU-2365): liveness is not work. Hook-less sessions that ARE
    working are still caught upstream by the maintenance state, which the loop
    sets to RUNNING when it dispatches an executor.
    """
    state = _agent_activity_state(worktree_path, runtime_session)
    if state in ("working", "waiting"):
        return state
    return "waiting" if live else "none"


def _waiting_reason(
    pr: PRData | None,
    runtime_session: session_registry.RuntimeSessionState | None,
) -> str:
    """Why an idle live session is idle — shown on the card's state chip."""
    if runtime_session is not None and runtime_session.supervisor_state in _HARNESS_WAITING_STATES:
        return "winding down"
    if pr is not None and (pr.ci_watch_pending or pr.status == PRStatus.CI_PENDING):
        return "external checks"
    return "user input"


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
    return humanize_relative(parsed)


def _now_epoch() -> int:
    return _worktree_now_epoch()


def _selected_worktree_cleanup_reason(
    worktree: dict,
    active_agents: list[AgentProcess],
    *,
    check_remote_pr: bool = True,
) -> tuple[bool, str]:
    original_subprocess = _worktrees.subprocess
    _worktrees.subprocess = subprocess
    try:
        return selected_worktree_cleanup_reason(
            worktree,
            active_agents,
            main_repo=get_main_repo_root(),
            check_remote_pr=check_remote_pr,
        )
    finally:
        _worktrees.subprocess = original_subprocess


def build_worktree_cards(show_agent_worktrees: bool = False) -> tuple[list[WorktreeCard], int, int]:
    worktrees = discover_worktrees()
    main_repo_root = get_main_repo_root()
    active_agents_by_path = discover_active_agents([wt["path"] for wt in worktrees])
    runtime_summary = session_registry.summarize_sessions()
    # Branch attribution (below) is scoped to THIS repo's worktrees and reads
    # their custom registries, so a same-named branch elsewhere can't hijack a
    # card and a custom-registry repo's session is still found.
    repo_worktree_paths = {wt["path"] for wt in worktrees}
    branch_session_summary = _repo_session_summary(worktrees, runtime_summary)
    prs = sorted(orchestrator.prs.values(), key=lambda pr: (pr.number, pr.title), reverse=True)
    hidden_worktree_paths = {
        wt["path"]
        for wt in worktrees
        if not show_agent_worktrees and _is_agent_worktree(wt)
    }
    visible_worktrees = [wt for wt in worktrees if wt["path"] not in hidden_worktree_paths]

    # Ownership resolution replays the full claim store. A cold card cache has
    # one distinct key per worktree, so capture once for the entire board build
    # instead of spending one bounded store read on every cache miss.
    from ._maintenance.ownership_resolution import claim_reads_enabled  # noqa: PLC0415

    ownership_snapshot = _OWNERSHIP_SNAPSHOT_UNAVAILABLE
    if claim_reads_enabled():
        try:
            from . import ownership  # noqa: PLC0415

            ownership_snapshot = ownership.snapshot()
        except Exception:  # noqa: BLE001
            pass

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
                _runtime_session_for_worktree(worktree["path"], runtime_summary),
                main_repo_root=main_repo_root,
                ownership_snapshot=ownership_snapshot,
            )
        )

    for pr in prs:
        if pr.number in seen_pr_numbers:
            continue
        # A PR whose head branch isn't checked out in any discovered worktree
        # still gets a card. Before falling back to "No worktree", consult the
        # session registry for a LIVE session on this branch — that's the agent
        # actively working a PR spun off a shared parent worktree (the common
        # "No worktree despite active agent" case). When found, the card is
        # attributed to that session's worktree + agent name.
        branch_session = _live_session_for_branch(
            pr.branch, branch_session_summary, allowed_worktree_paths=repo_worktree_paths
        )
        runtime_session = branch_session or _runtime_session_for_worktree(
            pr.worktree_path, runtime_summary
        )
        resolved_worktree_path = pr.worktree_path or (
            branch_session.worktree_path if branch_session else None
        )
        # A resolved path that is itself a hidden agent worktree keeps the card
        # hidden/non-navigable (the branch session may live in one).
        worktree_hidden = bool(resolved_worktree_path and resolved_worktree_path in hidden_worktree_paths)
        active_agents = active_agents_by_path.get(resolved_worktree_path or "", [])
        cards.append(
            _build_unassigned_pr_card(
                pr,
                active_agents=active_agents,
                worktree_hidden=worktree_hidden,
                runtime_session=runtime_session,
                session_worktree_path=resolved_worktree_path if branch_session else None,
                main_repo_root=main_repo_root,
                ownership_snapshot=ownership_snapshot,
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

def _card_status(
    pr: PRData | None, activity: str, reclaimable: bool = False
) -> PRStatus:
    """Route a card to its board column.

    ``activity`` is the tri-state from :func:`_resolve_agent_activity`. A card
    with a PR keeps that PR's status so the column always answers "what does
    this PR need" — the idle-session signal rides on ``session_activity`` and
    shows up on the state chip instead (BOU-2365). Only a worktree with no PR
    is routed by activity alone.
    """
    # A reclaimable worktree is terminal — a lingering chat process must not
    # keep it on the board as live work. A genuinely working agent still wins.
    if reclaimable and activity != "working":
        return PRStatus.READY_CLEANUP
    if pr:
        maintenance_is_active = (
            pr.maintenance is not None
            and pr.maintenance.state in ACTIVE_MAINTENANCE_STATES
        )
        if activity == "working" or maintenance_is_active:
            return PRStatus.AGENT_WORKING
        if pr.status == PRStatus.CLEAN and pr.review_comments:
            return PRStatus.HAS_COMMENTS
        return pr.status
    if activity == "working":
        return PRStatus.AGENT_WORKING
    if activity == "waiting":
        return PRStatus.AGENT_WAITING
    return PRStatus.NO_PR


def _runtime_session_for_worktree(
    worktree_path: str | None,
    default_summary: session_registry.SessionSummary,
) -> session_registry.RuntimeSessionState | None:
    if not worktree_path:
        return None

    candidates = list(
        default_summary.by_worktree_sessions.get(worktree_path)
        or [
            state
            for state in default_summary.sessions.values()
            if state.worktree_path == worktree_path
        ]
    )
    default_registry = session_registry.registry_path()
    worktree_registry = session_registry.registry_path(worktree_path)
    if worktree_registry != default_registry:
        target_summary = session_registry.summarize_sessions(path=worktree_registry)
        candidates.extend(
            target_summary.by_worktree_sessions.get(worktree_path)
            or [
                state
                for state in target_summary.sessions.values()
                if state.worktree_path == worktree_path
            ]
        )
    if not candidates:
        return None
    latest_by_session: dict[str, session_registry.RuntimeSessionState] = {}
    for state in candidates:
        current = latest_by_session.get(state.session_id)
        if current is None or state.timestamp >= current.timestamp:
            latest_by_session[state.session_id] = state
    materialized = list(latest_by_session.values())
    live = [state for state in materialized if _runtime_session_is_live(state)]
    return max(live or materialized, key=lambda state: state.timestamp)


def _repo_session_summary(
    worktrees: list[dict],
    default_summary: session_registry.SessionSummary,
) -> session_registry.SessionSummary:
    """Union of session state across THIS dashboard's registries.

    A worktree can point ``session_registry_path`` elsewhere (per-worktree
    config), so the default registry alone misses its sessions. Merge the
    default summary with each distinct per-worktree registry; the most recent
    event wins per session id. Used for branch attribution so a custom-registry
    repo's live session is still found.
    """
    merged = dict(default_summary.sessions)
    seen_registries = {session_registry.registry_path()}
    for worktree in worktrees:
        registry = session_registry.registry_path(worktree.get("path"))
        if registry in seen_registries:
            continue
        seen_registries.add(registry)
        for sid, state in session_registry.summarize_sessions(path=registry).sessions.items():
            current = merged.get(sid)
            if current is None or state.timestamp >= current.timestamp:
                merged[sid] = state
    return session_registry.SessionSummary(sessions=merged)


def _live_session_for_branch(
    branch: str | None,
    summary: session_registry.SessionSummary,
    allowed_worktree_paths: set[str] | None = None,
) -> session_registry.RuntimeSessionState | None:
    """Most-recent live, non-terminal session whose recorded branch == ``branch``.

    The PR→worktree link in build_worktree_cards is keyed on a branch being the
    *currently checked-out* branch of a discovered worktree (find_worktree_for_branch).
    A PR worked from a shared parent worktree — or whose branch was swapped out
    after the agent started — has no such live checkout, so it falls through to
    an "unassigned" card and reads as "No worktree" even though an agent is
    actively on it. The session registry already knows which session is on which
    branch; consult it so the card shows the owning agent + its worktree.

    ``allowed_worktree_paths`` restricts candidates to sessions whose worktree
    belongs to THIS dashboard's repo, so a same-named branch in an unrelated
    repository can't hijack the card (and focus the wrong path).

    Display-only: unlike active_sessions_for_worktree (which gates PR-maintenance
    deferral on the feature-pipeline marker), we surface ANY live agent on the
    branch — a developer's own Claude session on the branch is still useful to
    show. Liveness is the launcher pid, consistent with the rest of the registry.
    """
    if not branch:
        return None
    candidates = [
        state
        for state in summary.sessions.values()
        if state.branch == branch
        and not state.is_terminal
        and state.launch_source not in session_registry.DASHBOARD_LAUNCH_SOURCES
        and (allowed_worktree_paths is None or state.worktree_path in allowed_worktree_paths)
        and session_registry.pid_is_live(state.pid)
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda state: state.timestamp, reverse=True)[0]


def _terminal_session_matches_active_agents(
    runtime_session: session_registry.RuntimeSessionState | None,
    active_agents: list[AgentProcess],
) -> bool:
    if not runtime_session or not runtime_session.is_terminal:
        return False
    if not active_agents:
        return False
    if runtime_session.pid is None:
        return False
    return any(agent.pid == runtime_session.pid for agent in active_agents)


def _ownership_for_card(
    worktree_path: str | None,
    pr_number: int | None,
    repo_cwd: str,
    *,
    ownership_snapshot=None,
) -> dict:
    """Best-effort ownership/observability info for a WorktreeCard. Never raises.

    Returns a (possibly empty) dict with a subset of:
        owner_session_id, owner_pid, owner_pid_alive, armed_at,
        last_heartbeat_at, loop_state, thread_decisions.
    """
    result: dict = {}

    if worktree_path:
        try:
            from ._maintenance.markers import _read_marker  # noqa: PLC0415
            from ._maintenance._common import _pid_alive, _parse_iso  # noqa: PLC0415
            from ._maintenance.ownership_resolution import resolve_worktree  # noqa: PLC0415

            # Claim-first for identity (BOU-2223 Stage 3); the marker still
            # supplies armed_at/heartbeat, which have no claim equivalent — the
            # claim's lease is a different quantity and must not be shown as one.
            if ownership_snapshot is not _OWNERSHIP_SNAPSHOT_UNAVAILABLE:
                owned = resolve_worktree(
                    worktree_path,
                    kind="card_divergence",
                    snap=ownership_snapshot,
                )
                if owned.session_id:
                    result["owner_session_id"] = owned.session_id
                if owned.owner_pid is not None:
                    result["owner_pid"] = owned.owner_pid
                    result["owner_pid_alive"] = _pid_alive(str(owned.owner_pid))

            marker = _read_marker(worktree_path)
            if marker:
                session_id = marker.get("session_id") or None
                if session_id:
                    result.setdefault("owner_session_id", session_id)
                pid_str = marker.get("pid", "")
                if pid_str.isdigit():
                    result.setdefault("owner_pid", int(pid_str))
                    result.setdefault("owner_pid_alive", _pid_alive(pid_str))
                raw_armed = marker.get("armed_at")
                if raw_armed:
                    dt = _parse_iso(raw_armed)
                    if dt is not None:
                        result["armed_at"] = dt
                for hb_key in ("last_heartbeat", "heartbeat"):
                    raw_hb = marker.get(hb_key)
                    if raw_hb:
                        dt = _parse_iso(raw_hb)
                        if dt is not None:
                            result["last_heartbeat_at"] = dt
                            break
        except Exception:  # noqa: BLE001
            pass

        if pr_number is not None:
            try:
                from . import maintenance as _maintenance  # noqa: PLC0415
                state_obj = _maintenance.load_state(worktree_path, pr_number)
                if state_obj is not None:
                    result["loop_state"] = state_obj.state.value
            except Exception:  # noqa: BLE001
                pass

    if pr_number is not None:
        try:
            from .observability import get_event_store  # noqa: PLC0415
            from .models import ThreadDecision  # noqa: PLC0415

            events = get_event_store(repo_cwd).query(
                pr_number=pr_number, kind="comment_scan", limit=1
            )
            if events:
                decisions_raw = events[0].details.get("decisions", [])
                decisions: list[ThreadDecision] = []
                for raw_d in decisions_raw:
                    try:
                        decisions.append(ThreadDecision.model_validate(raw_d))
                    except Exception:  # noqa: BLE001
                        pass
                if decisions:
                    result["thread_decisions"] = decisions
        except Exception:  # noqa: BLE001
            pass

    return result


def _cached_ownership_for_card(
    worktree_path: str | None,
    pr_number: int | None,
    repo_cwd: str,
    *,
    ownership_snapshot=None,
) -> dict:
    """Share slow claim/event-store reads across dashboard view variants."""
    key = (worktree_path, pr_number, repo_cwd)
    now = time.monotonic()
    with _ownership_card_cache_lock:
        expired = [
            cached_key
            for cached_key, (timestamp, _value) in _ownership_card_cache.items()
            if now - timestamp > _OWNERSHIP_CACHE_TTL_SECONDS
        ]
        for expired_key in expired:
            _ownership_card_cache.pop(expired_key, None)
        cached = _ownership_card_cache.get(key)
        if cached and now - cached[0] <= _OWNERSHIP_CACHE_TTL_SECONDS:
            return cached[1]
        ownership = _ownership_for_card(
            worktree_path=worktree_path,
            pr_number=pr_number,
            repo_cwd=repo_cwd,
            ownership_snapshot=ownership_snapshot,
        )
        _ownership_card_cache[key] = (time.monotonic(), ownership)
        return ownership


def _runtime_card_fields(
    runtime_session: session_registry.RuntimeSessionState | None,
) -> dict[str, object]:
    if runtime_session is None:
        return {}
    return {
        "runtime_session_id": runtime_session.session_id,
        "runtime_chain_id": runtime_session.chain_id,
        "runtime_generation": runtime_session.generation,
        "supervisor_state": runtime_session.supervisor_state,
        "context_percent": runtime_session.context_percent,
        "context_tokens": runtime_session.context_tokens,
        "window_tokens": runtime_session.window_tokens,
        "cumulative_tokens": runtime_session.cumulative_tokens,
        "context_confidence": runtime_session.context_confidence,
        "runtime_quiescence": runtime_session.quiescence,
        "runtime_active_turns": runtime_session.active_turns,
        "runtime_active_tools": runtime_session.active_tools,
        "runtime_active_subagents": runtime_session.active_subagents,
        "runtime_active_critical_sections": runtime_session.active_critical_sections,
        "runtime_checkpoint_fingerprint": runtime_session.checkpoint_fingerprint,
        "runtime_outbox_depth": runtime_session.outbox_depth,
        "runtime_status_stale": _runtime_status_is_stale(runtime_session),
        "agent_name": runtime_session.agent_name,
        "docker_mode": runtime_session.docker_mode,
        "docker_daemon_name": runtime_session.docker_daemon_name,
        "container_names": runtime_session.container_names,
        "runtime_warnings": [runtime_session.warning]
        if runtime_session.warning
        else [],
    }


def _build_card_for_worktree(
    worktree: dict,
    pr: PRData | None,
    active_agents: list[AgentProcess],
    runtime_session: session_registry.RuntimeSessionState | None = None,
    *,
    main_repo_root: str | None = None,
    ownership_snapshot=None,
) -> WorktreeCard:
    fallback_agents = active_agents or _fallback_dashboard_agent(pr)
    # Prefer the turn-activity signal (real "in a turn" state) when the worktree
    # has an activity stamp; fall back to CPU-discovered active_agents otherwise.
    if _dashboard_dispatch_inflight(pr):
        activity = "working"
    elif _terminal_session_matches_active_agents(runtime_session, fallback_agents):
        activity = "none"
    else:
        activity = _resolve_agent_activity(
            worktree.get("path"), bool(fallback_agents), runtime_session
        )
    # Reclaimability is evaluated with NO agents so a lingering chat process
    # can't hide a merged/closed branch; the dirty-tree and protected-worktree
    # guards still apply. `cleanup_candidate` keeps the conservative semantics
    # it has always had, because it arms the destructive cleanup button.
    # Skipped entirely while the agent is working — nothing downstream consumes
    # it in that case, and the probe shells out to `gh` on every board poll.
    reclaimable = False
    root = main_repo_root or get_main_repo_root()
    if (
        pr is None
        and activity != "working"
        and root in orchestrator.observed_roots
    ):
        reclaimable, _ = _selected_worktree_cleanup_reason(
            worktree, [], check_remote_pr=False
        )
    cleanup_candidate = reclaimable and not fallback_agents
    status = _card_status(pr, activity, reclaimable)
    activity_message, activity_source = _card_activity_message(
        pr, fallback_agents, activity == "working"
    )

    # Prefer the PR's creation timestamp as "started_at"; fall back to the
    # worktree directory's birth/ctime when no PR (or PR has no created_at).
    _pr_created_at = (pr.created_at if pr else "") or ""
    if not _pr_created_at:
        _wt_dt = worktree_started_at(worktree.get("path") or "")
        if _wt_dt is not None:
            _pr_created_at = _wt_dt.isoformat().replace("+00:00", "Z")

    _ownership = _cached_ownership_for_card(
        worktree_path=worktree.get("path"),
        pr_number=pr.number if pr else None,
        repo_cwd=root,
        ownership_snapshot=ownership_snapshot,
    )

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
        waiting_decision_id=pr.waiting_decision_id if pr else None,
        waiting_decision_question=pr.waiting_decision_question if pr else None,
        waiting_decision_category=pr.waiting_decision_category if pr else None,
        waiting_decision_runtime=pr.waiting_decision_runtime if pr else None,
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
        session_activity=activity,
        waiting_reason=(
            _waiting_reason(pr, runtime_session) if activity == "waiting" else None
        ),
        last_polled=pr.last_polled if pr else None,
        last_agent_dispatch=pr.last_agent_dispatch if pr else None,
        maintenance=pr.maintenance if pr else None,
        cleanup_candidate=cleanup_candidate,
        escalated=pr.escalated if pr else False,
        escalated_reason=pr.escalated_reason if pr else None,
        pr_created_at=_pr_created_at,
        **_runtime_card_fields(runtime_session),
        **_ownership,
    )


def _build_unassigned_pr_card(
    pr: PRData,
    active_agents: list[AgentProcess] | None = None,
    worktree_hidden: bool = False,
    runtime_session: session_registry.RuntimeSessionState | None = None,
    session_worktree_path: str | None = None,
    main_repo_root: str | None = None,
    ownership_snapshot=None,
) -> WorktreeCard:
    # When a live branch-matched session attributes this PR to a worktree
    # (session_worktree_path), synthesize an agent from it so a PR worked from a
    # shared parent worktree reads as agent-working instead of "No worktree".
    if not active_agents and session_worktree_path and runtime_session:
        active_agents = [
            AgentProcess(
                pid=runtime_session.pid or 0,
                cli_name=runtime_session.cli,
                label=(runtime_session.agent_name or runtime_session.cli.capitalize()),
            )
        ]
    fallback_agents = active_agents or _fallback_dashboard_agent(pr)
    # Turn-activity signal is keyed on the worktree the agent is actually in —
    # the session's worktree when branch-matched, else the PR's own.
    activity_worktree_path = session_worktree_path or pr.worktree_path
    if _dashboard_dispatch_inflight(pr):
        activity = "working"
    elif _terminal_session_matches_active_agents(runtime_session, fallback_agents):
        activity = "none"
    else:
        activity = _resolve_agent_activity(
            activity_worktree_path, bool(fallback_agents), runtime_session
        )
    status = _card_status(pr, activity)
    activity_message, activity_source = _card_activity_message(
        pr, fallback_agents, activity == "working"
    )

    if worktree_hidden:
        worktree_name = "Agent worktree hidden"
    elif session_worktree_path:
        worktree_name = Path(session_worktree_path).name
    else:
        worktree_name = "No worktree"

    # A hidden agent worktree must stay non-navigable: the board template gates
    # click/focus on card.worktree_path *before* worktree_hidden, so exposing the
    # path here would make a supposedly-hidden card focusable. The session path
    # is still used above (activity_worktree_path) for the working/idle signal.
    card_worktree_path = None if worktree_hidden else session_worktree_path

    _ownership = _cached_ownership_for_card(
        worktree_path=session_worktree_path or pr.worktree_path,
        pr_number=pr.number,
        repo_cwd=main_repo_root or get_main_repo_root(),
        ownership_snapshot=ownership_snapshot,
    )

    return WorktreeCard(
        id=f"pr:{pr.number}",
        worktree_name=worktree_name,
        worktree_path=card_worktree_path,
        worktree_hidden=worktree_hidden,
        branch=pr.branch,
        pr_number=pr.number,
        pr_title=pr.title,
        pr_url=pr.url,
        is_draft=pr.is_draft,
        status=status,
        waiting_decision_id=pr.waiting_decision_id,
        waiting_decision_question=pr.waiting_decision_question,
        waiting_decision_category=pr.waiting_decision_category,
        waiting_decision_runtime=pr.waiting_decision_runtime,
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
        session_activity=activity,
        waiting_reason=(
            _waiting_reason(pr, runtime_session) if activity == "waiting" else None
        ),
        last_polled=pr.last_polled,
        last_agent_dispatch=pr.last_agent_dispatch,
        maintenance=pr.maintenance,
        escalated=pr.escalated,
        escalated_reason=pr.escalated_reason,
        pr_created_at=pr.created_at,
        **_runtime_card_fields(runtime_session),
        **_ownership,
    )


def _dashboard_dispatch_inflight(pr: PRData | None) -> bool:
    """True when the dashboard itself has an executor in flight for this PR.

    Unlike process liveness this is an unambiguous "we started work" signal, so
    it resolves to ``working`` even before the executor is CPU-visible or has
    stamped any activity (BOU-2365).
    """
    return bool(pr and pr.number in orchestrator._inflight_prs)


def _fallback_dashboard_agent(pr: PRData | None) -> list[AgentProcess]:
    if pr is None or not _dashboard_dispatch_inflight(pr):
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
        pr = orchestrator.get_pr(card.pr_number)
        if pr and "bug-bash" in pr.labels:
            count += 1
    return count


def _dashboard_context_from_cards(
    cards: list[WorktreeCard],
    worktree_count: int,
    hidden_agent_worktree_count: int,
    *,
    show_agent_worktrees: bool,
    active_tab: str,
    loaded: bool = True,
) -> dict[str, object]:
    runner_summary = orchestrator.weekly_runner_execution_summary
    runner_issues = _runner_issues(cards)
    running_github_jobs = _running_github_jobs(cards)
    desktop_docker_instances = _desktop_docker_instances(cards)
    escalated_prs = [c for c in cards if c.escalated]
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
        "escalated_prs": escalated_prs,
        "active_tab": active_tab if active_tab in VALID_DASHBOARD_TABS else "board",
        "board_tab_url": "/?tab=board&show_agents=1" if show_agent_worktrees else "/?tab=board",
        "runner_issues_tab_url": "/?tab=runner_issues&show_agents=1" if show_agent_worktrees else "/?tab=runner_issues",
        "board_partial_url": "/partials/board?show_agents=1" if show_agent_worktrees else "/partials/board",
        "runner_issues_partial_url": "/partials/runner-issues?show_agents=1" if show_agent_worktrees else "/partials/runner-issues",
        "worktrees_tab_url": "/?tab=worktrees&show_agents=1" if show_agent_worktrees else "/?tab=worktrees",
        "worktrees_partial_url": "/partials/worktrees?show_agents=1" if show_agent_worktrees else "/partials/worktrees",
        "no_pr_cards": no_pr_cards(cards),
        "quota": _quota_context(orchestrator.quota_telemetry),
        "asset_version": _asset_version(),
        # An empty board asserts "you have no PRs", which is the most wrong
        # thing this dashboard can say; the template renders a loading state
        # instead (BOU-3095).
        #
        # It takes BOTH the local card scan finishing and GitHub actually having
        # been observed. The local scan is independent of GitHub, so a daemon
        # starting during an outage would otherwise complete its scan, mark the
        # board loaded, and render confident "No worktrees" columns for the whole
        # outage — the false-empty board arriving through a second door
        # (PR #169 review round 4).
        # ``complete`` as well as ``observed_at``: in a multi-repo deployment the
        # anchor can be observed while a configured sibling is not, and an empty
        # board is then not an answer about the watched set — it is an answer
        # about the half that replied (round-8 review). Cards still render
        # either way; this only governs the count and the empty-state text.
        "board_loaded": loaded and _open_set_is_complete(),
        "observation": _observation_context(),
    }


def dashboard_context(show_agent_worktrees: bool = False, active_tab: str = "board") -> dict[str, object]:
    cards, worktree_count, hidden_agent_worktree_count = build_worktree_cards(show_agent_worktrees=show_agent_worktrees)
    return _dashboard_context_from_cards(
        cards,
        worktree_count,
        hidden_agent_worktree_count,
        show_agent_worktrees=show_agent_worktrees,
        active_tab=active_tab,
    )


def runner_fleet_context() -> dict[str, object]:
    return {"runner_fleet": get_cached_runner_fleet_load(cwd=get_main_repo_root())}


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
        "worktrees_tab_url": "/?tab=worktrees&show_agents=1" if show_agent_worktrees else "/?tab=worktrees",
        "worktrees_partial_url": "/partials/worktrees?show_agents=1" if show_agent_worktrees else "/partials/worktrees",
        "no_pr_cards": [],
        "board_tab_url": "/?tab=board&show_agents=1" if show_agent_worktrees else "/?tab=board",
        "runner_issues_tab_url": "/?tab=runner_issues&show_agents=1" if show_agent_worktrees else "/?tab=runner_issues",
        "board_partial_url": "/partials/board?show_agents=1" if show_agent_worktrees else "/partials/board",
        "runner_issues_partial_url": "/partials/runner-issues?show_agents=1" if show_agent_worktrees else "/partials/runner-issues",
        "asset_version": _asset_version(),
        # This tab builds its own context rather than going through
        # _dashboard_context_from_cards, so the shared header include would
        # otherwise fall back to "PR data loading" forever on it (BOU-3095
        # PR #169 review). The board's out-of-band swap does not reach this tab,
        # so the value is correct at render and refreshes on the tab's own poll.
        "observation": _observation_context(),
        # Same rule as the board: this tab's panels are equally wrong if they
        # render as authoritative before GitHub has been observed.
        "board_loaded": _open_set_is_complete(),
    }


def _canonical_dashboard_tab(active_tab: str) -> str:
    return active_tab if active_tab in VALID_DASHBOARD_TABS else "board"


async def _dashboard_context_async(
    show_agent_worktrees: bool = False,
    active_tab: str = "board",
) -> dict[str, object]:
    active_tab = _canonical_dashboard_tab(active_tab)
    key = (show_agent_worktrees, active_tab)
    now = time.monotonic()
    cached = _dashboard_context_cache.get(key)
    if cached is None:
        # A cold process has no materialized worktree/session snapshot yet.
        # Serve a valid board skeleton immediately and let the normal
        # stale-cache path populate it in the background. Browser polling
        # replaces this skeleton as soon as discovery completes, while a slow
        # local scan can no longer make the dashboard look dead after an
        # upgrade/restart.
        #
        # The skeleton is marked NOT loaded (BOU-3095): rendered as a normal
        # empty board it claimed "no worktrees" in every column, so a forced
        # refresh — which clears this cache — made the dashboard assert zero
        # PRs for as long as the rebuild took.
        cached = (
            _DASHBOARD_CONTEXT_STALE_AT,
            _dashboard_context_from_cards(
                [],
                0,
                0,
                show_agent_worktrees=show_agent_worktrees,
                active_tab=active_tab,
                loaded=False,
            ),
        )
        _dashboard_context_cache[key] = cached
    if cached and now - cached[0] <= CONTEXT_CACHE_TTL_SECONDS:
        return cached[1]

    task = _dashboard_context_tasks.get(key)
    if task is None or task.done():
        context_func = runner_dashboard_context if active_tab == "runner_issues" else dashboard_context
        build_generation = _dashboard_context_generation

        async def rebuild() -> dict[str, object]:
            try:
                context = await asyncio.to_thread(
                    context_func,
                    show_agent_worktrees=show_agent_worktrees,
                    active_tab=active_tab,
                )
            except Exception as exc:  # noqa: BLE001 — a failed rebuild must be visible
                # Nothing awaits this task once a cached context exists, so an
                # exception here was silently swallowed and the cold skeleton
                # could persist across polls with no explanation (BOU-3095).
                orchestrator.log(
                    f"Dashboard context rebuild failed: {exc}", level="error"
                )
                # Only clear the entry if it is still OURS. A refresh may have
                # cleared the map and a later poll installed a replacement;
                # popping that would start duplicate scans and strand the
                # replacement's result (BOU-3095 PR #169 review).
                if _dashboard_context_tasks.get(key) is asyncio.current_task():
                    _dashboard_context_tasks.pop(key, None)
                raise
            current_task = asyncio.current_task()
            if _dashboard_context_tasks.get(key) is current_task:
                _dashboard_context_tasks.pop(key, None)
                timestamp = (
                    time.monotonic()
                    if build_generation == _dashboard_context_generation
                    else _DASHBOARD_CONTEXT_STALE_AT
                )
                _dashboard_context_cache[key] = (timestamp, context)
            return context

        task = asyncio.create_task(rebuild())
        _dashboard_context_tasks[key] = task

    # Building cards inspects worktrees, processes, session logs, and ownership
    # records. That can take several seconds on a busy workstation. Once a
    # context exists, serve it immediately while one shared background task
    # rebuilds it; otherwise every five-second HTMX poll blocks on the scan and
    # the board appears frozen despite fresh orchestrator state.
    if cached:
        return cached[1]
    return await task


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
                agent_name="cobalt-fox",
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
                runtime_chain_id="pr-421-maintenance",
                runtime_generation=2,
                supervisor_state="warning",
                context_percent=67.5,
                context_tokens=675_000,
                window_tokens=1_000_000,
                cumulative_tokens=9_500_000,
                context_confidence="confident",
                runtime_quiescence="busy",
                runtime_active_turns=1,
                runtime_active_tools=1,
                runtime_active_subagents=0,
                runtime_active_critical_sections=0,
                runtime_checkpoint_fingerprint="abc123",
                runtime_outbox_depth=0,
                latest_commit_sha="b7c8d9e",
                latest_commit_date="2026-05-25T15:09:00Z",
                last_updated_label="30s ago",
            ),
            # Column 3 — Waiting: a worktree with no PR whose session is alive
            # but between turns. Liveness is not work (BOU-2365).
            WorktreeCard(
                id="demo-waiting",
                worktree_name="invoice-export",
                worktree_path="/home/dev/worktrees/invoice-export",
                branch="feature/invoice-export",
                environment_name="invoice-export",
                frontend_port="3004",
                backend_port="8040",
                slot="4",
                status=PRStatus.AGENT_WAITING,
                session_activity="waiting",
                waiting_reason="user input",
                ci_checks=[CICheck(name="unit", status="in_progress")],
                active_agents=[AgentProcess(pid=4343, cli_name="claude", label="Claude")],
                agent_name="amber-heron",
                activity_message="Claude watching",
                activity_source="local",
                supervisor_state="running",
                runtime_quiescence="idle",
                latest_commit_sha="c3d4e5f",
                latest_commit_date="2026-05-25T15:05:00Z",
                last_updated_label="7m ago",
            ),
            # Column 4 — Ready / Cleanup: merged, worktree reclaimable, and the
            # chat process is still draining. Terminal, not working.
            WorktreeCard(
                id="demo-ready-cleanup",
                worktree_name="local-agent-linear",
                worktree_path="/home/dev/worktrees/local-agent-linear",
                branch="feature/local-agent-linear",
                environment_name="local-agent-linear",
                frontend_port="3005",
                backend_port="8050",
                slot="5",
                status=PRStatus.READY_CLEANUP,
                cleanup_candidate=True,
                session_activity="waiting",
                agent_name="fleet-mantis",
                supervisor_state="draining",
                runtime_quiescence="idle",
                latest_commit_sha="e5f6a7b",
                latest_commit_date="2026-05-25T14:40:00Z",
                last_updated_label="32m ago",
            ),
            # Column 5 — Clean: ready to merge.
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
    active_tab = active_tab if active_tab in VALID_DASHBOARD_TABS else "board"
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
        "worktrees_tab_url": f"/proof/pr-dashboard-fixture/{scenario}?tab=worktrees",
        "board_partial_url": f"/proof/pr-dashboard-fixture/{scenario}/board",
        "runner_issues_partial_url": f"/proof/pr-dashboard-fixture/{scenario}/runner-issues",
        # Without a fixture-scoped partial url the tab's 5s poll would fall back
        # to the PRODUCTION /partials/worktrees route, which performs real
        # repository discovery — replacing a deterministic, no-network fixture
        # with live data seconds after it loads (PR #114 review).
        "worktrees_partial_url": f"/proof/pr-dashboard-fixture/{scenario}/worktrees",
        "no_pr_cards": no_pr_cards(cards),
        "quota": _quota_context(orchestrator.quota_telemetry),
        "asset_version": _asset_version(),
    }


# -- Jinja2 filters --

def status_label(status: PRStatus) -> str:
    return {
        PRStatus.CLEAN: "OK",
        PRStatus.OBSERVATION_UNAVAILABLE: "GitHub Unavailable",
        PRStatus.NO_PR: "No PR",
        PRStatus.CI_PENDING: "CI Pending",
        PRStatus.CI_FAILING: "CI Failing",
        PRStatus.HAS_COMMENTS: "Comments",
        PRStatus.CI_AND_COMMENTS: "CI + Comments",
        PRStatus.MERGE_CONFLICT: "Conflicts",
        PRStatus.AGENT_WORKING: "Agent Working",
        PRStatus.AGENT_WAITING: "Waiting",
        PRStatus.READY_CLEANUP: "Ready / Cleanup",
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
    context["quota"] = _quota_context(orchestrator.quota_telemetry)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=context,
    )


@app.get("/api/quota")
async def quota_api():
    return _quota_context(orchestrator.quota_telemetry)


@app.get("/partials/quota", response_class=HTMLResponse)
async def quota_partial(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="partials/quota_status.html",
        context={"quota": _quota_context(orchestrator.quota_telemetry)},
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


@app.get("/proof/pr-dashboard-fixture/{scenario}/worktrees", response_class=HTMLResponse)
async def pr_dashboard_proof_fixture_worktrees(request: Request, scenario: str):
    if scenario not in {"baseline", "diagnostic"}:
        return Response("Unknown PR-dashboard proof fixture", status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="partials/worktrees.html",
        context=_proof_fixture_context(scenario, active_tab="worktrees"),
    )


@app.get("/partials/board", response_class=HTMLResponse)
async def board_partial(request: Request):
    show_agent_worktrees = _show_agent_worktrees(request)
    ctx = await _dashboard_context_async(show_agent_worktrees=show_agent_worktrees)
    # board_oob: emit the out-of-band header swaps only for the HTMX partial
    # poll, so the title-bar banner refreshes with the board (the full-page
    # include must NOT duplicate the slot id) — codex PR #50 review.
    #
    # Copy first. ``_dashboard_context_async`` returns the CACHED dict, so
    # setting the flag on it in place leaked board_oob=True into every later
    # full-page render — which emitted the slot twice, and a duplicate id makes
    # htmx swap the wrong one (BOU-3095). Observed in the browser as the
    # observation-age indicator rendering both in the header and inside the
    # board.
    ctx = _with_header_oob(ctx, board_oob=True)
    return templates.TemplateResponse(
        request=request,
        name="partials/board.html",
        context=ctx,
    )


@app.get("/partials/runner-issues", response_class=HTMLResponse)
async def runner_issues_partial(request: Request):
    show_agent_worktrees = _show_agent_worktrees(request)
    ctx = await _dashboard_context_async(
        show_agent_worktrees=show_agent_worktrees,
        active_tab="runner_issues",
    )
    ctx = _with_header_oob(ctx)
    return templates.TemplateResponse(
        request=request,
        name="partials/runner_issues.html",
        context=ctx,
    )


@app.get("/partials/worktrees", response_class=HTMLResponse)
async def worktrees_partial(request: Request):
    show_agent_worktrees = _show_agent_worktrees(request)
    ctx = await _dashboard_context_async(
        show_agent_worktrees=show_agent_worktrees,
        active_tab="worktrees",
    )
    return templates.TemplateResponse(
        request=request,
        name="partials/worktrees.html",
        context=_with_header_oob(ctx),
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
    pr = orchestrator.get_pr(pr_number)
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
    pr = orchestrator.get_pr(pr_number)
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
    await orchestrator.refresh_prs(force=True)
    _invalidate_dashboard_context()
    return RedirectResponse(url="/", status_code=303)


async def _read_webhook_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_WEBHOOK_BODY_BYTES:
                raise WebhookRejected(413, "GitHub webhook payload is too large")
        except ValueError:
            pass

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_WEBHOOK_BODY_BYTES:
            raise WebhookRejected(413, "GitHub webhook payload is too large")
    return bytes(body)


@app.post("/api/github/webhook")
async def github_webhook(request: Request):
    """Accept authenticated GitHub observation events without blocking on refresh."""

    try:
        body = await _read_webhook_body(request)
        status_code = webhook_ingress.accept(
            request.headers.get("x-github-event", ""),
            request.headers.get("x-github-delivery"),
            request.headers.get("x-hub-signature-256"),
            body,
        )
    except WebhookRejected as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return Response(status_code=status_code)


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

    # Liveness, not activity — an idle or stopped occupant still owns this
    # checkout, and a scan we could not run is not proof that nobody is home
    # (BOU-2933). Same gate the loop's autonomous reaper uses.
    try:
        occupants = worktree_occupants([path]).get(path, [])
    except ProcessScanUnavailable as exc:
        orchestrator.log(f"Cleanup blocked for {Path(path).name}: {exc}", level="error")
        return _cleanup_response(
            request, '<span class="card-warning">Cannot cleanup: process scan unavailable</span>', 409
        )
    if occupants:
        # Name them: the gate now counts any live process in the tree, so a bare
        # "active agent detected" would read as a false positive to someone
        # looking at a worktree whose only occupant is a tunnel or a test run.
        summary = ", ".join(f"{o.label} ({o.pid})" for o in occupants[:3])
        if len(occupants) > 3:
            summary += f" +{len(occupants) - 3} more"
        return _cleanup_response(
            request,
            f'<span class="card-warning">Cannot cleanup: worktree in use by {escape(summary)}</span>',
            409,
        )

    eligible, reason = _selected_worktree_cleanup_reason(worktree, occupants)
    if not eligible:
        return _cleanup_response(request, f'<span class="card-warning">Cannot cleanup: {reason}</span>', 409)

    # Shared with the loop's reaper so both paths get the same registration-based
    # post-check — `Path(path).exists()` reported a successful removal as a
    # failure once the guardian recreated its scaffolding dirs (BOU-2933).
    removed, detail = remove_worktree(path)
    if not removed:
        orchestrator.log(f"Cleanup failed for {Path(path).name}: {detail}", level="error")
        return _cleanup_response(request, '<span class="card-warning">Cleanup failed</span>', 500)

    _invalidate_dashboard_context()
    orchestrator.log(f"Cleanup requested for {Path(path).name}: {reason}")
    return _cleanup_response(request, '<span class="card-clean-status">Cleanup requested</span>', 200)


@app.post("/api/focus-worktree")
async def focus_worktree(path: str = Form(...)):
    if not path:
        return Response(status_code=400)
    if focus_or_open_worktree(path):
        return Response(status_code=204)
    return Response(status_code=404)
