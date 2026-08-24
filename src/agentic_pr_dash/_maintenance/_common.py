"""Shared utilities — no heavy deps, stdlib only."""
from __future__ import annotations

import os
import subprocess


def _parse_iso(value: str):
    if not value:
        return None
    from datetime import datetime, timezone  # noqa: PLC0415

    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get("PR_AGENT_OPS_" + name) or os.environ.get("GAIA_PR_WATCH_" + name) or ""
    try:
        return int(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _fix_lease_seconds() -> int:
    from agentic_pr_dash.config import load as load_config  # noqa: PLC0415

    raw = os.environ.get("GAIA_PR_WATCH_LEASE_SECONDS", "")
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return load_config().lease_seconds


# Real pids fit in 10 decimal digits (Linux pid_max caps at 2**22; even a full
# 32-bit pid_t is 10 digits). Anything longer is corrupt — and must be rejected
# BEFORE int(): Python 3.11+'s int-str conversion limit raises ValueError on
# >4300-digit strings, ahead of any probe-level handling (PR #76 review).
_PID_MAX_DIGITS = 10


def _pid_alive(pid_raw: str) -> bool:
    """True only when ``pid_raw`` names a probe-ably live process.

    Fail closed on malformed input: ``str.isdigit()`` accepts non-ASCII
    digits (e.g. ``"²"``) that ``int()`` rejects, an oversized decimal string
    makes ``int()`` itself raise (3.11+ digit limit), and a huge-but-convertible
    value overflows ``os.kill``'s C pid_t — any of these would raise out of
    here and (via broad handlers upstream, e.g. the stop-gate's catch-all)
    release an uncovered PR. Any conversion/probe error means "not alive"
    (PR #76 review).
    """
    if not (pid_raw.isascii() and pid_raw.isdigit()):
        return False
    if len(pid_raw) > _PID_MAX_DIGITS:
        return False
    try:
        pid = int(pid_raw)
        if pid <= 0:
            # kill(0, 0) probes the whole process group, not a recorded process.
            return False
        os.kill(pid, 0)  # signal 0: existence probe only
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by another uid
    except (ValueError, OverflowError, OSError):
        # ValueError is unreachable given the guards above; kept so a future
        # guard regression still fails closed instead of raising.
        return False
    return True


def _resolve_owner_pid() -> int:
    """Best-effort durable owner pid to stamp into an adopted/armed marker."""
    start = os.getppid()
    pid = start
    for _ in range(8):
        if pid <= 1:
            break
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            break
        line = out.stdout.strip()
        if not line:
            break
        ppid_str, _sep, comm = line.partition(" ")
        comm_base = os.path.basename(comm.strip()).lower()
        if "claude" in comm_base or "codex" in comm_base or comm_base == "pi":
            return pid
        if not ppid_str.isdigit():
            break
        pid = int(ppid_str)
    return start


def _current_branch(cwd: str) -> str:
    """Return the current git branch name, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _repo_slug(cwd: str) -> str:
    """GitHub ``owner/name`` for the checkout at ``cwd``, or ""."""
    from pathlib import Path  # noqa: PLC0415
    from agentic_pr_dash.config import _detect_repo  # noqa: PLC0415
    try:
        return _detect_repo(Path(cwd)) or ""
    except Exception:  # noqa: BLE001
        return ""
