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
import os
import shlex
import shutil
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path

from . import coordinator
from ._maintenance import worktree_check
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

    Uses ``resolved_repo`` (canonical ``owner/name``) so the loop writer and the
    stop-gate / orchestrator readers derive the SAME slug regardless of whether
    they run from the worktree or the main checkout. Cached because that resolve
    can shell out to ``gh``/``git`` and is hit on every streak read.
    """
    repo = load_config(cwd).resolved_repo(Path(cwd))
    raw = repo or Path(cwd).resolve().name or "repo"
    return "".join(c if (c.isalnum() or c in "-_.") else "-" for c in raw)


def _health_file(cwd: str) -> Path:
    """Path to the per-repo loop health JSON file."""
    return _daemon_dir(cwd) / f"pr-maintenance-loop.health.{_repo_slug(cwd)}.json"


def _load_health(cwd: str) -> dict:
    """Load the health JSON, or {} on missing/corrupt."""
    try:
        raw = _health_file(cwd).read_text(encoding="utf-8")
        data = __import__("json").loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
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
    data = _load_health(cwd)
    new_streak = _entry_streak(data.get(key, {})) + 1
    data[key] = {"streak": new_streak, "last_error": err, "updated": time.time()}
    _save_health(cwd, data)
    _emit_loop_event(cwd, "streak", pr, {"streak": new_streak, "last_error": err[:200]})
    return new_streak


def _escalated_marker_path(cwd: str) -> Path:
    """Path to the per-repo escalation marker JSON (same daemon dir as health).

    Repo-scoped for the same reason as :func:`_health_file` — so PRs with the
    same number in different repos don't share an escalation marker.
    """
    return _daemon_dir(cwd) / f"pr-maintenance-loop.escalated.{_repo_slug(cwd)}.json"


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
    data = _load_health(cwd)
    if key in data:
        del data[key]
        _save_health(cwd, data)
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
    if not _load_health(cwd) and not _load_escalation(cwd):
        return
    import json as _json  # noqa: PLC0415
    try:
        r = subprocess.run(
            ["gh", "pr", "view", "--json", "number"],
            cwd=cwd, capture_output=True, text=True, timeout=30,
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
    """True when the detached loop is alive AND the failure streak < threshold.

    When the streak reaches the threshold the loop has repeatedly failed to fix
    the PR, so it no longer counts as coverage — the stop-gate must force a
    per-session waiter to bring the issue to the user.
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


def _parse_coordinator_claim_id(check_stdout: str) -> str | None:
    for line in reversed(check_stdout.splitlines()):
        if line.startswith("COORDINATOR_CLAIM_ID="):
            claim_id = line.split("=", 1)[1].strip()
            return claim_id or None
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
    try:
        # Bounded + guarded: a broken/missing gh on PATH raises OSError and an
        # interactive auth prompt would otherwise hang the whole loop before it
        # ever dispatches the executor. Either way, fall back to the local HEAD.
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=15)
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


def _try_run(executor: str, prompt: str, cwd: str) -> int | None:
    """Run an executor; return its exit code, or ``None`` if it couldn't spawn.

    The executor binary may be missing from PATH (OSError/FileNotFoundError) or
    otherwise fail to launch. Returning ``None`` lets the caller treat "couldn't
    launch" the same as "ran and failed" for fallback purposes, without killing
    the whole loop on one bad spawn.
    """
    try:
        return _run_executor(executor, prompt, cwd)
    except Exception as exc:
        print(f"[agentic-pr-dash] could not launch executor: {exc}", file=sys.stderr)
        return None


def _dispatch_with_fallback(primary: str, fallback: str, prompt: str, cwd: str, pr: int | None) -> bool:
    """Dispatch the fix to the primary executor, falling back on any failure.

    Chain (BOU-1734): run ``primary``; if it fails (non-zero exit or a failed
    spawn) and a ``fallback`` is configured, run the same prompt through the
    fallback; if BOTH fail, report a clear error rather than silently leaving the
    PR. Returns ``True`` when some executor serviced the PR (exit 0).
    """
    rc = _try_run(primary, prompt, cwd)
    if rc == 0:
        return True
    if not fallback:
        # Legacy single-executor behavior: leave the PR for the next tick.
        print(f"[agentic-pr-dash] executor exited {rc}; leaving PR #{pr} for next tick", file=sys.stderr)
        return False
    print(f"[agentic-pr-dash] primary executor failed (rc={rc}); falling back for PR #{pr}", file=sys.stderr)
    rc2 = _try_run(fallback, prompt, cwd)
    if rc2 == 0:
        return True
    print(
        f"[agentic-pr-dash] ERROR: both executors failed for PR #{pr} "
        f"(primary={rc}, fallback={rc2}); leaving for next tick",
        file=sys.stderr,
    )
    return False


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


def _tick(args, executor: str) -> None:
    fallback = getattr(args, "fallback_executor", "") or ""
    for cwd in _discover_cwds(args):
        if not Path(cwd).is_dir():
            continue
        if _cleanup_stale_no_pr_worktree(cwd, args.session_id or ""):
            continue
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
            continue
        pr = _parse_pr_number(check.stdout)
        claim_id = _parse_coordinator_claim_id(check.stdout)
        prompt = check.stdout
        baseline = _baseline_sha(cwd, pr)
        print(f"[agentic-pr-dash] PR #{pr} in {cwd} needs work — dispatching executor", file=sys.stderr)
        session = args.session_id or f"pid:{_loop_pid()}"
        if claim_id:
            coordinator.heartbeat_claim_id(claim_id, session)
        if not _dispatch_with_fallback(executor, fallback, prompt, cwd, pr):
            # Primary (and fallback, if any) failed. Record the failure streak,
            # then release the claim so the PR is not left wrongly owned until
            # the lease expires, then move on.
            err_summary = f"executor exit non-zero or spawn-failed for PR #{pr}"
            new_streak = record_executor_failure(cwd, pr, err_summary)
            _maybe_escalate(cwd, pr, err_summary, new_streak)
            if claim_id:
                reason = "all_executors_failed" if fallback else "executor_failed"
                coordinator.release_claim_id(claim_id, session, reason)
            continue
        complete_args = [sys.executable, "-m", "agentic_pr_dash", "complete", "--cwd", cwd, "--baseline", baseline]
        if pr is not None:
            complete_args += ["--pr", str(pr)]
        complete = subprocess.run(complete_args)
        if claim_id:
            reason = "completed" if complete.returncode == 0 else "complete_failed"
            coordinator.release_claim_id(claim_id, session, reason)
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
    executor_error = _validate_executor(executor)
    if executor_error:
        print(f"agentic-pr-dash loop: {executor_error}", file=sys.stderr)
        return 2

    # Validate the fallback the same way when one is configured, so a broken
    # fallback (typo, uninstalled agent) is caught at startup rather than only
    # when the primary first fails and the fallback can't rescue the PR (BOU-1734).
    if args.fallback_executor:
        fallback_error = _validate_executor(args.fallback_executor)
        if fallback_error:
            print(f"agentic-pr-dash loop (fallback): {fallback_error}", file=sys.stderr)
            return 2

    if args.once:
        _tick(args, executor)
        return 0
    # Long-running daemon: publish our pid so the stop-gate can detect live
    # detached coverage (BOU-1653).
    _write_loop_pidfile(cfg.maintenance_loop_pidfile)
    try:
        while True:
            _tick(args, executor)
            time.sleep(max(5, args.interval))
    finally:
        _remove_loop_pidfile(cfg.maintenance_loop_pidfile)


if __name__ == "__main__":
    raise SystemExit(main())
