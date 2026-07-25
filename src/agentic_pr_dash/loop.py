"""Agent-agnostic maintenance loop (``agentic-pr-dash loop``).

Each tick: discover the worktrees to service, run ``check`` on each, and when a
PR needs work dispatch the fix prompt to a **configurable executor** (any CLI
that accepts a prompt — Claude Code, Codex, aider, a shell script, …), then run
``complete`` to resolve the review threads the fix addressed.

The executor command comes from config (``executor`` / ``AGENTIC_PR_DASH_EXECUTOR``)
and uses ``{prompt}`` as the substitution point, e.g.::

    executor = "claude --dangerously-skip-permissions -p {prompt}"
    executor = "codex exec --full-auto {prompt}"
    executor = "aider --message {prompt} --yes"

This replaces the original project-specific ``pr-maintenance-loop.sh`` and hard
dependence on ``claude``/``codex``.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from functools import lru_cache
from pathlib import Path

from agent_coordinator.service import StaleClaimError

from . import coordinator
from ._maintenance import worktree_check
from ._maintenance._common import _pid_alive
from ._maintenance.worktrees import _live_independent_owner_paths
from .agents import discover_active_agents
from .config import load as load_config
from .worktrees import find_worktree_for_path, remove_worktree, selected_worktree_cleanup_reason

CHECK_WORK_FOUND = 10


# ---------------------------------------------------------------------------
# Per-PR executor-failure streak tracking (BOU-1789 Task 5)
# ---------------------------------------------------------------------------


def _daemon_dir(cwd: str) -> Path:
    cfg = load_config(cwd)
    if cfg.maintenance_loop_pidfile is not None:
        return cfg.maintenance_loop_pidfile.parent
    return Path.home() / ".claude" / "daemons"


def _raw_repo_identity(cwd: str) -> str:
    """The RAW (un-sanitized) canonical repo identity for ``cwd``.

    ``owner/name`` when resolvable (so the loop writer and the stop-gate /
    orchestrator readers agree regardless of whether they run from the worktree
    or the main checkout), else the worktree dir name, else ``"repo"``.
    """
    repo = load_config(cwd).resolved_repo(Path(cwd))
    return repo or Path(cwd).resolve().name or "repo"


def _sanitized_repo_slug(raw: str) -> str:
    """Filesystem-safe form of ``raw`` (``/`` and other non-``[A-Za-z0-9-_.]``
    chars → ``-``)."""
    return "".join(c if (c.isalnum() or c in "-_.") else "-" for c in raw)


@lru_cache(maxsize=64)
def _repo_slug(cwd: str) -> str:
    """Filesystem-safe identifier for the repo a worktree belongs to.

    The machine-wide loop services worktrees across several repos
    (``maintenance_repo_roots``) but shares one daemon dir, so the streak /
    escalation state must be namespaced by repo — otherwise repo A's PR #42 and
    repo B's PR #42 collide on a bare ``"42"`` key and one repo's failures
    escalate (or suppress loop-coverage for) the other's unrelated PR. Scoping
    the *filename* by repo keeps the JSON keys as bare PR numbers, so the
    stop-gate / dashboard readers (which parse ``int(key)``) stay valid.

    Cached because the repo resolve can shell out to ``gh``/``git`` and is hit
    on every streak read.
    """
    raw = _raw_repo_identity(cwd)
    safe = _sanitized_repo_slug(raw)
    # Sanitizing ``/`` to ``-`` collapses distinct repos ("acme/foo-bar" and
    # "acme-foo/bar" both become "acme-foo-bar"), so two independent repos
    # sharing the daemon dir would read/write the SAME health/escalation file
    # — one repo's healthy record would grant stop-gate coverage to a repo the
    # loop never services. Suffix a digest of the RAW identity so distinct
    # repos can never share a file (PR #76 review).
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


@lru_cache(maxsize=64)
def _legacy_repo_slug(cwd: str) -> str:
    """The pre-PR#76 slug (sanitized identity, NO digest suffix).

    Retained ONLY so :func:`_migrate_legacy_state` can find health/escalation
    files an OLDER install wrote under the digest-less name and move them to the
    current name — so streaks and escalation markers survive an in-place upgrade
    (PR #76 review)."""
    return _sanitized_repo_slug(_raw_repo_identity(cwd))


def _health_filename(slug: str) -> str:
    return f"pr-maintenance-loop.health.{slug}.json"


def _escalated_filename(slug: str) -> str:
    return f"pr-maintenance-loop.escalated.{slug}.json"


@lru_cache(maxsize=64)
def _migrate_legacy_state(cwd: str) -> None:
    """One-time, best-effort migration of pre-PR#76 loop state (BOU-2086).

    PR #76 appended an ``sha256`` digest to :func:`_repo_slug` so distinct repos
    sharing the daemon dir can no longer collide on one health/escalation file.
    That rename orphans every file an older install wrote under the digest-less
    slug: the per-PR executor-failure streaks read back as zero and escalation
    markers disappear, so a PR that had reached the escalation threshold would
    resume being treated as loop-covered after the upgrade. Move the legacy
    files to the new names on first access.

    Only migrates when the NEW file is ABSENT, so it never clobbers fresh
    post-upgrade state. If two repos collided on the legacy slug (the very case
    the digest fixed), the first to migrate wins the shared file and the other
    starts clean — no worse than the pre-fix collision it replaces. Routed
    through both path funnels (:func:`_health_file` /
    :func:`_escalated_marker_path`) so every reader — including the stop-gate's
    ``_read_escalation_marker`` — triggers it; lru_cached to run once per cwd.
    """
    # Best-effort and fail-safe: resolving the slug can shell out to git/gh, and
    # a migration miss only reverts to the pre-fix (state-reset) behavior — it
    # must NEVER raise into the path funnels that call this, or health/escalation
    # resolution (and the loop) would break. Swallow everything.
    try:
        legacy_slug = _legacy_repo_slug(cwd)
        new_slug = _repo_slug(cwd)
        if legacy_slug == new_slug:
            return
        daemon_dir = _daemon_dir(cwd)
        for filename in (_health_filename, _escalated_filename):
            new_path = daemon_dir / filename(new_slug)
            legacy_path = daemon_dir / filename(legacy_slug)
            try:
                if new_path.exists() or not legacy_path.exists():
                    continue
                os.replace(legacy_path, new_path)
            except OSError:
                pass
    except Exception:  # noqa: BLE001 — migration is best-effort; never break path resolution
        pass


def _health_file(cwd: str) -> Path:
    """Path to the per-repo loop health JSON file."""
    _migrate_legacy_state(cwd)
    return _daemon_dir(cwd) / _health_filename(_repo_slug(cwd))


def _load_health(cwd: str) -> dict:
    """Load the health JSON, or {} on missing/corrupt."""
    try:
        raw = _health_file(cwd).read_text(encoding="utf-8")
        data = __import__("json").loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _save_health(cwd: str, data: dict) -> None:
    """Atomically write the health JSON."""
    import json as _json  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    hf = _health_file(cwd)
    try:
        hf.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=hf.parent, delete=False, suffix=".tmp"
        ) as fh:
            _json.dump(data, fh)
            tmp_path = fh.name
        os.replace(tmp_path, hf)
    except OSError:
        pass


def _mutate_health(cwd: str, apply):
    """Run ``apply(data)`` on the loaded health dict and persist it, serialized
    by a cross-process exclusive lock. ``apply`` mutates ``data`` in place and
    may return a value, which is returned to the caller.

    :func:`_save_health` already writes atomically (os.replace), so a reader
    never sees a torn JSON file. But atomicity does NOT prevent a lost UPDATE: a
    bare load-modify-save is racy because two supported loop modes (a
    machine-wide loop and a session-scoped loop) stamp the SAME repo-wide health
    file. A machine tick can load, a session loop can then persist a
    freshly-failed PR streak, and the machine tick's save (holding only
    ``__loop__``) reverts the streak to absent — after which ``_loop_covers_pr``
    again reads the repeatedly-failing PR as covered and suppresses its waiter.
    Loading AND saving under the lock serializes the read-modify-write windows so
    no writer clobbers another's committed streak (PR #76 review).

    The lock is best-effort: on a platform without ``fcntl`` (non-POSIX) or if
    the lock file can't be opened, it degrades to an unlocked mutate rather than
    dropping the write entirely.
    """
    try:
        import fcntl  # noqa: PLC0415
    except ImportError:  # pragma: no cover — non-POSIX: degrade to unlocked
        fcntl = None  # type: ignore[assignment]
    lock_fd: int | None = None
    if fcntl is not None:
        try:
            hf = _health_file(cwd)
            hf.parent.mkdir(parents=True, exist_ok=True)
            lock_path = hf.with_name(hf.name + ".lock")
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except OSError:
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
                lock_fd = None
    try:
        data = _load_health(cwd)
        result = apply(data)
        _save_health(cwd, data)
        return result
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)


# ---------------------------------------------------------------------------
# Loop health record (BOU-2086)
# ---------------------------------------------------------------------------
# Reserved (non-numeric) key in the per-repo health file holding the LOOP's own
# health record — heartbeat + executor viability — alongside the per-PR streak
# entries (whose keys are bare PR numbers). Coverage checks require this record
# to be FRESH and viable; a live wrapper/daemon PID alone is not proof the loop
# can actually dispatch an executor (both executors can be unreachable while the
# process idles), so pid-alive-only coverage left red PRs unwatched.
LOOP_HEALTH_KEY = "__loop__"

# Freshness floor (seconds) for the loop heartbeat. The effective max age is
# max(LOOP_HEALTH_INTERVAL_MULTIPLIER * recorded tick interval, this floor) so a
# short-interval loop isn't declared stale during one long executor dispatch.
LOOP_HEALTH_MIN_FRESH_S = 900.0
LOOP_HEALTH_INTERVAL_MULTIPLIER = 3.0
_DEFAULT_TICK_INTERVAL_S = 600.0
# Sanity ceiling on a recorded tick interval (one day). A finite-but-huge
# interval (e.g. 1e308) passes isfinite yet overflows the freshness
# multiplication to inf — and even a non-overflowing 1e9 would grant a
# multi-decade staleness window. No real maintenance loop ticks slower than
# daily, so anything larger marks the record corrupt (PR #76 review).
LOOP_HEALTH_MAX_INTERVAL_S = 86400.0
# Tolerance for a heartbeat slightly AHEAD of the reader's clock (NTP nudges,
# cross-process timer skew). Anything further in the future is a corrupt or
# hand-written value (e.g. 1e308) or a large backward wall-clock jump — either
# way ``now - heartbeat`` would be negative and the record would pass the
# freshness check effectively forever, so fail closed (PR #76 review).
LOOP_HEALTH_MAX_CLOCK_SKEW_S = 120.0


def _pr_health_entries(data: dict) -> dict:
    """The per-PR streak entries of a health dict (drops the loop record)."""
    return {k: v for k, v in data.items() if k != LOOP_HEALTH_KEY}


def loop_health_entry(cwd: str) -> dict:
    """The loop's own health record from the per-repo health file, or {}."""
    entry = _load_health(cwd).get(LOOP_HEALTH_KEY)
    return entry if isinstance(entry, dict) else {}


def record_loop_health(
    cwd: str,
    *,
    executors_viable: bool,
    errors: dict | None = None,
    interval: float | None = None,
    session_id: str = "",
    no_discover_worktrees: bool = False,
    once: bool = False,
) -> None:
    """Persist the loop's heartbeat + executor-viability record (BOU-2086).

    Written on every tick (viable or not) and on startup executor-validation
    failure — so coverage readers can distinguish "daemon process exists" from
    "daemon can actually service PRs". ``errors`` retains the CONCRETE
    per-executor error strings (both primary and fallback) for diagnosis.

    The record's ``scope`` tells coverage readers whether this heartbeat is
    machine-wide coverage. It is ``"machine"`` ONLY when the loop enumerates
    every worktree in the repo — i.e. no ``--session-id`` AND full discovery.
    A ``--session-id`` loop is ``"session"`` (services only its own session's
    worktrees), a ``--no-discover-worktrees`` loop is ``"restricted"`` (services
    only its explicit ``--cwd`` set), and a ``--once`` loop is ``"oneshot"``
    (``main()`` exits after this single tick, so its heartbeat is not durable
    coverage — a stop-gate reached mid-tick must not suppress a waiter the
    exiting one-shot will never replace). All still stamp the same repo-wide
    health file, so if any read as machine-wide coverage it would suppress
    waiters for worktrees/windows it never services — only ``"machine"`` grants
    coverage in :func:`loop_health_ok` (PR #76 review).
    """
    if session_id:
        scope = "session"
    elif no_discover_worktrees:
        scope = "restricted"
    elif once:
        scope = "oneshot"
    else:
        scope = "machine"
    entry: dict = {
        "heartbeat": time.time(),
        "pid": os.getpid(),
        "executors_viable": bool(executors_viable),
        "scope": scope,
    }
    if session_id:
        entry["session_id"] = str(session_id)[:200]
    clean_errors = {k: str(v)[:500] for k, v in (errors or {}).items() if v}
    if clean_errors:
        entry["errors"] = clean_errors
    if interval is not None:
        try:
            entry["interval"] = float(interval)
        except (TypeError, ValueError):
            pass
    _mutate_health(cwd, lambda data: data.__setitem__(LOOP_HEALTH_KEY, entry))


def _pid_state(pid: int) -> str | None:
    """Best-effort process-state code for ``pid`` (e.g. ``"R"``, ``"S"``, ``"Z"``).

    Linux: the field after the LAST ``)`` in ``/proc/<pid>/stat`` (the comm
    field may itself contain parentheses). Elsewhere (macOS/BSD): ``ps -o
    stat=``. Returns None when the state cannot be determined (no /proc entry,
    ``ps`` missing/failed, permissions) so callers can keep the plain
    ``kill(0)`` liveness verdict instead of manufacturing false-deads.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        fields = raw.rpartition(")")[2].split()
        if fields:
            return fields[0]
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    state = out.stdout.strip().split()
    return state[0] if state else None


def loop_health_ok(cwd: str, now: float | None = None) -> bool:
    """True when the loop's health record is FRESH, executors are viable, AND
    the process that recorded it is still alive.

    Fail-closed: a missing/corrupt record, a stale heartbeat, a heartbeat in
    the future beyond ``LOOP_HEALTH_MAX_CLOCK_SKEW_S``, a non-boolean
    ``executors_viable`` value, an explicit ``executors_viable: false``, a
    dead (or positively-identified zombie) recorded pid, a non-finite
    ``heartbeat``/``interval``, a present malformed interval, an interval beyond
    ``LOOP_HEALTH_MAX_INTERVAL_S``, or a record not written by the
    machine-wide loop (``scope`` != ``"machine"``) all return False —
    pid-alive alone must never count as maintenance coverage (BOU-2086).
    """
    entry = loop_health_entry(cwd)
    # Require a REAL boolean True. This health record is the fail-closed
    # coverage signal, so a truthy-but-wrong value (e.g. the string "false"
    # from a shell/tooling writer or a hand-edited file) must read as
    # unhealthy (PR #76 review).
    if entry.get("executors_viable") is not True:
        return False
    # Require the record to have been written by the MACHINE-WIDE loop. A
    # session-scoped ``loop --session-id ...`` (scope "session") and a
    # ``loop --no-discover-worktrees`` (scope "restricted") both run the same
    # _tick and stamp this same repo-wide health file, but each services only a
    # subset of the repo's worktrees — their heartbeats are not machine-wide
    # coverage and must not suppress waiters for worktrees they never inspect.
    # Absent scope (older snapshot, hand-written file) fails closed like any
    # other unprovable record (PR #76 review).
    if entry.get("scope") != "machine":
        return False
    # The heartbeat must be tied to a live servicing process: the daemon
    # pidfile can hold a supervisor/wrapper pid that outlives the python loop,
    # and a one-shot healthy stamp (e.g. ``--once``) must not grant a whole
    # freshness window after its writer exits (PR #76 review).
    pid_raw = str(entry.get("pid", ""))
    if not _pid_alive(pid_raw):
        return False
    # ``os.kill(pid, 0)`` succeeds for zombies, so a crashed-but-unreaped loop
    # child would still read as a live servicing process. Fail closed only on a
    # POSITIVELY identified zombie; an unreadable/unknown state keeps the
    # kill(0) verdict so permission quirks don't cause false-deads
    # (PR #76 review).
    state = _pid_state(int(pid_raw))
    if state is not None and state.startswith("Z"):
        return False
    # A present heartbeat came from JSON, where a real numeric value can only be
    # an int or float. A decimal STRING ("123", or a shell writer's
    # ``str(time.time())``) would be silently coerced by ``float()`` even though
    # the fail-closed record is malformed — mirror the interval guard below and
    # reject anything that isn't a real (non-bool) number (PR #76 review).
    heartbeat_raw = entry.get("heartbeat")
    if isinstance(heartbeat_raw, bool) or not isinstance(heartbeat_raw, (int, float)):
        return False
    # JSON integers are arbitrary-precision, so a 400-digit int passes the type
    # guard but overflows ``float()`` — catch that instead of letting it escape.
    try:
        heartbeat = float(heartbeat_raw)
    except (TypeError, ValueError, OverflowError):
        return False
    interval_raw = entry.get("interval", _DEFAULT_TICK_INTERVAL_S)
    # Only records written before interval persistence may use the default.
    # A present value came from JSON, where a real numeric interval can only be
    # an int or float; strings, containers, null, and booleans are corrupt state
    # and must not silently receive a healthy 600-second freshness window.
    if isinstance(interval_raw, bool) or not isinstance(interval_raw, (int, float)):
        return False
    try:
        interval = float(interval_raw)
    except (TypeError, ValueError, OverflowError):
        return False
    # JSON like ``1e309`` parses to inf: a non-finite heartbeat makes
    # ``now - heartbeat`` -inf (granting coverage forever) and a non-finite
    # interval makes the freshness window infinite — either way the record is
    # invalid, so fail closed (PR #76 review).
    if not math.isfinite(heartbeat) or not math.isfinite(interval):
        return False
    # isfinite alone is not enough: a finite-but-huge interval (1e308) still
    # overflows the multiplication below to inf, accepting arbitrarily stale
    # heartbeats. Bound the interval so the COMPUTED freshness window is
    # always finite and sane (PR #76 review).
    if interval > LOOP_HEALTH_MAX_INTERVAL_S:
        return False
    max_age = max(LOOP_HEALTH_INTERVAL_MULTIPLIER * interval, LOOP_HEALTH_MIN_FRESH_S)
    now = time.time() if now is None else now
    # A finite heartbeat in the FUTURE (corrupt value like 1e308, or a large
    # backward wall-clock adjustment) makes ``now - heartbeat`` negative and
    # would satisfy any max_age indefinitely. Allow only a small clock-skew
    # tolerance; beyond that the record is invalid (PR #76 review).
    if heartbeat - now > LOOP_HEALTH_MAX_CLOCK_SKEW_S:
        return False
    return (now - heartbeat) <= max_age


def _entry_streak(entry: object) -> int:
    """Streak from a per-PR health entry, coercing malformed content to 0.

    A health file can be hand-edited or partially written, so a per-PR entry
    may not be a dict, or ``streak`` may not parse as an int. Raising here would
    propagate into _loop_covers_pr / record_executor_failure; in the stop-gate
    path that exception is swallowed and the session releases with NO waiter even
    though the PR may be at the escalation threshold (codex PR #50 review)."""
    if not isinstance(entry, dict):
        return 0
    try:
        return int(entry.get("streak", 0))
    except (TypeError, ValueError):
        return 0


def executor_failure_streak(cwd: str, pr: int | None) -> int:
    """Return the current executor-failure streak for PR ``pr`` (0 if unknown)."""
    return _entry_streak(_load_health(cwd).get(str(pr), {}))


def _emit_loop_event(cwd: str, kind: str, pr: int | None, details: dict) -> None:
    """Best-effort observability emit (BOU-1880): persist streak/escalation to the
    decoupled events.jsonl so they survive restart. Never raises, never alters the
    loop's dispatch flow (mirrors orchestrator._emit)."""
    try:
        from datetime import datetime, timezone  # noqa: PLC0415
        from .observability.event_store import ObservabilityEvent, get_event_store  # noqa: PLC0415
        get_event_store(cwd).append(ObservabilityEvent(
            ts=datetime.now(timezone.utc), repo=cwd, pr_number=pr, kind=kind,
            session_id=None, details=details,
        ))
    except Exception:  # noqa: BLE001
        pass


def record_executor_failure(cwd: str, pr: int | None, err: str) -> int:
    """Record a new executor failure for ``pr``; return the new streak count."""
    key = str(pr)

    def _apply(data: dict) -> int:
        streak = _entry_streak(data.get(key, {})) + 1
        data[key] = {"streak": streak, "last_error": err, "updated": time.time()}
        return streak

    new_streak = _mutate_health(cwd, _apply)
    _emit_loop_event(cwd, "streak", pr, {"streak": new_streak, "last_error": err[:200]})
    return new_streak


def _escalated_marker_path(cwd: str) -> Path:
    """Path to the per-repo escalation marker JSON (same daemon dir as health).

    Repo-scoped for the same reason as :func:`_health_file` — so PRs with the
    same number in different repos don't share an escalation marker.
    """
    _migrate_legacy_state(cwd)
    return _daemon_dir(cwd) / _escalated_filename(_repo_slug(cwd))


def _clear_escalation_entry(cwd: str, pr: int | None) -> None:
    """Drop ``pr`` from the escalation marker so a recovered PR stops nagging.

    Without this, a PR that escalated and was then fixed (but is still open,
    awaiting merge) would re-fire the stop-gate escalation block forever — the
    streak resets but the marker would otherwise persist.
    """
    import json as _json  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    marker_path = _escalated_marker_path(cwd)
    try:
        existing = _json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(existing, dict) or str(pr) not in existing:
        return
    del existing[str(pr)]
    try:
        if existing:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=marker_path.parent,
                delete=False, suffix=".tmp",
            ) as fh:
                _json.dump(existing, fh)
                tmp = fh.name
            os.replace(tmp, marker_path)
        else:
            marker_path.unlink(missing_ok=True)
    except OSError:
        pass


def reset_executor_failure(cwd: str, pr: int | None) -> None:
    """Reset the executor-failure streak for ``pr`` after a successful dispatch.

    Also clears any escalation marker for ``pr`` so a recovered PR stops
    surfacing the stop-gate escalation block.
    """
    key = str(pr)
    _mutate_health(cwd, lambda data: data.pop(key, None))
    _clear_escalation_entry(cwd, pr)


def _load_escalation(cwd: str) -> dict:
    """Load the escalation marker JSON for this repo, or {} on missing/corrupt."""
    import json as _json  # noqa: PLC0415
    try:
        data = _json.loads(_escalated_marker_path(cwd).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _clear_recovered_streak(cwd: str) -> None:
    """Drop a worktree's streak/escalation entry when its check comes back clean.

    The streak/marker is only reset on the executor-success path. If a PR is
    fixed manually (following the escalation prompt) or recovers externally
    (CI/comments resolved), the next tick's ``check`` returns 0 and continues
    BEFORE any dispatch — leaving the marker in place so the stop-gate/dashboard
    keep treating the now-clean PR as escalated. Clearing on a clean result
    closes that gap. Cheap-guarded: only resolves the PR via ``gh`` when this
    repo actually has recorded streak/escalation state.
    """
    # Only the per-PR streak entries gate this guard — the loop's own health
    # record (LOOP_HEALTH_KEY) is persistent, so counting it would turn the
    # cheap-guard into a `gh pr view` per worktree per tick (BOU-2086).
    if not _pr_health_entries(_load_health(cwd)) and not _load_escalation(cwd):
        return
    import json as _json  # noqa: PLC0415
    from agentic_pr_dash import github_api  # noqa: PLC0415
    try:
        r = subprocess.run(
            ["gh", "pr", "view", "--json", "number"],
            cwd=cwd, capture_output=True, text=True, timeout=30,
            env=github_api.automation_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if r.returncode != 0:
        return
    try:
        pr = _json.loads(r.stdout or "{}").get("number")
    except ValueError:
        return
    if isinstance(pr, int):
        reset_executor_failure(cwd, pr)


def _loop_covers_pr(cwd: str, pr: int | None) -> bool:
    """True when the detached loop is alive+healthy AND the streak < threshold.

    When the streak reaches the threshold the loop has repeatedly failed to fix
    the PR, so it no longer counts as coverage — the stop-gate must force a
    per-session waiter to bring the issue to the user.

    ``_detached_loop_alive`` itself requires a fresh, executors-viable loop
    health record (BOU-2086), so a wrapper pid that is alive while the python
    loop exited (or can dispatch neither executor) is NOT coverage.
    """
    from ._maintenance.waiter import _detached_loop_alive  # noqa: PLC0415
    if not _detached_loop_alive(cwd):
        return False
    # Session precedence: if a live in-session owner holds this worktree's marker
    # (pid alive), the loop DEFERS to it — the dashboard skips dispatch and the
    # check/stop-gate gate on _live_foreign_owner. So the loop is NOT coverage for
    # this PR; the owning session must keep its own waiter to surface feedback.
    # Return False so the stop-gate forces a per-session waiter instead of idling
    # on phantom loop coverage.
    from ._maintenance import markers  # noqa: PLC0415
    if markers._marker_live_foreign_pid(cwd, ""):
        return False
    # ...and the claim-side half of the same question. Unioned, not a
    # replacement: from Stage 4 the marker is no longer written, so the check
    # above degrades to "nobody owns this" and the loop would report itself as
    # coverage for a PR a live session is actively holding — which also
    # suppresses that session's own waiter (BOU-2223 Stage 4).
    from ._maintenance.ownership_resolution import live_foreign_claim  # noqa: PLC0415
    if live_foreign_claim(cwd, "", kind="loop_coverage_divergence"):
        return False
    # BOU-1924: also defer to a live owner resolved from the durable
    # ledger/registry — the marker at THIS cwd is stale/absent when the owning
    # session repointed its worktree away, so marker-only resolution would miss
    # a live stacked owner and wrongly count the loop as coverage.
    if pr is not None:
        from ._maintenance._common import _repo_slug  # noqa: PLC0415
        if markers._live_pr_owner(pr, _repo_slug(cwd), "", cwd):
            return False
    cfg = load_config(cwd)
    threshold = cfg.escalation_failure_threshold
    return executor_failure_streak(cwd, pr) < threshold


def _maybe_escalate(cwd: str, pr: int | None, err: str, streak: int) -> None:
    """Edge-triggered escalation at streak == threshold (fires once per threshold crossing).

    Writes an escalation marker, calls iterm.notify, and sets a dashboard flag.
    Wired in from _tick after record_executor_failure.
    """
    from .config import load as _load_config  # noqa: PLC0415
    cfg = _load_config(cwd)
    threshold = cfg.escalation_failure_threshold
    if streak != threshold:
        return  # only fire once at the crossing point

    import json as _json  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    from . import iterm  # noqa: PLC0415

    # Write escalation marker
    marker_path = _escalated_marker_path(cwd)
    daemon_dir = marker_path.parent
    try:
        existing: dict = {}
        try:
            existing = _json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        # A valid-JSON-but-non-dict marker (e.g. ``[]`` from a bad write/manual
        # edit) would make the key assignment below raise TypeError — which the
        # outer ``except OSError`` does NOT catch — aborting the loop exactly at
        # the escalation moment, before the coordinator claim is released. Treat
        # any non-dict content as empty, mirroring the reader.
        if not isinstance(existing, dict):
            existing = {}
        existing[str(pr)] = {
            "streak": streak, "last_error": err, "escalated_at": time.time(),
        }
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=daemon_dir, delete=False, suffix=".tmp"
        ) as fh:
            _json.dump(existing, fh)
            tmp = fh.name
        os.replace(tmp, marker_path)
    except OSError:
        pass

    _emit_loop_event(cwd, "escalation", pr, {
        "streak": streak, "threshold": threshold, "last_error": err[:200],
    })

    iterm.notify(
        f"PR #{pr} escalated",
        f"Executor failed {streak} times in a row: {err[:120]}",
    )
    print(
        f"[agentic-pr-dash] ESCALATION: PR #{pr} executor failed {streak} times "
        f"(threshold={threshold}); notified user and wrote escalation marker.",
        file=sys.stderr,
    )


def _discover_cwds(args) -> list[str]:
    """Worktrees to service this tick."""
    if args.no_discover_worktrees:
        return list(args.cwd)
    if args.session_id:
        # Scope to worktrees this session owns, across EVERY configured --cwd
        # (the flag is repeatable, one per repo worktree-pool). Run list-owned
        # per cwd and merge: each scan runs `git worktree list` in its own dir,
        # so a session owning PRs under several repos discovers them all rather
        # than only the first (PR #7 review, P2).
        discovered: list[str] = []
        seen: set[str] = set()

        def _add(path: str) -> None:
            if path and path not in seen:
                seen.add(path)
                discovered.append(path)

        for cwd in args.cwd:
            out = subprocess.run(
                [sys.executable, "-m", "agentic_pr_dash", "list-owned",
                 "--session-id", args.session_id, "--pid", str(_loop_pid()),
                 "--cwd", cwd],
                capture_output=True, text=True,
            )
            if out.returncode != 0:
                # Discovery FAILED for this repo — fall back to servicing the
                # configured root itself, so its PRs aren't dropped on an error.
                _add(cwd)
                continue
            # rc 0: the owned set is authoritative for this repo (an EMPTY result
            # means "owns nothing here" — do NOT fall back to cwd, or the
            # live-owner gate's exclusions would be undone by servicing a foreign
            # worktree, BOU-1540 P1).
            for ln in out.stdout.splitlines():
                _add(ln.strip())
        return discovered
    # Every worktree on the machine, ACROSS every configured root (BOU-1546).
    # Span all --cwd values AND each one's maintenance_repo_roots so a machine-wide
    # gaia loop (no --session-id) services sibling repos too — previously this only
    # enumerated args.cwd[0], silently dropping extra --cwd and all config roots.
    from .maintenance_check import _resolve_maintenance_roots  # noqa: PLC0415

    roots: list[str] = []
    seen_roots: set[str] = set()
    for c in (args.cwd or ["."]):
        for r in _resolve_maintenance_roots(c):
            if r not in seen_roots:
                seen_roots.add(r)
                roots.append(r)
    paths: list[str] = []
    seen_paths: set[str] = set()
    for root in roots:
        out = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=root,
            capture_output=True, text=True,
        )
        for ln in out.stdout.splitlines():
            if ln.startswith("worktree "):
                p = ln.split(" ", 1)[1].strip()
                if p and p not in seen_paths:
                    seen_paths.add(p)
                    paths.append(p)
    return paths or list(args.cwd)


def _loop_pid() -> int:
    import os
    return os.getpid()


def _parse_pr_number(check_stdout: str) -> int | None:
    for line in reversed(check_stdout.splitlines()):
        if line.startswith("PR_NUMBER="):
            try:
                return int(line.split("=", 1)[1].strip())
            except ValueError:
                return None
    return None


def _parse_coordinator_claim_handle(
    check_stdout: str,
) -> coordinator.ClaimHandle | None:
    claim_id: str | None = None
    lease_epoch: int | None = None
    for line in check_stdout.splitlines():
        if line.startswith("COORDINATOR_CLAIM_ID="):
            claim_id = line.split("=", 1)[1].strip() or None
        elif line.startswith("COORDINATOR_LEASE_EPOCH="):
            try:
                lease_epoch = int(line.split("=", 1)[1].strip())
            except ValueError:
                return None
    if claim_id is None or lease_epoch is None:
        return None
    try:
        return coordinator.ClaimHandle(
            claim_id=claim_id,
            lease_epoch=lease_epoch,
        )
    except ValueError:
        return None


def _head_sha(cwd: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip()


def _decision_requested_during_dispatch(cwd: str, pr: int | None) -> bool:
    """Whether the executor left an unresolved human decision for this PR.

    BOU-2040 / BOU-2038: an executor that reaches a boundary it does not own
    records a coordinator decision request and stops. From out here that is
    indistinguishable from a failed run by exit code alone, so the coordinator
    ledger is the authority — deliberately not a stdout phrase, which an agent
    could emit accidentally or a log could swallow.

    The probe carries ``worktree_path`` and no url on purpose:
    ``_repo_slug_for_pr`` then resolves the slug from this worktree's config,
    with no subprocess and no network. Shelling out to ``gh repo view`` here
    would put a timeout-bounded child process on the failure path of every loop
    tick.

    Best-effort: any lookup error means we cannot prove a decision is pending,
    so the caller falls through to normal failure handling. Suppressing a real
    failure on a bad read would be the more dangerous mistake.
    """
    if pr is None:
        return False
    try:
        from . import coordinator as _coordinator_mod  # noqa: PLC0415
        from .models import PRData  # noqa: PLC0415

        probe = PRData(
            number=pr, title="", branch="", url="", worktree_path=cwd,
        )
        return _coordinator_mod.pending_decision_for_pr(probe) is not None
    except Exception:  # noqa: BLE001
        return False


def _baseline_sha(cwd: str, pr: int | None) -> str:
    """The PR branch's PUBLISHED head BEFORE the executor runs.

    ``complete --baseline`` counts only commits pushed *after* this point as the
    fix, so an executor that exits 0 without pushing can't resolve review
    threads. The authoritative "before" reference is the PR's remote head
    (``gh pr view --json headRefOid``), not the local ``HEAD`` — a worktree with
    unpushed local commits ahead of the PR would otherwise yield an empty fix
    range and leave addressed threads open. Falls back to the local HEAD when gh
    can't answer (offline / no PR resolved).
    """
    cmd = ["gh", "pr", "view"]
    if pr is not None:
        cmd.append(str(pr))
    cmd += ["--json", "headRefOid", "-q", ".headRefOid"]
    from agentic_pr_dash import github_api  # noqa: PLC0415
    try:
        # Bounded + guarded: a broken/missing gh on PATH raises OSError and an
        # interactive auth prompt would otherwise hang the whole loop before it
        # ever dispatches the executor. Either way, fall back to the local HEAD.
        out = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=15,
            env=github_api.automation_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return _head_sha(cwd)
    sha = out.stdout.strip()
    if out.returncode == 0 and sha:
        return sha
    return _head_sha(cwd)


def _executor_program(executor: str) -> str | None:
    """The first shell token of the executor template — the program to spawn.

    Returns None when the template is empty or shlex can't tokenize it (an
    unbalanced quote, etc.), which is itself a misconfiguration the caller treats
    as unresolvable.
    """
    try:
        tokens = shlex.split(executor)
    except ValueError:
        return None
    return tokens[0] if tokens else None


def _validate_executor(executor: str) -> str | None:
    """Return an error string if the configured executor can't be run, else None.

    Resolves the executor's program on PATH at loop STARTUP (BOU-1637) so a
    misconfigured command (typo, binary not installed) fails loudly immediately
    instead of being discovered only on the first dispatch tick — by which point
    a PR is already claimed and the loop spins releasing it every tick. An
    absolute/relative path is checked for existence + executability; a bare name
    is resolved via ``shutil.which``.
    """
    program = _executor_program(executor)
    if program is None:
        return (
            f"executor command is empty or unparseable: {executor!r}. "
            "Set a runnable command, e.g. executor = \"codex exec --full-auto {prompt}\"."
        )
    if os.sep in program or (os.altsep and os.altsep in program):
        if os.path.isfile(program) and os.access(program, os.X_OK):
            return None
        return (
            f"executor program {program!r} is not an executable file. "
            "Fix the path in agentic-pr-dash.toml / AGENTIC_PR_DASH_EXECUTOR."
        )
    if shutil.which(program) is None:
        return (
            f"executor program {program!r} was not found on PATH. "
            "Install it or correct the executor command in "
            "agentic-pr-dash.toml / AGENTIC_PR_DASH_EXECUTOR."
        )
    return None


def _run_executor(executor: str, prompt: str, cwd: str) -> int:
    """Run the configured executor with the prompt, in the worktree dir."""
    if "{prompt}" in executor:
        # Split the template, then inject the prompt as a single argv element so
        # it is never re-split by the shell.
        parts: list[str] = []
        for tok in shlex.split(executor):
            parts.append(prompt if tok == "{prompt}" else tok)
    else:
        parts = [*shlex.split(executor), prompt]
    return subprocess.run(parts, cwd=cwd).returncode


_SPAWN_FAILED_PREFIX = "spawn failed: "


def _try_run(executor: str, prompt: str, cwd: str) -> tuple[int | None, str]:
    """Run an executor; return ``(exit code | None, concrete error text)``.

    The executor binary may be missing from PATH (OSError/FileNotFoundError) or
    otherwise fail to launch. Returning ``None`` lets the caller treat "couldn't
    launch" the same as "ran and failed" for fallback purposes, without killing
    the whole loop on one bad spawn. The error text retains the CONCRETE cause
    (the spawn exception, or the exit code) instead of collapsing it (BOU-2086);
    it is ``""`` on success.
    """
    try:
        rc = _run_executor(executor, prompt, cwd)
    except Exception as exc:
        print(f"[agentic-pr-dash] could not launch executor: {exc}", file=sys.stderr)
        return None, f"{_SPAWN_FAILED_PREFIX}{exc}"
    return rc, ("" if rc == 0 else f"exit {rc}")


def _dispatch_with_fallback(
    primary: str, fallback: str, prompt: str, cwd: str, pr: int | None
) -> tuple[bool, dict[str, str]]:
    """Dispatch the fix to the primary executor, falling back on any failure.

    Chain (BOU-1734): run ``primary``; if it fails (non-zero exit or a failed
    spawn) and a ``fallback`` is configured, run the same prompt through the
    fallback; if BOTH fail, report a clear error rather than silently leaving the
    PR. Returns ``(serviced, errors)``: ``serviced`` is True when some executor
    serviced the PR (exit 0); ``errors`` maps ``"primary"``/``"fallback"`` to the
    concrete per-executor failure text (BOU-2086) — empty when serviced.
    """
    rc, err = _try_run(primary, prompt, cwd)
    if rc == 0:
        return True, {}
    errors = {"primary": f"{_executor_program(primary) or primary}: {err}"}
    if not fallback:
        # Legacy single-executor behavior: leave the PR for the next tick.
        print(f"[agentic-pr-dash] executor exited {rc}; leaving PR #{pr} for next tick", file=sys.stderr)
        return False, errors
    print(f"[agentic-pr-dash] primary executor failed (rc={rc}); falling back for PR #{pr}", file=sys.stderr)
    rc2, err2 = _try_run(fallback, prompt, cwd)
    if rc2 == 0:
        return True, {}
    errors["fallback"] = f"{_executor_program(fallback) or fallback}: {err2}"
    print(
        f"[agentic-pr-dash] ERROR: both executors failed for PR #{pr} "
        f"(primary={rc}, fallback={rc2}); leaving for next tick",
        file=sys.stderr,
    )
    return False, errors


def _cleanup_stale_no_pr_worktree(cwd: str, session_id: str = "") -> bool:
    """Remove a stale worktree with no open PR; return True when removed."""
    worktree = find_worktree_for_path(cwd)
    if not worktree:
        return False
    if os.path.abspath(cwd) in _live_independent_owner_paths([cwd], session_id):
        return False
    active_agents = discover_active_agents([cwd]).get(cwd, [])
    eligible, reason = selected_worktree_cleanup_reason(worktree, active_agents)
    if not eligible:
        return False
    removed, detail = remove_worktree(cwd)
    name = Path(cwd).name
    if removed:
        print(f"[agentic-pr-dash] cleaned stale no-PR worktree {name}: {reason}", file=sys.stderr)
        return True
    print(f"[agentic-pr-dash] failed to clean stale no-PR worktree {name}: {detail}", file=sys.stderr)
    return False


def _tick_executor_viability(executor: str, fallback: str) -> tuple[bool, dict[str, str]]:
    """Re-check executor resolvability for this tick (BOU-2086).

    Viable means the loop can dispatch AT LEAST one executor: the primary
    resolves, or a configured fallback resolves (a broken primary still gets
    serviced via the fallback chain). Returns ``(viable, errors)`` with the
    concrete per-executor validation errors retained.
    """
    errors: dict[str, str] = {}
    primary_err = _validate_executor(executor)
    if primary_err:
        errors["primary"] = primary_err
    fallback_err = None
    if fallback:
        fallback_err = _validate_executor(fallback)
        if fallback_err:
            errors["fallback"] = fallback_err
    viable = primary_err is None or (bool(fallback) and fallback_err is None)
    return viable, errors


def _tick(args, executor: str) -> None:
    fallback = getattr(args, "fallback_executor", "") or ""
    # Heartbeat + viability (BOU-2086): stamp each serviced repo's health file
    # once per tick so coverage readers can require a FRESH, executors-viable
    # loop rather than trusting a live pid alone.
    tick_viable, tick_errors = _tick_executor_viability(executor, fallback)
    interval = getattr(args, "interval", None)
    stamped_health_files: set[Path] = set()
    for cwd in _discover_cwds(args):
        if not Path(cwd).is_dir():
            continue
        # BOU-2210: one worktree's bad luck must never abort the tick (or, in
        # the daemon, the whole process). The coordinator facade can raise a
        # whole family of exceptions beyond StaleClaimError — PermissionError
        # (owner_session_id mismatch: the `check` subprocess claims as
        # `pid:<ancestor-agent-pid>` when the loop runs without --session-id,
        # while the loop heartbeats as `pid:<os.getpid()>`), KeyError (claims
        # .jsonl rotated between check and heartbeat), ValueError (the claim was
        # released when the PR went clean), OSError (fsync on a full disk) — and
        # git/gh/subprocess/IO can fail for their own reasons. Log and continue.
        try:
            _service_cwd(
                args,
                executor,
                fallback,
                cwd,
                interval=interval,
                tick_viable=tick_viable,
                tick_errors=tick_errors,
                stamped_health_files=stamped_health_files,
            )
        except Exception as exc:  # noqa: BLE001 — a daemon must survive one PR
            print(
                f"[agentic-pr-dash] tick for {cwd} failed: "
                f"{type(exc).__name__}: {exc} — continuing",
                file=sys.stderr,
            )
            traceback.print_exc()


def _service_cwd(
    args,
    executor: str,
    fallback: str,
    cwd: str,
    *,
    interval: int | None,
    tick_viable: bool,
    tick_errors: dict[str, str],
    stamped_health_files: set[Path],
) -> None:
    """Service exactly one worktree. Raising is fine — ``_tick`` isolates it."""
    try:
        hf = _health_file(cwd)
    except Exception:  # noqa: BLE001 — heartbeat is best-effort
        hf = None
    if hf is not None and hf not in stamped_health_files:
        stamped_health_files.add(hf)
        record_loop_health(
            cwd, executors_viable=tick_viable, errors=tick_errors, interval=interval,
            session_id=args.session_id or "",
            no_discover_worktrees=bool(getattr(args, "no_discover_worktrees", False)),
            once=bool(getattr(args, "once", False)),
        )
    if _cleanup_stale_no_pr_worktree(cwd, args.session_id or ""):
        return
    check = subprocess.run(
        [sys.executable, "-m", "agentic_pr_dash", "check",
         "--cwd", cwd, "--session-id", args.session_id or ""],
        capture_output=True, text=True,
    )
    if check.returncode != CHECK_WORK_FOUND:
        # 0 = clean/deferred, 2 = gh unavailable. A warn-only defer (a blocked
        # owned PR we deferred to its live owner without dispatching) is exit 0
        # by design, but must still be VISIBLE in loop output — otherwise the
        # detached loop's coverage looks clean while the PR is red (BOU-1788,
        # codex PR #48 review).
        notice = check.stdout.strip()
        is_warn_only = bool(notice and worktree_check.WARN_ONLY_MARKER in notice)
        if is_warn_only:
            print(f"[agentic-pr-dash] {notice}", file=sys.stderr)
        # On a genuinely clean result, drop any stale streak/escalation marker
        # for this PR (it recovered without an executor dispatch). Skip rc 2
        # (gh failure is not evidence of recovery) AND skip warn-only defers —
        # those are a KNOWN-blocked PR deferred to its live owner, not a
        # recovery, so clearing would hide a still-red PR (BOU-1789/BOU-1788).
        if check.returncode == 0 and not is_warn_only:
            _clear_recovered_streak(cwd)
        return
    pr = _parse_pr_number(check.stdout)
    claim_handle = _parse_coordinator_claim_handle(check.stdout)
    prompt = check.stdout
    baseline = _baseline_sha(cwd, pr)
    print(f"[agentic-pr-dash] PR #{pr} in {cwd} needs work — dispatching executor", file=sys.stderr)
    session = args.session_id or f"pid:{_loop_pid()}"
    if claim_handle:
        try:
            coordinator.heartbeat_claim(claim_handle, session)
        except StaleClaimError:
            return
    serviced, exec_errors = _dispatch_with_fallback(executor, fallback, prompt, cwd, pr)
    if not serviced and _decision_requested_during_dispatch(cwd, pr):
        # BOU-2040: the executor stopped because it owes a human an answer, not
        # because it failed. The coordinator ledger — not an exit code or a
        # stdout phrase — is the source of truth here (BOU-2038). Recording a
        # failure would burn a streak slot toward escalation and re-dispatch an
        # executor that will hit the same boundary and stop again.
        #
        # Release the claim NON-terminally so the PR is not left wrongly owned
        # while a human thinks, and so a replacement owner may pick the work up
        # after resolution. `completed` would be rejected by the coordinator
        # anyway while the decision is unresolved.
        if claim_handle:
            try:
                coordinator.release_claim(claim_handle, session, "waiting_human")
            except StaleClaimError:
                pass
        return
    if not serviced:
        # Primary (and fallback, if any) failed. Record the failure streak,
        # then release the claim so the PR is not left wrongly owned until
        # the lease expires, then move on. Retain the CONCRETE per-executor
        # errors in the streak record (BOU-2086) instead of a generic line.
        detail = "; ".join(f"{k}: {v}" for k, v in exec_errors.items())
        err_summary = (
            f"executor dispatch failed for PR #{pr}: {detail}"
            if detail else f"executor exit non-zero or spawn-failed for PR #{pr}"
        )
        # Every attempted executor failed to even SPAWN → the loop cannot
        # currently dispatch anything: downgrade this repo's viability record
        # immediately rather than waiting for the next tick's validation.
        if exec_errors and all(_SPAWN_FAILED_PREFIX in v for v in exec_errors.values()):
            record_loop_health(
                cwd, executors_viable=False, errors=exec_errors, interval=interval,
                session_id=args.session_id or "",
                no_discover_worktrees=bool(getattr(args, "no_discover_worktrees", False)),
                once=bool(getattr(args, "once", False)),
            )
        new_streak = record_executor_failure(cwd, pr, err_summary)
        _maybe_escalate(cwd, pr, err_summary, new_streak)
        if claim_handle:
            reason = "all_executors_failed" if fallback else "executor_failed"
            try:
                coordinator.release_claim(claim_handle, session, reason)
            except StaleClaimError:
                pass
        return
    complete_args = [sys.executable, "-m", "agentic_pr_dash", "complete", "--cwd", cwd, "--baseline", baseline]
    if pr is not None:
        complete_args += ["--pr", str(pr)]
    complete = subprocess.run(complete_args)
    if claim_handle:
        reason = "completed" if complete.returncode == 0 else "complete_failed"
        try:
            coordinator.release_claim(claim_handle, session, reason)
        except StaleClaimError:
            # Someone fenced the claim out from under us mid-flight. The
            # dispatch + complete still SUCCEEDED, so the failure streak must
            # still be reset (BOU-2210): the old `continue` skipped the reset
            # below, leaving a stale streak that could trip a 3-strike
            # escalation on only 2 genuinely-earned failures.
            pass
    # Reset the failure streak on a successful dispatch + complete.
    reset_executor_failure(cwd, pr)


def _write_loop_pidfile(pidfile: Path | None) -> None:
    """Stamp this loop's pid so the stop-gate's ``_detached_loop_alive`` can see
    it (BOU-1653). Best-effort: if it can't be written, coverage detection just
    won't treat the loop as alive."""
    if pidfile is None:
        return
    try:
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def _remove_loop_pidfile(pidfile: Path | None) -> None:
    """Remove the pidfile on exit — but only if it still holds OUR pid, so we
    never delete a file an external supervisor has re-stamped."""
    if pidfile is None:
        return
    try:
        if pidfile.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pidfile.unlink()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser(prog="agentic-pr-dash loop", description=__doc__)
    parser.add_argument("--interval", type=int, default=600, help="Seconds between ticks (default 600).")
    parser.add_argument("--cwd", action="append", default=None, help="Worktree root (repeatable; default '.').")
    parser.add_argument("--session-id", default="", help="Scope discovery to worktrees this session owns.")
    parser.add_argument("--no-discover-worktrees", action="store_true", help="Use only --cwd values; don't enumerate worktrees.")
    parser.add_argument("--once", action="store_true", help="Run a single tick and exit.")
    parser.add_argument("--executor", default=None, help="Override the configured executor command.")
    parser.add_argument("--fallback-executor", default=None,
                        help="Executor to run when the primary fails (per PR). Defaults to config fallback_executor.")
    args = parser.parse_args(argv)
    args.cwd = args.cwd or ["."]
    args.fallback_executor = args.fallback_executor or cfg.fallback_executor

    executor = args.executor or cfg.executor
    if not executor:
        print(
            "agentic-pr-dash loop needs an executor. Set it via:\n"
            "  agentic-pr-dash.toml  ->  executor = \"claude --dangerously-skip-permissions -p {prompt}\"\n"
            "  env                ->  AGENTIC_PR_DASH_EXECUTOR=...\n"
            "  flag               ->  --executor '...'",
            file=sys.stderr,
        )
        return 2

    # Validate the executor up-front (BOU-1637): catch a missing/misconfigured
    # command at startup, not on the first dispatch tick (where a PR would already
    # be claimed). Fail loudly with exit 2 so a supervisor sees the misconfig.
    # Validate BOTH the primary and the fallback before exiting (BOU-2086) so a
    # DEGRADED health record retaining every concrete error is persisted — the
    # wrapper daemon's pid can stay "running" after this exit, and pid-alive
    # alone must never read as coverage.
    startup_errors: dict[str, str] = {}
    executor_error = _validate_executor(executor)
    if executor_error:
        startup_errors["primary"] = executor_error
        print(f"agentic-pr-dash loop: {executor_error}", file=sys.stderr)

    # Validate the fallback the same way when one is configured, so a broken
    # fallback (typo, uninstalled agent) is caught at startup rather than only
    # when the primary first fails and the fallback can't rescue the PR (BOU-1734).
    if args.fallback_executor:
        fallback_error = _validate_executor(args.fallback_executor)
        if fallback_error:
            startup_errors["fallback"] = fallback_error
            print(f"agentic-pr-dash loop (fallback): {fallback_error}", file=sys.stderr)

    if startup_errors:
        for cwd in args.cwd:
            try:
                record_loop_health(
                    cwd, executors_viable=False, errors=startup_errors,
                    interval=args.interval, session_id=args.session_id or "",
                    no_discover_worktrees=bool(getattr(args, "no_discover_worktrees", False)),
                    once=bool(getattr(args, "once", False)),
                )
            except Exception:  # noqa: BLE001 — never mask the loud exit
                pass
        return 2

    if args.once:
        _tick(args, executor)
        return 0
    # Long-running daemon: publish our pid so the stop-gate can detect live
    # detached coverage (BOU-1653).
    _write_loop_pidfile(cfg.maintenance_loop_pidfile)
    try:
        while True:
            # Belt-and-braces over the per-cwd guard in `_tick` (BOU-2210): a
            # fault OUTSIDE the per-cwd body — worktree discovery, executor
            # viability probing — must not kill the daemon either. Only
            # `Exception` is caught, so KeyboardInterrupt/SystemExit still stop
            # the loop as a supervisor expects.
            try:
                _tick(args, executor)
            except Exception as exc:  # noqa: BLE001 — the daemon must survive
                print(
                    f"[agentic-pr-dash] loop tick failed: "
                    f"{type(exc).__name__}: {exc} — retrying next interval",
                    file=sys.stderr,
                )
                traceback.print_exc()
            time.sleep(max(5, args.interval))
    finally:
        _remove_loop_pidfile(cfg.maintenance_loop_pidfile)


if __name__ == "__main__":
    raise SystemExit(main())
