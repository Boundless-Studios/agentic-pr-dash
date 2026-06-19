"""Ownership-marker read/write helpers."""
from __future__ import annotations

import os
import sys

from agentic_pr_dash.config import load as load_config
from ._common import _parse_iso, _pid_alive, _fix_lease_seconds, _resolve_owner_pid, _current_branch, _repo_slug

# How long an owner's loop heartbeat stays "fresh" (the ownership lease).
_HEARTBEAT_TTL_SECONDS = 600       # 10 min — alive-and-ticking window (3m loop + slack)
_DEFAULT_FIX_LEASE_SECONDS = 1800  # 30 min — covers a long fix phase; override via env

# Heartbeat write coalescing window.
_DEFAULT_HEARTBEAT_MIN_INTERVAL_SECONDS = 60


def _marker_path(cwd: str) -> str:
    return str(load_config(cwd).watch_marker_for(cwd))


def _read_marker(cwd: str) -> dict[str, str] | None:
    try:
        with open(_marker_path(cwd), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def _heartbeat_ttl_seconds(cwd: str | None = None) -> int:
    """How long an owner's heartbeat counts as 'fresh'."""
    cfg = load_config(cwd) if cwd else load_config()
    modern = os.environ.get("AGENTIC_PR_DASH_HEARTBEAT_TTL_SECONDS", "")
    if modern.isdigit() and int(modern) > 0:
        return int(modern)
    toml_ttl = cfg.extra.get("heartbeat_ttl_seconds")
    if isinstance(toml_ttl, int) and toml_ttl > 0:
        return toml_ttl
    legacy = os.environ.get("GAIA_PR_WATCH_HEARTBEAT_TTL", "")
    if legacy.isdigit() and int(legacy) > 0:
        return int(legacy)
    return cfg.heartbeat_ttl_seconds


def _heartbeat_fresh(heartbeat: str, cwd: str | None = None) -> bool:
    """True if the owner's loop heartbeat is within the short alive-TTL of now."""
    ts = _parse_iso(heartbeat)
    if ts is None:
        return False
    from datetime import datetime, timezone  # noqa: PLC0415

    return (datetime.now(timezone.utc) - ts).total_seconds() <= _heartbeat_ttl_seconds(cwd)


def _fix_lease_active(lease_until: str) -> bool:
    """True if an in-progress fix lease has not yet expired."""
    if not lease_until:
        return False
    ts = _parse_iso(lease_until)
    if ts is None:
        print(
            f"[pr-watch] warning: unparseable fix_lease_until={lease_until!r}; "
            f"treating as ACTIVE (deferring) to avoid a double-dispatch race",
            file=sys.stderr,
        )
        return True
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc) < ts


def _live_foreign_owner(cwd: str, self_session_id: str) -> str | None:
    """Session id of a live, ACTIVELY-LOOPING in-session owner, else None."""
    fields = _read_marker(cwd)
    if fields is None:
        return None
    owner = fields.get("session_id", "")
    if not owner or owner == (self_session_id or ""):
        return None
    if not _pid_alive(fields.get("pid", "")):
        return None
    if _heartbeat_fresh(fields.get("heartbeat", ""), cwd) or _fix_lease_active(fields.get("fix_lease_until", "")):
        return owner
    return None


def _marker_live_foreign_pid(cwd: str, self_session_id: str) -> bool:
    """True if the worktree's marker names a DIFFERENT session whose pid is still alive."""
    fields = _read_marker(cwd)
    if fields is None:
        return False
    owner = fields.get("session_id", "")
    if not owner or owner == (self_session_id or ""):
        return False
    return _pid_alive(fields.get("pid", ""))


def _heartbeat_min_interval_seconds() -> int:
    raw = os.environ.get("AGENTIC_PR_DASH_HEARTBEAT_MIN_INTERVAL_SECONDS", "")
    if raw.isdigit() and int(raw) >= 0:
        return int(raw)
    return _DEFAULT_HEARTBEAT_MIN_INTERVAL_SECONDS


def _touch_owner_heartbeat(cwd: str, self_session_id: str, work_found: bool) -> None:
    """Refresh this owner's coordination stamps in the marker (owner-only write)."""
    if not self_session_id:
        return
    fields = _read_marker(cwd)
    if fields is None or fields.get("session_id", "") != self_session_id:
        return
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    prior_heartbeat = fields.get("heartbeat", "")
    had_lease = "fix_lease_until" in fields

    if not work_found:
        if not had_lease:
            prior_ts = _parse_iso(prior_heartbeat)
            if prior_ts is not None:
                age = (now - prior_ts).total_seconds()
                if 0 <= age < _heartbeat_min_interval_seconds():
                    return

    fields["heartbeat"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if work_found:
        fields["fix_lease_until"] = (now + timedelta(seconds=_fix_lease_seconds())).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    else:
        fields.pop("fix_lease_until", None)
    import tempfile  # noqa: PLC0415

    content = "".join(f"{k}={v}\n" for k, v in fields.items())
    target = _marker_path(cwd)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".pr-watch.armed.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, target)
    except OSError:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _write_arm_marker(cwd: str, session_id: str, pid: int, pr_number: int) -> bool:
    """Write the pr-watch.armed ownership marker (the single writer)."""
    import tempfile  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    state_dir = str(load_config(cwd).state_dir_for(cwd))
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        return False

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fields = {
        "pr": str(pr_number),
        "armed_at": now,
        "session_id": session_id,
        "pid": str(pid),
    }
    content = "".join(f"{k}={v}\n" for k, v in fields.items())
    target = os.path.join(state_dir, "pr-watch.armed")
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".pr-watch.armed.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, target)
    except OSError:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False

    try:
        with open(os.path.join(state_dir, "pr-watch.session"), "w", encoding="utf-8") as fh:
            fh.write(session_id + "\n")
    except OSError:
        pass

    try:
        import subprocess  # noqa: PLC0415
        from agentic_pr_dash import session_ledger  # noqa: PLC0415
        baseline = None
        try:
            rev = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=10)
            if rev.returncode == 0:
                baseline = rev.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            baseline = None
        branch = _current_branch(cwd)
        session_ledger.append(session_id, pr_number, branch, cwd, baseline,
                              repo=_repo_slug(cwd))
    except Exception:  # noqa: BLE001
        pass
    return True


def _marker_session_id(worktree_path: str) -> str | None:
    """Return the ``session_id=`` value from a worktree's pr-watch marker."""
    marker = _marker_path(worktree_path)
    try:
        with open(marker, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith("session_id="):
                    return line[len("session_id="):]
    except OSError:
        return None
    return None


def _read_session_marker(cwd: str) -> str:
    """Best-effort: read the owning session id from pr-watch.session."""
    try:
        with open(
            str(load_config(cwd).session_marker_for(cwd)),
            encoding="utf-8",
        ) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _prune_stale_marker(cwd: str, marker: dict, session_id: str) -> None:
    """Remove stale ownership artifacts when a marker's PR is authoritatively closed/merged."""
    from .pr_state import _pr_open_state  # noqa: PLC0415
    from .stop_gate import _stop_state_path  # noqa: PLC0415
    pr_raw = marker.get("pr", "")
    if not str(pr_raw).isdigit():
        return
    pr_number = int(pr_raw)

    state, *_ = _pr_open_state(pr_number, cwd)
    if state not in ("merged", "closed"):
        return

    try:
        os.remove(_marker_path(cwd))
    except OSError:
        pass

    try:
        os.remove(_stop_state_path(cwd))
    except OSError:
        pass

    try:
        from agentic_pr_dash import session_ledger  # noqa: PLC0415
        target_repo = _repo_slug(cwd)
        session_ledger.prune(session_id, {pr_number}, repo=target_repo)
    except Exception:  # noqa: BLE001
        pass

    try:
        from agentic_pr_dash import maintenance  # noqa: PLC0415
        maintenance.prune_state(cwd, pr_number)
    except Exception:  # noqa: BLE001
        pass


def _session_is_live(session_id: str, cwd: str | None = None) -> bool:
    """True if `session_id` has a non-terminal, pid-live registry entry."""
    from agentic_pr_dash import session_registry  # noqa: PLC0415
    try:
        reg = session_registry.registry_path(cwd) if cwd else None
        summary = session_registry.summarize_sessions(path=reg)
    except Exception:  # noqa: BLE001
        return False
    state = summary.sessions.get(session_id)
    if state is None or state.is_terminal:
        return False
    return session_registry.pid_is_live(state.pid)


def _claim_pr(pr_number: int, session_id: str, pid: int, repo: str = "") -> bool:
    """Win an exclusive claim on an orphan PR."""
    from agentic_pr_dash import session_ledger, session_registry  # noqa: PLC0415
    with session_ledger.claim_lock(pr_number, repo):
        existing = session_ledger.read_claim(pr_number, repo)
        if existing:
            if existing.get("session_id") == session_id:
                return True
            holder_pid = existing.get("pid")
            try:
                holder_pid = int(holder_pid)
            except (TypeError, ValueError):
                holder_pid = None
            if session_registry.pid_is_live(holder_pid):
                return False
        session_ledger.write_claim(pr_number, session_id, pid, repo)
        return True
