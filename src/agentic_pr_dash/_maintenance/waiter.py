"""Background feedback waiter helpers."""
from __future__ import annotations

import json
import os

from agentic_pr_dash.config import load as load_config
from ._common import _pid_alive


def _await_pidfile(cwd: str, session_id: str = "") -> str:
    """Per-session pidfile path."""
    from agentic_pr_dash import session_ledger as _sl  # noqa: PLC0415
    safe = _sl._safe_session(session_id) if session_id else "unknown"
    return str(load_config(cwd).state_dir_for(cwd) / f"pr-watch.await.{safe}.pid")


def _read_await_pidfile(cwd: str, session_id: str = "") -> dict:
    """Return the pidfile contents as a dict, or {} on missing/corrupt."""
    try:
        with open(_await_pidfile(cwd, session_id), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_await_pidfile(cwd: str, data: dict, session_id: str = "") -> None:
    """Write the await pidfile atomically (best-effort)."""
    path = _await_pidfile(cwd, session_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def _remove_await_pidfile(cwd: str, session_id: str = "") -> None:
    """Remove the await pidfile (best-effort)."""
    try:
        os.remove(_await_pidfile(cwd, session_id))
    except OSError:
        pass


def _await_alive(cwd: str, session_id: str) -> bool:
    """True if a live waiter pidfile exists with a matching session_id."""
    data = _read_await_pidfile(cwd, session_id)
    if not data:
        return False
    return (
        data.get("session_id") == session_id
        and _pid_alive(str(data.get("pid", "")))
    )


def _detached_loop_alive(cwd: str) -> bool:
    """True when the detached ``pr-maintenance-loop`` daemon is running."""
    cfg = load_config(cwd)
    if not cfg.maintenance_loop_machine_wide:
        return False
    pidfile = cfg.maintenance_loop_pidfile
    if pidfile is None:
        return False
    try:
        pid_raw = pidfile.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return _pid_alive(pid_raw)


def _detached_pending_entry(r: dict) -> tuple[str, str]:
    """Build the (label, text) pending entry for a detached PR record."""
    why = []
    if r["unresolved_threads"]:
        why.append(f"{r['unresolved_threads']} unresolved review thread(s)")
    if r["ci_failing"]:
        why.append("failing CI")
    if r.get("changes_requested"):
        why.append("review-level CHANGES_REQUESTED")
    if r.get("merge_conflict"):
        why.append("merge conflict")
    tag = " [P1]" if r["p1"] else ""
    text = (
        f"PR #{r['pr']}{tag} (No worktree / Awaiting Fixes): {r['url']}\n"
        f"  needs: {', '.join(why)}\n"
        f"  This PR's worktree was torn down. Recreate it from the branch "
        f"({r['branch'] or '<branch>'}) to fix, or explicitly hand it off — "
        f"do NOT leave it unmonitored.\n"
        f"PR_NUMBER={r['pr']}"
    )
    return (f"(no worktree) PR #{r['pr']}", text)
