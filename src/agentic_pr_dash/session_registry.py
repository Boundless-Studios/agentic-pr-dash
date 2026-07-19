"""Durable runtime session event registry for agent/worktree activity.

Agent launchers can emit ``started``, ``exited``, and activity events into this
JSONL registry. The dashboard reads the registry to answer "is an agent already
working in this worktree?" without guessing from PR state alone. The default
paths preserve Gaia compatibility, but the registry is configured through
``agentic-pr-dash`` settings and is not Gaia-specific.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import load as load_config


def _env(name: str, default: str = "") -> str:
    """Prefer AGENTIC_PR_DASH_<name>, fall back to GAIA_<name>."""
    return os.environ.get("AGENTIC_PR_DASH_" + name) or os.environ.get("GAIA_" + name) or default


_NEW_DEFAULT_REGISTRY = Path.home() / ".agentic-pr-dash" / "sessions" / "events.jsonl"
_LEGACY_DEFAULT_REGISTRY = Path.home() / ".gaia" / "sessions" / "events.jsonl"


def _default_registry(cwd: str | None = None) -> Path:
    """Return the default registry path, preferring legacy if it already exists.

    ``cwd`` selects which ``agentic-pr-dash.toml`` provides a configured
    ``session_registry_path``. Pass the TARGET worktree's dir when resolving the
    registry on its behalf — reading the process cwd's config instead would miss
    a repo that points its registry elsewhere (PR #7 review, P2).
    """
    override = _env("SESSION_REGISTRY")
    if override:
        return Path(override).expanduser()
    # Honor a configured session_registry_path from config (if set)
    cfg_path = load_config(cwd).session_registry_path
    if cfg_path is not None:
        return cfg_path
    # Prefer legacy path if it exists so existing installs keep working
    if _LEGACY_DEFAULT_REGISTRY.exists():
        return _LEGACY_DEFAULT_REGISTRY
    return _NEW_DEFAULT_REGISTRY


DEFAULT_REGISTRY = _NEW_DEFAULT_REGISTRY

# Upper bound on how many trailing event lines ``summarize_sessions`` /
# ``read_events`` will parse when no explicit limit is given (BOU-1637). The
# registry is append-only, so on a long-lived machine the file grows without
# bound and every dashboard tick re-parsed the whole thing. Reading only the
# tail keeps each summarize O(cap) instead of O(file); the most recent events
# for a session are always the ones that decide its current state, and
# ``compact_registry`` keeps the live tail small anyway. Override with
# AGENTIC_PR_DASH_REGISTRY_READ_LIMIT (0/blank disables the cap).
_DEFAULT_READ_LIMIT = 20000

# Terminal sessions older than this are dropped by ``compact_registry`` — a
# completed/failed session's events have no bearing on "who is working now".
# Override with AGENTIC_PR_DASH_REGISTRY_RETENTION_SECONDS.
_DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60  # 7 days

# When the registry crosses this many lines, ``record_event`` opportunistically
# self-compacts (drops old terminal sessions). Override / disable (0) via
# AGENTIC_PR_DASH_REGISTRY_COMPACT_THRESHOLD.
_DEFAULT_COMPACT_THRESHOLD = 5000
_MAX_STATUS_REPORT_BYTES = 1_048_576
_STATUS_DEDUPE_TOMBSTONES_PER_SESSION = 256

_PRIVATE_REPORT_FIELDS = {
    "api_key",
    "api_keys",
    "authorization",
    "credential",
    "credentials",
    "env",
    "environment",
    "environment_variables",
    "message",
    "messages",
    "password",
    "passwords",
    "prompt",
    "prompts",
    "prompt_text",
    "raw_prompt",
    "raw_transcript",
    "transcript",
    "transcripts",
    "transcript_body",
    "tool_input",
    "tool_inputs",
    "tool_output",
    "tool_outputs",
    "tool_arguments",
    "secret",
    "secrets",
    "secret_value",
}

# Launch sources that are the dashboard's OWN automation, not an independent
# session whose worktree we should defer to.
DASHBOARD_LAUNCH_SOURCES = ("agentic-pr-dash", "pr-dashboard")


class HarnessActiveCounts(BaseModel):
    """Forward-compatible copy of ``StatusReport.active`` schema v1."""

    model_config = ConfigDict(extra="allow", frozen=True)

    turns: int = Field(ge=0)
    tools: int = Field(ge=0)
    subagents: int = Field(ge=0)
    critical_sections: int = Field(ge=0)


class HarnessStatusReport(BaseModel):
    """Local wire model for ``agent_session_harness.report.StatusReport`` v1.

    The harness does not yet have an immutable release for the dashboard to
    depend on. Keeping the small published JSON contract local avoids a mutable
    Git dependency while allowing additive fields in future v1 producers.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    schema_version: Literal[1]
    event_id: str | None = Field(default=None, min_length=1, max_length=240)
    runtime: Literal["claude", "codex"]
    state: str = Field(min_length=1, max_length=64)
    chain_id: str = Field(min_length=1, max_length=240)
    conversation_id: str | None
    generation: int = Field(ge=0)
    context_percent: float | None = Field(ge=0)
    context_tokens: int | None = Field(ge=0)
    window_tokens: int | None = Field(gt=0)
    cumulative_tokens: int | None = Field(ge=0)
    confidence: Literal["confident", "degraded", "unknown"]
    quiescence: Literal["idle", "busy", "unknown"]
    active: HarnessActiveCounts
    checkpoint_fingerprint: str | None
    outbox_depth: int = Field(ge=0)


def _read_limit() -> int | None:
    """Default tail cap for an unbounded read, or None when disabled via env."""
    raw = os.environ.get("AGENTIC_PR_DASH_REGISTRY_READ_LIMIT", "")
    if raw == "":
        return _DEFAULT_READ_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_READ_LIMIT
    return value if value > 0 else None


@dataclass
class RuntimeSessionState:
    session_id: str
    event: str
    timestamp: str
    cli: str = "unknown"
    agent_name: str | None = None
    launch_source: str = "unknown"
    pid: int | None = None
    ppid: int | None = None
    worktree_path: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    bead_id: str | None = None
    docker_mode: str = "unknown"
    docker_host: str | None = None
    docker_daemon_name: str | None = None
    docker_context: str | None = None
    container_names: list[str] = field(default_factory=list)
    ports: dict[str, str] = field(default_factory=dict)
    exit_code: int | None = None
    failure_reason: str | None = None
    is_feature_pipeline: bool = False
    chain_id: str | None = None
    generation: int | None = None
    supervisor_state: str | None = None
    context_percent: float | None = None
    context_tokens: int | None = None
    window_tokens: int | None = None
    cumulative_tokens: int | None = None
    context_confidence: str | None = None
    quiescence: str | None = None
    active_turns: int = 0
    active_tools: int = 0
    active_subagents: int = 0
    active_critical_sections: int = 0
    checkpoint_fingerprint: str | None = None
    outbox_depth: int = 0
    harness_reported_at: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.event in {"completed", "failed", "cleanup_completed"}

    @property
    def warning(self) -> str | None:
        if self.docker_mode == "local-fallback":
            return "Docker is running locally after remote fallback"
        if self.event == "orphan_detected":
            return "Session appears orphaned"
        return None


@dataclass
class SessionSummary:
    sessions: dict[str, RuntimeSessionState] = field(default_factory=dict)
    by_worktree: dict[str, RuntimeSessionState] = field(default_factory=dict)
    by_worktree_sessions: dict[str, list[RuntimeSessionState]] = field(
        default_factory=dict
    )

    def reindex(self) -> None:
        grouped: dict[str, list[RuntimeSessionState]] = {}
        for state in self.sessions.values():
            if state.worktree_path:
                grouped.setdefault(state.worktree_path, []).append(state)
        for states in grouped.values():
            states.sort(key=lambda item: item.timestamp, reverse=True)
        self.by_worktree_sessions = grouped
        self.by_worktree = {
            worktree: states[0]
            for worktree, states in grouped.items()
            if states
        }

    @property
    def recent(self) -> list[RuntimeSessionState]:
        return sorted(self.sessions.values(), key=lambda item: item.timestamp, reverse=True)


def registry_path(cwd: str | None = None) -> Path:
    return _default_registry(cwd)


@contextmanager
def _registry_write_lock(target: Path) -> Iterator[None]:
    """Serialize registry append/dedupe/compaction across local processes."""
    try:
        import fcntl  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - supported hosts are POSIX
        raise RuntimeError(
            "exclusive session-registry locking is unavailable"
        ) from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f".{target.name}.lock")
    with lock_path.open("a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def new_session_id(prefix: str = "gaia") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item for item in value.split(",") if item]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _coerce_dict(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(key): str(val) for key, val in value.items() if val is not None}
    if isinstance(value, str):
        result: dict[str, str] = {}
        for item in value.split(","):
            if not item:
                continue
            if "=" in item:
                key, val = item.split("=", 1)
                result[key] = val
        return result
    return {}


def _clean_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in (None, [], {})}


def _git_branch(worktree_path: str | None) -> str | None:
    if not worktree_path:
        return None
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=worktree_path,
            text=True,
            capture_output=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    branch = result.stdout.strip()
    return branch or None


def record_event(
    *,
    event: str,
    session_id: str | None = None,
    cli: str | None = None,
    agent_name: str | None = None,
    launch_source: str | None = None,
    pid: int | None = None,
    ppid: int | None = None,
    worktree_path: str | None = None,
    branch: str | None = None,
    pr_number: int | None = None,
    bead_id: str | None = None,
    docker_mode: str | None = None,
    docker_host: str | None = None,
    docker_daemon_name: str | None = None,
    docker_context: str | None = None,
    container_names: list[str] | str | None = None,
    ports: dict[str, str] | str | None = None,
    exit_code: int | None = None,
    failure_reason: str | None = None,
    feature_pipeline: bool | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Append a session event to the registry and return the serialized event."""
    target = path or registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    resolved_worktree = worktree_path or _env("PROJECT_DIR") or os.getcwd()
    resolved_branch = branch or _git_branch(resolved_worktree)
    payload = _clean_payload(
        {
            "event_id": uuid.uuid4().hex,
            "session_id": session_id or _env("SESSION_ID") or new_session_id(),
            "event": event,
            "timestamp": _utc_now(),
            "cli": cli or _env("SESSION_CLI") or "unknown",
            "agent_name": agent_name or _env("AGENT_NAME") or None,
            "launch_source": launch_source
            or _env("SESSION_LAUNCH_SOURCE")
            or "unknown",
            "pid": pid,
            "ppid": ppid,
            "worktree_path": resolved_worktree,
            "branch": resolved_branch,
            "pr_number": pr_number,
            "bead_id": bead_id,
            "docker_mode": docker_mode or _env("DOCKER_MODE") or "unknown",
            "docker_host": docker_host or _env("DOCKER_SELECTED_HOST") or None,
            "docker_daemon_name": docker_daemon_name
            or _env("DOCKER_DAEMON_NAME")
            or None,
            "docker_context": docker_context or os.environ.get("DOCKER_CONTEXT") or None,
            "container_names": _coerce_list(container_names),
            "ports": _coerce_dict(ports),
            "exit_code": exit_code,
            "failure_reason": failure_reason,
            "feature_pipeline": (
                feature_pipeline
                if feature_pipeline is not None
                else (_env("SESSION_FEATURE_PIPELINE") == "1")
            )
            or None,
        }
    )
    with _registry_write_lock(target):
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
    # Amortized self-compaction (BOU-1637): once the registry crosses a line
    # threshold, drop old terminal sessions so the append-only file can't grow
    # without bound. Best-effort and atomic — a failure here never blocks the
    # event write that just succeeded. Disabled by setting the threshold to 0.
    try:
        _maybe_compact(target)
    except Exception:  # noqa: BLE001 — compaction is opportunistic, never fatal
        pass
    return payload


def _private_report_field(value: object) -> str | None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            if key in _PRIVATE_REPORT_FIELDS:
                return key
            found = _private_report_field(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _private_report_field(child)
            if found:
                return found
    return None


def _parse_status_report(payload: dict[str, Any]) -> HarnessStatusReport:
    private_field = _private_report_field(payload)
    if private_field:
        raise ValueError(f"private field is not allowed in StatusReport: {private_field}")
    try:
        return HarnessStatusReport.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"invalid StatusReport: {exc}") from exc


def _status_event_id(
    producer_event_id: str | None,
    worktree_path: str,
    session_id: str,
    chain_id: str,
    generation: int,
) -> str:
    source_id = producer_event_id or uuid.uuid4().hex
    canonical = json.dumps(
        {
            "chain_id": chain_id,
            "generation": generation,
            "producer_event_id": source_id,
            "session_id": session_id,
            "worktree_path": worktree_path,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "harness-status-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _registry_event_by_id(target: Path, event_id: str) -> dict[str, Any] | None:
    if not target.exists():
        return None
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                existing = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(existing, dict) and existing.get("event_id") == event_id:
                return existing
    return None


def record_status_report(
    payload: dict[str, Any],
    *,
    worktree_path: str | None = None,
    branch: str | None = None,
    pid: int | None = None,
    agent_name: str | None = None,
    launch_source: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Validate and append one canonical harness ``StatusReport`` projection."""
    report = _parse_status_report(payload)
    resolved_worktree = str(
        Path(worktree_path or _env("PROJECT_DIR") or os.getcwd())
        .expanduser()
        .resolve()
    )
    target = path or registry_path(resolved_worktree)
    timestamp = _utc_now()
    session_id = report.conversation_id or (
        f"{report.chain_id}:generation:{report.generation}"
    )
    event = _clean_payload(
        {
            "event_id": _status_event_id(
                report.event_id,
                resolved_worktree,
                session_id,
                report.chain_id,
                report.generation,
            ),
            "session_id": session_id,
            "event": "harness_status",
            "timestamp": timestamp,
            "cli": report.runtime,
            "agent_name": agent_name,
            "launch_source": launch_source,
            "pid": pid,
            "worktree_path": resolved_worktree,
            "branch": branch or _git_branch(resolved_worktree),
            "chain_id": report.chain_id,
            "generation": report.generation,
            "supervisor_state": report.state,
            "context_percent": report.context_percent,
            "context_tokens": report.context_tokens,
            "window_tokens": report.window_tokens,
            "cumulative_tokens": report.cumulative_tokens,
            "context_confidence": report.confidence,
            "quiescence": report.quiescence,
            "active_turns": report.active.turns,
            "active_tools": report.active.tools,
            "active_subagents": report.active.subagents,
            "active_critical_sections": report.active.critical_sections,
            "checkpoint_fingerprint": report.checkpoint_fingerprint,
            "outbox_depth": report.outbox_depth,
            "harness_reported_at": timestamp,
            "idempotency_keyed": report.event_id is not None,
        }
    )
    event_id = str(event["event_id"])
    with _registry_write_lock(target):
        if report.event_id is not None:
            existing = _registry_event_by_id(target, event_id)
            if existing is not None:
                return existing
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    try:
        _maybe_compact(target)
    except Exception:  # noqa: BLE001 - report persistence already succeeded
        pass
    return event


def _compact_threshold() -> int:
    raw = os.environ.get("AGENTIC_PR_DASH_REGISTRY_COMPACT_THRESHOLD", "")
    if raw == "":
        return _DEFAULT_COMPACT_THRESHOLD
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_COMPACT_THRESHOLD
    return value  # 0 disables auto-compaction


def _maybe_compact(target: Path) -> None:
    """Run ``compact_registry`` when the file exceeds the line threshold."""
    threshold = _compact_threshold()
    if threshold <= 0:
        return
    try:
        # Counting newlines is cheaper than json-parsing every line.
        with target.open("rb") as fh:
            line_count = sum(1 for _ in fh)
    except OSError:
        return
    if line_count >= threshold:
        compact_registry(path=target)


def record_event_from_env(event: str, **kwargs: Any) -> dict[str, Any]:
    pid = kwargs.pop("pid", None)
    ppid = kwargs.pop("ppid", None)
    return record_event(
        event=event,
        session_id=_env("SESSION_ID") or None,
        cli=_env("SESSION_CLI") or None,
        launch_source=_env("SESSION_LAUNCH_SOURCE") or None,
        pid=int(pid or os.getpid()),
        ppid=int(ppid or os.getppid()),
        worktree_path=_env("PROJECT_DIR") or os.getcwd(),
        **kwargs,
    )


def read_events(path: Path | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    target = path or registry_path()
    if not target.exists():
        return []
    lines = target.read_text(encoding="utf-8").splitlines()
    # An explicit ``limit`` wins; otherwise apply the default tail cap so an
    # unbounded registry on a long-lived machine doesn't make every read O(file)
    # (BOU-1637). The cap can be disabled via env for callers that truly need the
    # whole history.
    effective = limit if limit is not None else _read_limit()
    if effective is not None:
        lines = lines[-effective:]
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _merge_event(state: RuntimeSessionState | None, event: dict[str, Any]) -> RuntimeSessionState:
    incoming_event = str(event.get("event") or "unknown")
    if (
        state is not None
        and state.is_terminal
        and incoming_event
        not in {"started", "completed", "failed", "cleanup_completed"}
    ):
        return state
    if state is None:
        state = RuntimeSessionState(
            session_id=str(event.get("session_id") or ""),
            event=str(event.get("event") or "unknown"),
            timestamp=str(event.get("timestamp") or ""),
        )
    state.event = str(event.get("event") or state.event)
    state.timestamp = str(event.get("timestamp") or state.timestamp)
    for field_name in (
        "cli",
        "agent_name",
        "launch_source",
        "worktree_path",
        "branch",
        "docker_mode",
        "docker_host",
        "docker_daemon_name",
        "docker_context",
        "failure_reason",
    ):
        value = event.get(field_name)
        if value not in (None, "", [], {}):
            setattr(state, field_name, value)
    # The session pid is the long-lived launcher/agent pid recorded by the
    # `started` event. Later events — notably the codex session-start hook's
    # `heartbeat`, which records its own short-lived subprocess pid — must NOT
    # overwrite it, or the dashboard's liveness gate would probe a dead hook
    # pid and filter out an actually-live session. A fresh `started` (session
    # ids are reused across worktree-console relaunches) does update it.
    if (
        str(event.get("event")) == "started"
        or (str(event.get("event")) == "harness_status" and state.pid is None)
    ) and event.get("pid") is not None:
        state.pid = int(event["pid"])
    for field_name in ("ppid", "pr_number", "exit_code"):
        value = event.get(field_name)
        if value is not None:
            setattr(state, field_name, int(value))
    if event.get("feature_pipeline"):
        # Sticky: once a session is known to be a feature-pipeline run, later
        # events that omit the marker don't clear it.
        state.is_feature_pipeline = True
    if event.get("bead_id"):
        state.bead_id = str(event["bead_id"])
    if event.get("container_names"):
        state.container_names = _coerce_list(event["container_names"])
    if event.get("ports"):
        state.ports = _coerce_dict(event["ports"])
    if incoming_event == "harness_status":
        for field_name in (
            "chain_id",
            "supervisor_state",
            "context_confidence",
            "quiescence",
            "checkpoint_fingerprint",
            "harness_reported_at",
        ):
            value = event.get(field_name)
            if value not in (None, ""):
                setattr(state, field_name, str(value))
        for field_name in (
            "generation",
            "context_tokens",
            "window_tokens",
            "cumulative_tokens",
            "active_turns",
            "active_tools",
            "active_subagents",
            "active_critical_sections",
            "outbox_depth",
        ):
            value = event.get(field_name)
            if value is not None:
                setattr(state, field_name, int(value))
        if event.get("context_percent") is not None:
            state.context_percent = float(event["context_percent"])
    return state


def pid_is_live(pid: int | None) -> bool:
    """True if a process with this pid currently exists.

    `os.kill(pid, 0)` sends no signal; it only probes for existence. A
    PermissionError means the process exists but is owned by another user
    (still "live" for our purposes).
    """
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def active_sessions_for_worktree(
    worktree_path: str,
    *,
    require_feature_pipeline: bool = True,
    exclude_launch_sources: tuple[str, ...] = DASHBOARD_LAUNCH_SOURCES,
    path: Path | None = None,
) -> list[RuntimeSessionState]:
    """Authoritative "who is actively working this worktree" view.

    Returns non-terminal sessions for the worktree whose recorded launcher pid
    is still live, excluding the dashboard's own automation. By default only
    feature-pipeline sessions qualify — a generic Claude/Codex session a
    developer happens to have open on a worktree must not cause the dashboard
    to defer (and leave the PR handoff unattended).

    Liveness is decided purely by `pid_is_live` on the long-lived launcher pid.
    There is deliberately no wall-clock TTL: the launcher records `started`
    then a terminal event but no refreshing heartbeat for Claude sessions, so a
    TTL would drop genuinely-live long-running sessions and let the dashboard
    spawn a competing agent. Most-recent first.
    """
    if not worktree_path:
        return []
    summary = summarize_sessions(path=path)
    out: list[RuntimeSessionState] = []
    for state in summary.sessions.values():
        if state.worktree_path != worktree_path:
            continue
        if state.is_terminal:
            continue
        if state.launch_source in exclude_launch_sources:
            continue
        if require_feature_pipeline and not state.is_feature_pipeline:
            continue
        if not pid_is_live(state.pid):
            continue
        out.append(state)
    return sorted(out, key=lambda item: item.timestamp, reverse=True)


def summarize_sessions(path: Path | None = None) -> SessionSummary:
    summary = SessionSummary()
    for event in read_events(path=path):
        if event.get("event") == "harness_status_seen":
            continue
        session_id = str(event.get("session_id") or "")
        if not session_id:
            continue
        state = _merge_event(summary.sessions.get(session_id), event)
        summary.sessions[session_id] = state
    summary.reindex()
    return summary


def _retention_seconds() -> int:
    raw = os.environ.get("AGENTIC_PR_DASH_REGISTRY_RETENTION_SECONDS", "")
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_RETENTION_SECONDS
    return value if value > 0 else _DEFAULT_RETENTION_SECONDS


def _event_is_stale(event: dict[str, Any], cutoff_iso: str) -> bool:
    """True if a TERMINAL session's event is older than the retention cutoff.

    Non-terminal (still-running) sessions are never stale — a live agent's
    ``started`` event must be kept even if it's old. Timestamps are RFC3339 /
    ISO-8601 with a ``Z`` suffix (see ``_utc_now``), so a lexical compare is a
    valid chronological compare.
    """
    if str(event.get("event") or "") not in {"completed", "failed", "cleanup_completed"}:
        return False
    return str(event.get("timestamp") or "") < cutoff_iso


def compact_registry(path: Path | None = None, *, retention_seconds: int | None = None) -> int:
    """Drop events for sessions that are terminal AND last-seen before the
    retention cutoff, then atomically rewrite the registry (BOU-1637).

    A session is kept in full when EITHER its latest state is non-terminal (still
    running — liveness is decided later by ``pid_is_live``) OR its latest event is
    within the retention window. Returns the number of event lines removed. The
    append-only registry otherwise grows forever; periodic compaction bounds it so
    ``summarize_sessions`` stays cheap and the tail cap never silently hides a
    live session behind a wall of ancient terminal noise.
    """
    target = path or registry_path()
    if not target.exists():
        return 0
    with _registry_write_lock(target):
        return _compact_registry_locked(target, retention_seconds=retention_seconds)


def _compact_registry_locked(
    target: Path,
    *,
    retention_seconds: int | None = None,
) -> int:
    if not target.exists():
        return 0
    retention = retention_seconds if retention_seconds is not None else _retention_seconds()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=retention)
    cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")

    raw_lines = target.read_text(encoding="utf-8").splitlines()
    parsed: list[tuple[str, dict[str, Any]]] = []
    for line in raw_lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            parsed.append((line, event))

    # Decide per session: terminal in latest state AND its newest event is stale.
    # Harness reports are snapshots rather than an audit log, so retain only the
    # latest report for each session (plus the latest report before a terminal
    # event, which preserves runtime details after completion).
    latest_event_by_session: dict[str, str] = {}
    latest_ts_by_session: dict[str, str] = {}
    latest_status_index_by_session: dict[str, int] = {}
    status_before_terminal_by_session: dict[str, int] = {}
    keyed_history_indices_by_session: dict[str, list[int]] = {}
    for index, (_line, event) in enumerate(parsed):
        sid = str(event.get("session_id") or "")
        if not sid:
            continue
        event_name = str(event.get("event") or "")
        if event_name == "harness_status_seen" or (
            event_name == "harness_status"
            and event.get("idempotency_keyed") is not False
        ):
            keyed_history_indices_by_session.setdefault(sid, []).append(index)
        if event_name == "harness_status":
            latest_status_index_by_session[sid] = index
        elif event_name in {"completed", "failed", "cleanup_completed"}:
            prior_status = latest_status_index_by_session.get(sid)
            if prior_status is not None:
                status_before_terminal_by_session[sid] = prior_status
        ts = str(event.get("timestamp") or "")
        if event_name != "harness_status_seen" and ts >= latest_ts_by_session.get(
            sid, ""
        ):
            latest_ts_by_session[sid] = ts
            latest_event_by_session[sid] = event_name

    drop_sessions: set[str] = set()
    for sid, last_event in latest_event_by_session.items():
        if last_event not in {"completed", "failed", "cleanup_completed"}:
            continue
        if latest_ts_by_session.get(sid, "") < cutoff_iso:
            drop_sessions.add(sid)

    keep_status_indices = set(latest_status_index_by_session.values())
    keep_status_indices.update(status_before_terminal_by_session.values())
    keep_dedupe_indices: set[int] = set()
    for indices in keyed_history_indices_by_session.values():
        tombstone_candidates = [
            index for index in indices if index not in keep_status_indices
        ]
        keep_dedupe_indices.update(
            tombstone_candidates[-_STATUS_DEDUPE_TOMBSTONES_PER_SESSION:]
        )
    kept: list[str] = []
    rewritten = False
    for index, (line, event) in enumerate(parsed):
        sid = str(event.get("session_id") or "")
        if sid in drop_sessions:
            continue
        event_name = str(event.get("event") or "")
        if event_name == "harness_status_seen":
            if index in keep_dedupe_indices:
                kept.append(line)
            continue
        if (
            sid
            and event_name == "harness_status"
            and index not in keep_status_indices
        ):
            if index in keep_dedupe_indices:
                tombstone = _clean_payload(
                    {
                        "event_id": event.get("event_id"),
                        "session_id": sid,
                        "event": "harness_status_seen",
                        "timestamp": event.get("timestamp"),
                    }
                )
                kept.append(json.dumps(tombstone, sort_keys=True))
                rewritten = True
            continue
        kept.append(line)
    removed = len(parsed) - len(kept)
    if removed <= 0 and not rewritten:
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    import tempfile  # noqa: PLC0415

    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".events.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for line in kept:
                handle.write(line + "\n")
        os.replace(tmp, target)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 0
    return removed


def _record_from_args(args: argparse.Namespace) -> int:
    event = record_event(
        event=args.event,
        session_id=args.session_id,
        cli=args.cli,
        agent_name=args.agent_name,
        launch_source=args.launch_source,
        pid=args.pid,
        ppid=args.ppid,
        worktree_path=args.worktree_path,
        branch=args.branch,
        pr_number=args.pr_number,
        bead_id=args.bead_id,
        docker_mode=args.docker_mode,
        docker_host=args.docker_host,
        docker_daemon_name=args.docker_daemon_name,
        docker_context=args.docker_context,
        container_names=args.container_names,
        ports=args.ports,
        exit_code=args.exit_code,
        failure_reason=args.failure_reason,
        feature_pipeline=args.feature_pipeline or None,
    )
    print(json.dumps(event, sort_keys=True))
    return 0


def _report_from_args(args: argparse.Namespace) -> int:
    raw = sys.stdin.read(_MAX_STATUS_REPORT_BYTES + 1)
    if len(raw.encode("utf-8")) > _MAX_STATUS_REPORT_BYTES:
        raise ValueError("StatusReport JSON exceeds the 1 MiB limit")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid StatusReport JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("StatusReport JSON must be an object")
    event = record_status_report(
        payload,
        worktree_path=args.worktree_path,
        branch=args.branch,
        pid=args.pid,
        agent_name=args.agent_name,
        launch_source=args.launch_source,
    )
    print(
        json.dumps(
            {
                "event_id": event["event_id"],
                "ok": True,
                "session_id": event["session_id"],
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record Gaia runtime session events")
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser("record")
    record.add_argument("--event", required=True)
    record.add_argument("--session-id")
    record.add_argument("--cli")
    record.add_argument("--agent-name")
    record.add_argument("--launch-source")
    record.add_argument("--pid", type=int)
    record.add_argument("--ppid", type=int)
    record.add_argument("--worktree-path")
    record.add_argument("--branch")
    record.add_argument("--pr-number", type=int)
    record.add_argument("--bead-id")
    record.add_argument("--docker-mode")
    record.add_argument("--docker-host")
    record.add_argument("--docker-daemon-name")
    record.add_argument("--docker-context")
    record.add_argument("--container-names")
    record.add_argument("--ports")
    record.add_argument("--exit-code", type=int)
    record.add_argument("--failure-reason")
    record.add_argument("--feature-pipeline", action="store_true")
    record.set_defaults(func=_record_from_args)
    report = subparsers.add_parser("report")
    report.add_argument("--json", action="store_true", required=True)
    report.add_argument("--worktree-path")
    report.add_argument("--branch")
    report.add_argument("--pid", type=int)
    report.add_argument("--agent-name")
    report.add_argument("--launch-source")
    report.set_defaults(func=_report_from_args)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
