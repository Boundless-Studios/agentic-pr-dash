"""Durable session runtime event registry for Gaia agent/worktree sessions."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

# Launch sources that are the dashboard's OWN automation, not an independent
# session whose worktree we should defer to.
DASHBOARD_LAUNCH_SOURCES = ("agentic-pr-dash", "pr-dashboard")


@dataclass
class RuntimeSessionState:
    session_id: str
    event: str
    timestamp: str
    cli: str = "unknown"
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

    @property
    def recent(self) -> list[RuntimeSessionState]:
        return sorted(self.sessions.values(), key=lambda item: item.timestamp, reverse=True)


def registry_path(cwd: str | None = None) -> Path:
    return _default_registry(cwd)


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
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return payload


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
    if limit is not None:
        lines = lines[-limit:]
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
    if str(event.get("event")) == "started" and event.get("pid") is not None:
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
        session_id = str(event.get("session_id") or "")
        if not session_id:
            continue
        state = _merge_event(summary.sessions.get(session_id), event)
        summary.sessions[session_id] = state
        if state.worktree_path:
            summary.by_worktree[state.worktree_path] = state
    return summary


def _record_from_args(args: argparse.Namespace) -> int:
    event = record_event(
        event=args.event,
        session_id=args.session_id,
        cli=args.cli,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record Gaia runtime session events")
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser("record")
    record.add_argument("--event", required=True)
    record.add_argument("--session-id")
    record.add_argument("--cli")
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
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
