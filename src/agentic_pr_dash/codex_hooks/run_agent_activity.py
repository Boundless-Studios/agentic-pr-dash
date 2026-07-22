"""Turn-lifecycle activity stamp for the PR dashboard (runtime-agnostic).

Both Claude Code and Codex fire turn-boundary hooks. This stamps the agent's
*real* turn state into ``<worktree>/<activity-subdir>/agent-activity.json`` so
the dashboard can tell "actively processing a turn" from "idle between turns" —
a precise signal CPU% can't give (a live REPL + MCP servers idle above the CPU
floor, and a periodic watch tick itself spikes CPU).

Records are keyed PER SESSION so two sessions sharing one worktree don't clobber
each other (one session's Stop must not flip another's sustained turn to idle)::

    {"sessions": {"<session_id>": {"state": "busy|idle", "busy_since": "...", "updated": "...", "pid": N}}}

Event -> state (for the firing session):
  UserPromptSubmit            -> busy, START a turn (busy_since = now)
  PreToolUse / PostToolUse    -> busy, keep busy_since, refresh ``updated``
  Stop                        -> idle (this session's turn finished)

The dashboard combines this with live-process presence: it's the authority for
"idle", but a "working" verdict still requires a live agent, and a stale busy
record falls back to live presence (covers a long single tool that fires no hook
for a while).

Concurrency & housekeeping:
  * The read-merge-write is serialized with an exclusive ``fcntl.flock`` on a
    sibling ``.agent-activity.lock`` and re-reads the snapshot *inside* the lock,
    so two sessions firing at once can't drop each other's record. Lock
    acquisition is bounded; if it fails, the advisory event is skipped rather
    than risking an unlocked read-modify-write that clobbers sibling sessions.
  * Orphan records — dead owner pid, or untouched past a TTL — are pruned on
    every write. The firing session's own record is never pruned.

The activity directory subdir is the dashboard's resolved state directory so the
running dashboard and these hooks agree on one location: the modern
``.agentic-pr-dash`` for fresh installs, or a pre-existing legacy ``.gaia`` when
one is already present (the same precedence ``config.py`` uses). Overridable via
``AGENT_ACTIVITY_SUBDIR``. Best-effort: never blocks for long, never errors out.

The payload arrives on stdin as ``{"hook_event_name", "session_id", "cwd",
"owner_pid"}`` — the repo-local shim normalizes each runtime's raw payload to
that shape (and supplies the durable owner pid) before delegating here.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

from agentic_pr_dash.config import DEFAULT_STATE_DIRNAME, LEGACY_STATE_DIRNAME

_TRACKED_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
_IDLE_TTL_SECONDS = 24 * 3600  # prune records untouched longer than this, regardless of pid
_LOCK_ATTEMPTS = 500           # x _LOCK_SLEEP ~ 5s max wait before skipping the event
_LOCK_SLEEP = 0.01


def _activity_subdir(root: str) -> str:
    """The state-dir subdir to stamp into, matching the dashboard's resolution.

    Explicit ``AGENT_ACTIVITY_SUBDIR`` wins. Otherwise prefer the modern
    ``.agentic-pr-dash``, but adopt a pre-existing legacy ``.gaia`` so installs
    that still carry the old dir keep a single source of truth (the same
    precedence ``config._resolve_state_dir`` applies)."""
    override = os.environ.get("AGENT_ACTIVITY_SUBDIR")
    if override:
        return override
    if os.path.isdir(os.path.join(root, LEGACY_STATE_DIRNAME)):
        return LEGACY_STATE_DIRNAME
    return DEFAULT_STATE_DIRNAME


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _worktree_root(payload: dict) -> str:
    return os.environ.get("CLAUDE_PROJECT_DIR") or str(payload.get("cwd") or os.getcwd())


def _pid_is_live(pid) -> bool:
    """True if a process with this pid currently exists. PermissionError means the
    process exists but is owned by another user -> still live."""
    if not isinstance(pid, int) or pid <= 0:
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


def _is_orphan(rec, now: datetime) -> bool:
    """A record (other than the firing session's) worth pruning.

    The owner pid is the authority: a record backed by a LIVE pid is never
    pruned, regardless of age — a session inside one long-running tool emits no
    hooks, so its ``updated`` stamp stays old while it is genuinely still
    working; the dashboard relies on that live-pid busy stamp. So: dead int pid
    -> orphan; live int pid -> keep; no usable pid -> fall back to the age TTL."""
    if not isinstance(rec, dict):
        return True
    pid = rec.get("pid")
    if isinstance(pid, int):
        return not _pid_is_live(pid)
    updated = _parse_iso(rec.get("updated"))
    return updated is None or (now - updated).total_seconds() > _IDLE_TTL_SECONDS


def _build_record(event: str, prev: dict, now: str, owner_pid: int) -> dict:
    if event == "UserPromptSubmit":
        return {"state": "busy", "busy_since": now, "updated": now, "pid": owner_pid}
    if event in ("PreToolUse", "PostToolUse"):
        return {
            "state": "busy",
            "busy_since": prev.get("busy_since") or now,  # preserve turn start
            "updated": now,
            "pid": owner_pid,
        }
    # Stop
    return {"state": "idle", "busy_since": "", "updated": now, "pid": owner_pid}


def _read_sessions(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return {}
    sessions = data.get("sessions")
    return sessions if isinstance(sessions, dict) else {}


def _write_atomic(activity_dir: str, path: str, sessions: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=activity_dir, prefix=".agent-activity.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"sessions": sessions}, fh)
        os.replace(tmp, path)  # atomic — readers never see a torn file
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _acquire(lock_fd: int) -> bool:
    """Bounded non-blocking exclusive lock. Returns True if acquired; False after
    ~5s so a wedged holder cannot hang the agent indefinitely."""
    for _ in range(_LOCK_ATTEMPTS):
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            time.sleep(_LOCK_SLEEP)
        except OSError:
            return False
    return False


def _merge_and_write(
    activity_dir: str, path: str, session_id: str, record: dict, now: datetime
) -> None:
    """Re-read the freshest snapshot, set this session's record, prune orphan
    siblings, write atomically. Caller holds the lock (or accepts best-effort)."""
    sessions = _read_sessions(path)
    sessions = {
        sid: rec
        for sid, rec in sessions.items()
        if sid == session_id or not _is_orphan(rec, now)  # never prune the firing session
    }
    sessions[session_id] = record
    _write_atomic(activity_dir, path, sessions)


def _load_payload() -> dict:
    try:
        raw = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return {}
    return raw if isinstance(raw, dict) else {}


def main() -> int:
    payload = _load_payload()
    # The firing event is passed as argv[1] (the hooks.json/settings.json
    # registration) and/or carried in the payload.
    event = (sys.argv[1] if len(sys.argv) > 1 else "") or str(payload.get("hook_event_name") or "")
    if event not in _TRACKED_EVENTS:
        return 0  # event we don't track — leave the stamp untouched, take no lock

    session_id = str(payload.get("session_id") or "") or "unknown"
    # Owner pid for the dashboard's liveness/orphan check. The hook's parent IS
    # the agent process for Claude; the Codex adapter passes its own ppid as
    # owner_pid (its parent is the codex process, not this short-lived child).
    try:
        owner_pid = int(payload.get("owner_pid") or os.getppid())
    except (TypeError, ValueError):
        owner_pid = os.getppid()

    root = _worktree_root(payload)
    activity_dir = os.path.join(root, _activity_subdir(root))
    path = os.path.join(activity_dir, "agent-activity.json")
    lock_path = os.path.join(activity_dir, ".agent-activity.lock")

    now_str = _now()
    now_dt = datetime.now(timezone.utc)

    try:
        os.makedirs(activity_dir, exist_ok=True)
    except OSError:
        return 0

    prev = _read_sessions(path).get(session_id) or {}
    record = _build_record(event, prev, now_str, owner_pid)

    lock_fd = None
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        lock_fd = None

    if lock_fd is None:
        return 0

    acquired = _acquire(lock_fd)
    if not acquired:
        os.close(lock_fd)
        return 0

    try:
        _merge_and_write(activity_dir, path, session_id, record, now_dt)
    except OSError:
        pass
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
