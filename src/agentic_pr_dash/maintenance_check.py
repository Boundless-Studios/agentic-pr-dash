"""Runtime-agnostic PR maintenance check CLI — stateless, read-only worker.

Subcommands:
  check    — resolve branch→PR, compute live blockers, print prompt, exit 10
             if work exists, else exit 0. READ-ONLY: writes no files.
  complete — re-fetch unresolved review threads from GitHub and resolve them
             statelessly (no ledger required). Best-effort close the bead.

Run via:  agentic-pr-dash <subcommand> [args]

Module-level imports are stdlib only; heavy deps are deferred into subcommand
functions so that ``--help`` works without the project venv's heavy deps.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

from .config import load as load_config


# ---------------------------------------------------------------------------
# Branch/PR resolution
# ---------------------------------------------------------------------------


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


_GH_UNAVAILABLE = object()  # sentinel: gh CLI failed


def _repo_slug(cwd: str) -> str:
    """GitHub ``owner/name`` for the checkout at ``cwd``, or "" if undetectable.

    Used to scope ledger/claim records so a session that spans multiple repos
    (different ``--cwd`` checkouts) never has a same-number PR in one repo
    clobber or mis-resolve another's (PR #16 review round 2, P1). Best-effort:
    an empty slug degrades to the legacy repo-less (unscoped) behavior.
    """
    from pathlib import Path  # noqa: PLC0415
    from .config import _detect_repo  # noqa: PLC0415
    try:
        return _detect_repo(Path(cwd)) or ""
    except Exception:  # noqa: BLE001
        return ""


def _resolve_pr_for_branch(cwd: str):
    """Find the open PR whose headRefName matches the current branch.

    Returns a populated PRData, None (no PR), or _GH_UNAVAILABLE sentinel.
    """
    from . import github_api  # noqa: PLC0415
    from .models import PRData, PRStatus  # noqa: PLC0415

    branch = _current_branch(cwd)
    if not branch:
        return None

    prs = github_api.list_open_prs(cwd)
    if prs is None:
        return _GH_UNAVAILABLE
    if not prs:
        return None

    raw: dict | None = None
    for entry in prs:
        if entry.get("headRefName") == branch:
            raw = entry
            break
    if raw is None:
        return None

    pr_number = int(raw["number"])
    latest_sha, latest_date = github_api.get_latest_commit(pr_number, cwd)
    checks = github_api.get_ci_checks(pr_number, cwd)
    failing = [
        c.name
        for c in checks
        if c.conclusion == "failure" and not github_api._is_infra_check(c.name)
    ]
    review_comments = github_api.get_unaddressed_comments(pr_number, latest_date, cwd)
    merge_state = raw.get("mergeStateStatus", "unknown")
    mergeable = raw.get("mergeable", "unknown")

    return PRData(
        number=pr_number,
        title=raw.get("title", ""),
        branch=branch,
        base_branch=raw.get("baseRefName", "main"),
        url=raw.get("url", ""),
        is_draft=bool(raw.get("isDraft", False)),
        merge_state=merge_state,
        mergeable=mergeable,
        ci_checks=checks,
        failing_checks=failing,
        review_comments=review_comments,
        latest_commit_sha=latest_sha,
        latest_commit_date=latest_date,
        worktree_path=cwd,
        status=PRStatus.CLEAN,
    )


def _resolve_pr_by_number(pr_number: int, cwd: str):
    """Resolve a PR by explicit number (for --pr override).

    Returns a populated PRData, or the _GH_UNAVAILABLE sentinel when the gh CLI
    failed (outage/rate-limit).
    """
    from . import github_api  # noqa: PLC0415
    from .models import PRData, PRStatus  # noqa: PLC0415

    prs = github_api.list_open_prs(cwd)
    if prs is None:
        return _GH_UNAVAILABLE
    raw: dict | None = None
    if prs:
        for entry in prs:
            if entry.get("number") == pr_number:
                raw = entry
                break

    latest_sha, latest_date = github_api.get_latest_commit(pr_number, cwd)
    checks = github_api.get_ci_checks(pr_number, cwd)
    failing = [
        c.name
        for c in checks
        if c.conclusion == "failure" and not github_api._is_infra_check(c.name)
    ]
    review_comments = github_api.get_unaddressed_comments(pr_number, latest_date, cwd)
    merge_state = (raw or {}).get("mergeStateStatus", "unknown")
    mergeable = (raw or {}).get("mergeable", "unknown")

    return PRData(
        number=pr_number,
        title=(raw or {}).get("title", ""),
        branch=(raw or {}).get("headRefName", ""),
        base_branch=(raw or {}).get("baseRefName", "main"),
        url=(raw or {}).get("url", ""),
        is_draft=bool((raw or {}).get("isDraft", False)),
        merge_state=merge_state,
        mergeable=mergeable,
        ci_checks=checks,
        failing_checks=failing,
        review_comments=review_comments,
        latest_commit_sha=latest_sha,
        latest_commit_date=latest_date,
        worktree_path=cwd,
        status=PRStatus.CLEAN,
    )


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


# How long an owner's loop heartbeat stays "fresh" (the ownership lease).
#
# The heartbeat is stamped at the START of each `check`, but when `check` finds
# work the owning session then spends the whole fix phase (resolve conflicts /
# fix CI / run tests / push) BEFORE its next `/loop` tick re-stamps it — easily
# tens of minutes. So the lease MUST comfortably exceed the maximum expected
# maintenance runtime, or the detached loop would treat a still-busy owner as
# stale mid-fix and dispatch a competing runtime — the exact double-fix race this
# coordination prevents (ownership-coordination review).
#
# A generous lease is safe because it is NOT the dead-session signal: pid-liveness
# (`_pid_alive`) is, and it reaps a crashed/exited owner immediately regardless of
# this value. The lease only bounds the rare "owner alive but loop genuinely
# stopped" case. Override with GAIA_PR_WATCH_LEASE_SECONDS.
# Two ownership windows (PR #1909 review). The detached loop defers to a live
# in-session owner while EITHER is current:
#   * heartbeat  — "the loop is alive and ticking". Stamped on EVERY owner check
#     (incl. clean ones), SHORT TTL (~a few loop ticks). A clean check therefore
#     only holds ownership briefly, so a stopped-but-alive loop releases quickly
#     instead of pinning the long lease.
#   * fix lease  — "a fix is actively in progress". Stamped ONLY when a check
#     finds work (the owner is about to spend a long, tick-less fix phase), long
#     enough to cover it. A clean check never grants this long lease.
# pid-liveness (`_pid_alive`) still reaps a crashed owner immediately regardless.
_HEARTBEAT_TTL_SECONDS = 600       # 10 min — alive-and-ticking window (3m loop + slack)
_DEFAULT_FIX_LEASE_SECONDS = 1800  # 30 min — covers a long fix phase; override via env


def _fix_lease_seconds() -> int:
    raw = os.environ.get("GAIA_PR_WATCH_LEASE_SECONDS", "")
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return load_config().lease_seconds


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


def _pid_alive(pid_raw: str) -> bool:
    if not pid_raw.isdigit():
        return False
    try:
        os.kill(int(pid_raw), 0)  # signal 0: existence probe only
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by another uid
    except OSError:
        return False
    return True


def _resolve_owner_pid() -> int:
    """Best-effort durable owner pid to stamp into an adopted/armed marker.

    When `--pid` is omitted, `os.getppid()` is the SHORT-LIVED shell that ran this
    CLI — it exits the instant the command returns, so the marker would instantly
    look dead to the ownership gate and a sibling/detached loop could grab the
    same PR (PR #1949 review, P2). Walk up the process ancestry to the nearest
    `claude` process (the session that owns the in-session loop) and use that
    durable pid; fall back to `os.getppid()` when no such ancestor is found (e.g.
    the detached codex loop, which should pass `--pid $$` explicitly).
    """
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
        if "claude" in os.path.basename(comm.strip()).lower():
            return pid
        if not ppid_str.isdigit():
            break
        pid = int(ppid_str)
    return start


def _pr_draft_status(cwd: str, pr_number: int):
    """Optional[bool]: True=draft, False=non-draft, None=could-not-determine
    (gh unavailable / error / malformed JSON / field absent).

    The explicit `arm --pr <N>` path FAILS CLOSED on None — it never arms a PR
    whose non-draft status can't be positively confirmed, so the "drafts are
    never armed" guarantee holds even when gh is down or unauthenticated
    (PR #1949 review follow-up). This mirrors the gh-resolved path, which also
    declines to arm when it cannot list the PR.
    """
    import json  # noqa: PLC0415

    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "isDraft"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "")
    except ValueError:
        return None
    if not isinstance(data, dict) or "isDraft" not in data:
        return None
    return bool(data["isDraft"])


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


def _heartbeat_ttl_seconds(cwd: str | None = None) -> int:
    """How long an owner's heartbeat counts as 'fresh'. Configurable so the
    recovery latency can be tuned for the turn-driven era (BOU-1478).

    Precedence: the modern AGENTIC_PR_DASH_HEARTBEAT_TTL_SECONDS env wins, then
    the per-worktree config (agentic-pr-dash.toml at ``cwd``), and only when no
    modern setting exists does the legacy GAIA_PR_WATCH_HEARTBEAT_TTL apply — so
    setting the new knob always takes effect even with stale legacy env around.
    """
    cfg = load_config(cwd) if cwd else load_config()
    # 1. Modern env wins.
    modern = os.environ.get("AGENTIC_PR_DASH_HEARTBEAT_TTL_SECONDS", "")
    if modern.isdigit() and int(modern) > 0:
        return int(modern)
    # 2. The CHECKED worktree's config (agentic-pr-dash.toml) beats the legacy
    #    env — a per-repo setting must not be overridden by stale shell state.
    #    The loop runs `check --cwd <worktree>` without changing subprocess cwd,
    #    so cfg is loaded for the target worktree.
    toml_ttl = cfg.extra.get("heartbeat_ttl_seconds")
    if isinstance(toml_ttl, int) and toml_ttl > 0:
        return toml_ttl
    # 3. Legacy env fallback, then the config default.
    legacy = os.environ.get("GAIA_PR_WATCH_HEARTBEAT_TTL", "")
    if legacy.isdigit() and int(legacy) > 0:
        return int(legacy)
    return cfg.heartbeat_ttl_seconds


def _heartbeat_fresh(heartbeat: str, cwd: str | None = None) -> bool:
    """True if the owner's loop heartbeat is within the short alive-TTL of now —
    proof the in-session loop is still ticking, not merely armed or stopped.
    """
    ts = _parse_iso(heartbeat)
    if ts is None:
        return False
    from datetime import datetime, timezone  # noqa: PLC0415

    return (datetime.now(timezone.utc) - ts).total_seconds() <= _heartbeat_ttl_seconds(cwd)


def _fix_lease_active(lease_until: str) -> bool:
    """True if an in-progress fix lease (an absolute future timestamp the owner
    stamps when a check finds work) has not yet expired — the owner is mid-fix and
    not ticking, so the detached loop must keep deferring through the fix phase.
    """
    ts = _parse_iso(lease_until)
    if ts is None:
        return False
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc) < ts


def _live_foreign_owner(cwd: str, self_session_id: str) -> str | None:
    """Session id of a live, ACTIVELY-LOOPING in-session owner, else None.

    Coordination (ownership-coordination): the detached pr-maintenance loop must not
    service a worktree that a live in-session `/pr-maintenance-check` loop is
    already working, or they apply simultaneous conflicting fixes. Ownership is
    the `pr-watch.armed` marker. Defers ONLY when the owner is (a) a
    different session, (b) its pid is alive, AND (c) its loop `heartbeat=` is
    fresh — proof a loop is actually running, not merely armed. So a dead owner,
    a stale/absent heartbeat (armed-but-never-looped, or an ignored advisory
    nudge), an unowned worktree, or the caller itself never strands the detached
    fallback. Stdlib only: keeps `check` dependency-free.
    """
    fields = _read_marker(cwd)
    if fields is None:
        return None
    owner = fields.get("session_id", "")
    if not owner or owner == (self_session_id or ""):
        return None
    if not _pid_alive(fields.get("pid", "")):
        return None
    # Defer while the loop is provably engaged: actively ticking (heartbeat) OR
    # mid-fix (fix lease). A merely-armed marker (no heartbeat, no lease) or a
    # stopped loop does NOT block the detached fallback.
    if _heartbeat_fresh(fields.get("heartbeat", ""), cwd) or _fix_lease_active(fields.get("fix_lease_until", "")):
        return owner
    return None


def _marker_live_foreign_pid(cwd: str, self_session_id: str) -> bool:
    """True if the worktree's marker names a DIFFERENT session whose pid is still
    alive — even before that session has stamped its first heartbeat.

    The reconciliation ADOPTION path must treat such a worktree as OWNED. This is
    deliberately STRICTER than `_live_foreign_owner` (which also requires a fresh
    heartbeat / fix-lease): when another session has JUST opened/readied a PR, its
    hook writes a marker with a live pid but no heartbeat yet, and we must never
    overwrite that live session's marker and steal its PR (PR #1949 review, P1).
    A dead pid (crashed/exited session) is NOT a live owner, so genuine orphans
    and sub-agent worktrees (no marker at all) are still adopted.
    """
    fields = _read_marker(cwd)
    if fields is None:
        return False
    owner = fields.get("session_id", "")
    if not owner or owner == (self_session_id or ""):
        return False
    return _pid_alive(fields.get("pid", ""))


def _touch_owner_heartbeat(cwd: str, self_session_id: str, work_found: bool) -> None:
    """Refresh this owner's coordination stamps in the marker (owner-only write).

    `check`'s only write — coordination stamps, not PR state. ALWAYS refresh
    `heartbeat` (proof the loop is alive and ticking). When the check found work,
    ALSO set `fix_lease_until` = now + fix-lease (the owner is about to spend a
    tick-less fix phase, so the detached loop must keep deferring through it); a
    clean check CLEARS any prior `fix_lease_until`, so a stopped-but-alive loop
    never pins the long lease without a fix in progress (PR #1909 review).
    """
    if not self_session_id:
        return
    fields = _read_marker(cwd)
    if fields is None or fields.get("session_id", "") != self_session_id:
        return
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    fields["heartbeat"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if work_found:
        fields["fix_lease_until"] = (now + timedelta(seconds=_fix_lease_seconds())).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    else:
        fields.pop("fix_lease_until", None)
    # Write atomically (temp file in the same dir + os.replace) so a concurrent
    # detached-loop reader never observes a truncated/partial marker — a torn read
    # would make _live_foreign_owner return None and let it dispatch a second
    # fixer for the same PR (ownership-coordination review).
    import tempfile  # noqa: PLC0415

    content = "".join(f"{k}={v}\n" for k, v in fields.items())
    target = _marker_path(cwd)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".pr-watch.armed.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, target)  # atomic on POSIX (same filesystem)
    except OSError:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _write_arm_marker(cwd: str, session_id: str, pid: int, pr_number: int) -> bool:
    """Write the pr-watch.armed ownership marker (the single writer).

    Produces the EXACT same marker `.claude/hooks/arm-pr-watch.sh` writes
    (pr=/armed_at=/session_id=/pid=), atomically (tempfile + os.replace) so a
    concurrent reader never sees a torn marker. Also writes pr-watch.session
    so the worktree self-identifies. Reused by both the explicit `arm` subcommand
    and `list-owned` reconciliation (BOU-1442). Returns True on success.
    """
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
        os.replace(tmp, target)  # atomic on POSIX (same filesystem)
    except OSError:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False

    # Self-identify so the in-session loop / a later list-owned can read the owner.
    try:
        with open(os.path.join(state_dir, "pr-watch.session"), "w", encoding="utf-8") as fh:
            fh.write(session_id + "\n")
    except OSError:
        pass

    # Durably record the session->PR membership OUTSIDE the worktree so teardown
    # never drops it from monitoring (BOU-1587). Non-fatal: the marker remains the
    # live source of truth if this fails.
    try:
        from . import session_ledger  # noqa: PLC0415
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
    except Exception:  # noqa: BLE001 — ledger is best-effort
        pass
    return True


def _gh_pr_list_json(cwd: str, extra_args: list[str], fields: str) -> list | None:
    """Run `gh pr list --author @me --state open --json <fields> <extra>` and
    return the parsed JSON list, or None when gh is unavailable / errors.

    Stdlib only (subprocess + json) so the subcommands stay dependency-free —
    no `--jq` (bash-jq parity is brittle); we parse the raw JSON in Python.
    """
    import json  # noqa: PLC0415

    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--author", "@me", "--state", "open", *extra_args, "--json", fields],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "[]")
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def _resolve_open_pr_for_branch(cwd: str, branch: str):
    """(pr_number, is_draft) for this branch's open @me PR, or None if there is none."""
    data = _gh_pr_list_json(cwd, ["--head", branch], "number,isDraft")
    if not data:
        return None
    entry = data[0]
    return int(entry.get("number")), bool(entry.get("isDraft", False))


def _list_my_open_prs(cwd: str) -> dict[str, tuple[int, bool]]:
    """Map branch -> (pr_number, is_draft) for the user's open PRs; {} on gh failure."""
    data = _gh_pr_list_json(cwd, [], "number,headRefName,isDraft")
    if not data:
        return {}
    out: dict[str, tuple[int, bool]] = {}
    for entry in data:
        branch = entry.get("headRefName")
        if not branch:
            continue
        number = int(entry.get("number"))
        is_draft = bool(entry.get("isDraft", False))
        existing = out.get(branch)
        if existing is not None and not existing[1] and is_draft:
            continue
        out[branch] = (number, is_draft)
    return out


def _unresolved_review_threads(pr_number: int, cwd: str):
    """Non-outdated, unresolved review threads for a PR (BOU-1587 AC #4).

    Outdated threads (the code they pointed at changed) and resolved threads are
    excluded — they are not actionable feedback.
    """
    from . import github_api  # noqa: PLC0415

    threads = github_api.get_review_threads(pr_number, cwd)
    return [t for t in threads if not t.is_resolved and not t.is_outdated]


def pr_has_unresolved_review_threads(pr_number: int, cwd: str) -> bool:
    """True if the PR has at least one non-outdated, unresolved review thread.

    Used to keep a PR out of any ready-to-merge batch even when CI is green
    (BOU-1587 AC #4). Outdated threads (the code they pointed at changed) and
    resolved threads do not count.
    """
    return bool(_unresolved_review_threads(pr_number, cwd))


def _review_comments_from_threads(threads) -> list:
    """Build ReviewComment records from review threads so the maintenance prompt
    can render file/line/body details (PR #16 review round 2, P2).

    Mirrors the inline-thread shape `github_api.get_unaddressed_comments`
    produces, so a thread surfaced by the authoritative `_unresolved_review_threads`
    check is hydrated identically to one that came through the normal path.
    """
    from .models import ReviewComment  # noqa: PLC0415

    out = []
    for t in threads:
        top = t.top
        out.append(ReviewComment(
            id=top.database_id,
            author=top.author,
            body=top.body,
            path=top.path,
            line=top.line,
            created_at=top.created_at,
            is_inline=True,
            thread_id=t.node_id,
        ))
    return out


def _cmd_arm(args: argparse.Namespace) -> int:
    """Explicitly register a worktree's open non-draft PR under a session.

    The deterministic counterpart to the PostToolUse hook for PRs opened by
    DISPATCHED SUB-AGENTS (whose `gh pr create` never fires the parent's hook):
    an orchestrator calls this for each sub-agent worktree so the parent's
    `/pr-maintenance-check` loop picks it up via `list-owned` (BOU-1442).
    """
    cwd = os.path.abspath(args.cwd)
    session_id = args.session_id
    pid = args.pid if args.pid is not None else _resolve_owner_pid()

    pr_number = args.pr
    if pr_number is None:
        branch = _current_branch(cwd)
        if not branch:
            print("could not resolve branch; nothing to arm")
            return 0
        resolved = _resolve_open_pr_for_branch(cwd, branch)
        if resolved is None:
            print("no open PR for this branch; nothing to arm")
            return 0
        pr_number, is_draft = resolved
        if is_draft:
            print(f"PR #{pr_number} is a draft; not arming")
            return 0
    else:
        # Explicit --pr honors the draft gate, FAIL CLOSED: only arm when gh
        # positively confirms the PR is NOT a draft. If the status can't be
        # determined (gh down / unauthenticated), decline rather than risk arming
        # a draft (PR #1949 review follow-up).
        status = _pr_draft_status(cwd, int(pr_number))
        if status is None:
            print(f"could not verify PR #{pr_number} is non-draft (gh unavailable); not arming")
            return 0
        if status:
            print(f"PR #{pr_number} is a draft; not arming")
            return 0

    if _write_arm_marker(cwd, session_id, int(pid), int(pr_number)):
        print(f"armed PR #{pr_number} for session {session_id} in {cwd}")
        return 0
    # A failed marker write means the registration did NOT take — exit non-zero
    # so the calling automation can detect it and retry/stop (PR #1949 review).
    print(f"could not write arm marker in {cwd}", file=sys.stderr)
    return 1


def _check_worktree(cwd: str, self_session_id: str, *, claim: bool = True) -> tuple[int, str]:
    """Read-only blocker check for ONE worktree. Returns ``(exit_code, text)``.

    The single shared core of the ``check`` CLI and the ``stop-gate`` Stop hook:
      * 0  — clean / deferred-to-live-owner / no PR / draft (``text`` explains).
      * 2  — gh unavailable.
      * 10 — work pending; ``text`` is the self-contained maintenance prompt
             (ending in ``PR_NUMBER=<n>``).

    READ-ONLY except for the owner heartbeat/lease stamp (same as before the
    extraction). ``check`` prints ``text`` and returns the code; ``stop-gate``
    aggregates the exit-10 texts across owned worktrees.

    ``claim`` controls whether a found-work path creates an agent-coordinator
    claim. The ``check`` CLI (loop dispatch) claims so the loop owns the fix and
    later heartbeats/releases it. The Stop-hook ``stop-gate`` path is PASSIVE —
    it only prints a prompt to the interactive session and never releases — so it
    passes ``claim=False``; a stop-gate-created claim would survive the idle
    session and suppress the very work it just surfaced on the next Stop attempt
    (codex P1). When ``claim=False`` the active-claim suppression check below is
    also skipped so the passive probe always reports the pending work.
    """
    from . import coordinator, github_api, maintenance  # noqa: PLC0415

    cwd = os.path.abspath(cwd)

    # Ownership gate — defer to a live, actively-looping in-session owner before
    # doing any work (cheap, before resolving the PR). See _live_foreign_owner.
    owner = _live_foreign_owner(cwd, self_session_id)
    if owner is not None:
        return 0, f"deferring to live PR-watch owner session {owner}"

    # Resolve PR
    pr = _resolve_pr_for_branch(cwd)

    if pr is _GH_UNAVAILABLE:
        return 2, "could not list PRs (gh unavailable)"
    if pr is None:
        return 0, "no open PR for this branch"

    # Never service a DRAFT — the author marked it not-ready. This guards the
    # case where a PR was armed while open and later converted to draft
    # (arm/reconcile already refuse to arm a draft in the first place; this is
    # defense-in-depth on the read path, PR #1949 review).
    if pr.is_draft:
        return 0, "PR is a draft; nothing pending"

    # Check for blockers — no state written, purely read
    blockers = maintenance.blockers_for_pr(pr)

    # `blockers_for_pr` derives review_comments from get_unaddressed_comments
    # (comments since the latest commit), which misses an OLD non-outdated
    # unresolved review thread on a PR with green CI and no new comments — that PR
    # would read as CLEAN despite needing work (PR #16 review, P2). Consult review
    # threads directly when nothing else flags it, so the ready/clean path honors
    # AC #4. Gated on `not blockers` so it adds at most one gh call on a
    # would-be-clean tick. Outdated threads are already filtered out of
    # review_comments at the source (get_unaddressed_comments), so an outdated-only
    # thread leaves `blockers` empty here and `_unresolved_review_threads` (which
    # also drops outdated) returns nothing — the PR stays clean (PR #16 review
    # round 2, P2).
    if not blockers:
        unresolved_threads = _unresolved_review_threads(pr.number, cwd)
        if unresolved_threads:
            # Hydrate the threads into review_comments so build_maintenance_prompt
            # renders the file/line/body the agent needs — otherwise the stop gate
            # blocks with an empty Review Comments section (PR #16 review round 2,
            # P2).
            pr.review_comments = _review_comments_from_threads(unresolved_threads)
            blockers = ["review_comments"]

    if not blockers:
        # Clean check: refresh the alive heartbeat (hold ownership) and clear any
        # stale fix lease. But if THIS marker is ours yet a live independent owner
        # is present, it's a stolen marker — refreshing its heartbeat would force
        # the real owner through `_live_foreign_owner` to defer to us until the
        # TTL, so review/CI that arrives just after this tick isn't healed
        # promptly (PR #7 review, P2). Skip the refresh so the stolen marker goes
        # stale. The scan runs only when the marker is ours (the sole case
        # `_touch_owner_heartbeat` would write), so a clean tick on someone
        # else's worktree stays cheap.
        if _marker_session_id(cwd) == self_session_id and _live_independent_owner_paths(
            [cwd], self_session_id
        ):
            return 0, "stale stolen marker; deferring to live independent owner"
        _touch_owner_heartbeat(cwd, self_session_id, False)
        return 0, "nothing pending"

    # Work exists — but defer to a live INDEPENDENT owner BEFORE writing any
    # heartbeat/lease (BOU-1540). A live independent session may own this
    # worktree without a fresh marker (arming is opt-in, and the marker/registry
    # id namespaces are disjoint), so `_live_foreign_owner` above (marker-only)
    # misses it. This also HEALS an already-stolen marker (our id wrongly on a
    # sibling) and makes a mistaken loop fallback to a foreign cwd safe — both
    # defer here. Crucially this runs BEFORE the heartbeat/lease write: stamping
    # a fresh heartbeat on a stolen marker and THEN deferring would make the real
    # owner / detached loop defer to the session that just declined to work,
    # pinning the PR every tick (PR #7 review, P2). The ps/lsof scan runs only on
    # this work-found path; the owner set excludes us, so our own worktree falls
    # through and IS serviced.
    if _live_independent_owner_paths([cwd], self_session_id):
        return 0, "deferring to live independent owner of this worktree"

    owner_session_id = self_session_id or f"pid:{_resolve_owner_pid()}"

    coordinator_claim_id: str | None = None
    coordinator_fingerprint: str | None = None
    if claim:
        coord_decision = coordinator.dispatch_decision_for_pr(pr)
        if not coord_decision.should_dispatch:
            return 0, f"deferring to agent-coordinator {coord_decision.state}: {coord_decision.reason}"

        claimed = coordinator.claim_pr(
            pr,
            session_id=owner_session_id,
            pid=_resolve_owner_pid(),
            agent="agentic-pr-dash-check",
            lease_seconds=_fix_lease_seconds(),
        )
        if claimed is None:
            return 0, "deferring to active agent-coordinator claim"
        coordinator_claim_id = claimed.claim_id
        coordinator_fingerprint = claimed.task.fingerprint

        # We will service: refresh the heartbeat and set the long fix lease (a
        # tick-less fix phase is about to start).
        _touch_owner_heartbeat(cwd, self_session_id, True)

    # Fetch failed CI logs
    logs: dict[str, str] = {}
    if pr.failing_checks:
        logs = github_api.get_failed_logs(pr.latest_commit_sha, pr.failing_checks, cwd)

    prompt = maintenance.build_maintenance_prompt(pr, failed_logs=logs)
    text = f"{prompt}\nPR_NUMBER={pr.number}"
    if coordinator_claim_id is not None:
        text += (
            f"\nCOORDINATOR_CLAIM_ID={coordinator_claim_id}"
            f"\nCOORDINATOR_TASK_FINGERPRINT={coordinator_fingerprint}"
        )
    return 10, text


def _cmd_check(args: argparse.Namespace) -> int:
    code, text = _check_worktree(args.cwd, args.session_id or "")
    # Preserve the historical stdout shape: the prompt is printed with a single
    # trailing newline (print adds it); the non-10 messages are one-liners.
    print(text)
    return code


def _iter_worktrees_with_branch(cwd: str):
    """Yield (path, branch) for non-bare, non-locked worktrees from `git worktree list`.

    Stdlib only — no heavy imports — so the subcommand runs deps-free. Each
    porcelain block is delimited by a blank line; ``bare`` and ``locked`` lines
    mark entries we must skip (the bare repo and agent-locked worktrees are not
    candidates for session ownership). ``branch`` is the short name (``refs/heads/``
    stripped), or "" for a detached HEAD.
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if result.returncode != 0:
        return

    path: str | None = None
    branch = ""
    bare = False
    locked = False
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
            branch = ""
            bare = False
            locked = False
        elif line.startswith("branch "):
            ref = line[len("branch ") :]
            branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
        elif line == "bare":
            bare = True
        elif line == "locked" or line.startswith("locked "):
            locked = True
        elif line == "":
            if path and not bare and not locked:
                yield path, branch
            path = None
            branch = ""
            bare = False
            locked = False
    if path and not bare and not locked:
        yield path, branch


def _iter_worktree_paths(cwd: str):
    """Yield non-bare, non-locked worktree paths (branch-agnostic wrapper)."""
    for path, _branch in _iter_worktrees_with_branch(cwd):
        yield path


def _marker_session_id(worktree_path: str) -> str | None:
    """Return the ``session_id=`` value from a worktree's pr-watch marker.

    Returns None when the marker is absent/unreadable or carries no session_id
    line (legacy markers written before session stamping count as unowned).
    """
    marker = _marker_path(worktree_path)
    try:
        with open(marker, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith("session_id="):
                    return line[len("session_id=") :]
    except OSError:
        return None
    return None


def _cmd_list_owned(args: argparse.Namespace) -> int:
    """Print worktree paths this session owns — markered OR reconciled-and-adopted.

    The single shared, unit-testable home for "which worktrees does THIS session
    own". Both the standalone codex loop and the in-session Claude skill consume
    it to scope discovery, so one session's loop never services another's PRs.

    Two sources (BOU-1442):
      1. worktrees already carrying our `session_id=` marker (the original
         behavior — PRs the arming hook stamped for us).
      2. RECONCILIATION: sibling worktrees under the same worktrees-root whose
         branch has an open NON-DRAFT @me PR but are UNOWNED (no live foreign
         owner). These are adopted — we stamp the marker with our session id/pid,
         then print them. This is the auto/crash-recovery path that catches PRs
         opened by dispatched sub-agents, whose `gh pr create` never fired the
         parent session's PostToolUse arming hook so no marker was ever written.

    Adoption only ever targets UNOWNED worktrees (the same condition `check` uses
    to decide it may service one), so the pid/heartbeat ownership gate still
    yields exactly one executor per PR — no cross-session double-servicing.
    """
    # Surface a REAL discovery failure (cwd is missing / not a git worktree) as a
    # non-zero exit. Otherwise `_iter_worktrees_with_branch` swallows the git
    # error and yields nothing, so an empty result here is indistinguishable from
    # "owns nothing" — and the loop, which now treats rc-0-empty as authoritative,
    # would service nothing for that repo instead of falling back to its root
    # (PR #7 review, P2).
    try:
        probe = subprocess.run(
            ["git", "-C", args.cwd, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        # A hung/unresponsive git invocation is itself a discovery failure — match
        # the 10s bound `_iter_worktrees_with_branch` uses and signal failure
        # rather than stalling the loop forever (PR #7 review, P2).
        print(f"list-owned: worktree probe failed/timed out: {args.cwd}", file=sys.stderr)
        return 3
    if probe.returncode != 0:
        print(f"list-owned: not a git worktree: {args.cwd}", file=sys.stderr)
        return 3

    paths = _collect_owned_worktrees(args.session_id, args.cwd, args.pid)
    for path in paths:
        print(path)
    return 0


def _self_pid_chain(max_depth: int = 16) -> set[int]:
    """PIDs of this process and its ancestors (toward init).

    Used to recognize OUR OWN session among discovered worktree owners,
    independent of session-id namespace or process `comm`: the in-session
    stop-gate / loop runs `check` as a descendant of the very Claude/Codex
    session that owns the worktree, so that session's pid is in this chain.
    Comparing against a single resolved owner pid is unreliable — the wrapper
    (`node …/claude`) and `comm`-walk pid sources disagree (PR #7 review, P2).
    """
    pids: set[int] = set()
    pid = os.getpid()
    for _ in range(max_depth):
        if pid <= 1 or pid in pids:
            break
        pids.add(pid)
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True, text=True, timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            break
        parent = out.stdout.strip()
        if not parent.isdigit():
            break
        pid = int(parent)
    return pids


def _live_independent_owner_paths(paths, self_session_id: str) -> set[str]:
    """Subset of ``paths`` where a LIVE INDEPENDENT session is present.

    This is the liveness signal the marker gates MISS: an independent session
    that is alive on the worktree but whose pr-watch marker is absent, stale, or
    not looping. The pr-watch MARKER is deliberately NOT consulted here:

      * an armed-and-looping marker is already handled by `_live_foreign_owner`
        (called first in `_check_worktree`) and `_marker_live_foreign_pid` (in
        the reconciliation branch); and
      * a live-pid-but-stale/idle marker is intentionally TAKEOVER-ABLE per the
        existing contract (`_live_foreign_owner` / test_stale_heartbeat_no_lease
        _takes_over) — treating it as owned here would let an armed-but-not-
        looping editor session pin its blockers forever (PR #7 review, P2).

    Two signals, correlated by ``worktree_path``:

      1. a registry session for the path, non-terminal + pid-alive (NOT CPU-gated,
         so an idle owner still counts) whose pid/session isn't ours;
      2. an interactive feature-pipeline process whose cwd is the path, alive but
         possibly idle (`min_cpu=0.0`) — catches a session that never registered.

    Config (registry path + CLI ``discovery_names``) is resolved from EACH
    CANDIDATE worktree's own ``agentic-pr-dash.toml``, not the caller's cwd, so a
    sibling worktree that points its registry elsewhere or recognizes a custom
    CLI is still checked correctly (PR #7 review, P2). To keep this O(registry)
    rather than O(worktrees × registry), registry summaries are cached per
    distinct registry path and indexed by worktree_path. Our own session is
    excluded via the ancestor-pid chain AND an exact session-id match, so a
    genuinely-orphaned worktree (sub-agent PR / crashed session) is never treated
    as owned — preserving BOU-1442 pickup and crash recovery.
    """
    from . import agents, session_registry  # noqa: PLC0415

    candidates = list(dict.fromkeys(os.path.abspath(p) for p in paths if p))
    if not candidates:
        return set()

    self_pids = _self_pid_chain()
    # Per-candidate config (load_config is lru-cached): registry path + allow-list.
    reg_of = {c: session_registry.registry_path(c) for c in candidates}  # Path objects
    clis_of = {c: set(load_config(c).discovery_names) for c in candidates}
    owned: set[str] = set()

    # 1) registry — summarize each DISTINCT registry path once (dedupe by string,
    #    pass the Path), index live candidate-eligible states by worktree_path
    #    (path-independent filters only), then apply the per-candidate CLI
    #    allow-list at lookup.
    distinct_regs = {str(reg_of[c]): reg_of[c] for c in candidates}
    index_by_reg: dict[str, dict[str, list]] = {}
    for reg_str, reg_path in distinct_regs.items():
        summary = session_registry.summarize_sessions(path=reg_path)
        idx: dict[str, list] = {}
        for state in summary.sessions.values():
            if state.is_terminal:
                continue
            if state.launch_source in session_registry.DASHBOARD_LAUNCH_SOURCES:
                continue
            if not state.is_feature_pipeline:
                continue
            if self_session_id and state.session_id == self_session_id:
                continue
            if state.pid in self_pids:
                continue
            if not session_registry.pid_is_live(state.pid):
                continue
            if state.worktree_path:
                idx.setdefault(os.path.abspath(state.worktree_path), []).append(state)
        index_by_reg[reg_str] = idx
    for c in candidates:
        states = index_by_reg[str(reg_of[c])].get(c, [])
        if any(s.cli in clis_of[c] for s in states):
            owned.add(c)

    # 2) a live interactive feature-pipeline PROCESS sitting on the worktree —
    #    catches a session that never registered. Liveness, not activity
    #    (min_cpu=0.0). ONE batched ps/lsof scan with the UNION of candidate
    #    allow-lists, then filter the returned agents by EACH candidate's own
    #    allow-list (AgentProcess carries cli_name) so a custom-CLI owner isn't
    #    missed and an excluded one isn't counted (PR #7 review, P2).
    remaining = [p for p in candidates if p not in owned]
    if remaining:
        union_clis: set[str] = set()
        for c in remaining:
            union_clis |= clis_of[c]
        by_path = agents.discover_primary_feature_pipeline_agents(
            remaining, min_cpu=0.0, discovery_names=union_clis
        )
        for path, agent_list in by_path.items():
            abs_path = os.path.abspath(path)
            allow = clis_of.get(abs_path, union_clis)
            if any(a.pid not in self_pids and a.cli_name in allow for a in agent_list):
                owned.add(abs_path)

    return owned


def _collect_owned_worktrees(
    session_id: str, cwd: str, pid: int | None
) -> list[str]:
    """Return the worktree paths this session owns — markered OR adopted.

    Extracted from ``_cmd_list_owned`` so the ``stop-gate`` Stop hook can scope
    its block to the same set the CLI prints. Behavior is unchanged: emit order
    is `git worktree list` order, with reconciliation adopting unowned sibling
    PR worktrees (BOU-1442). Returns [] when no session_id is given.
    """
    cwd = os.path.abspath(cwd)
    if not session_id:
        return []
    eff_pid = pid if pid is not None else _resolve_owner_pid()

    # One repo-wide gh call; {} when gh is unavailable → reconciliation is simply
    # skipped (the markered pass below still works deps-free).
    pr_map = _list_my_open_prs(cwd)

    result: list[str] = []
    seen: set[str] = set()

    def _emit(path: str) -> None:
        if path not in seen:
            seen.add(path)
            result.append(path)

    # `git worktree list` is already scoped to THIS repo's worktree pool (wherever
    # they live on disk — a sibling `<repo>-worktrees/` dir for bug-bash, or
    # alongside the launch worktree for feature-pipeline), so it is the correct
    # candidate set. We deliberately do NOT additionally filter by the caller's
    # parent directory: bug-bash runs this from the MAIN repo while its sub-agent
    # worktrees live under `<repo>-worktrees/`, so a dirname filter would silently
    # skip exactly the PRs we must adopt. Cross-session safety comes from the
    # live-independent-owner gate below, not from paths.
    candidates = list(_iter_worktrees_with_branch(cwd))

    # Compute the live-independent-owner set ONCE for every candidate path (not
    # per-path) so a large worktree pool doesn't multiply the ps/lsof scan
    # (BOU-1540, PR #7 review, P2).
    independent = _live_independent_owner_paths(
        [path for path, _branch in candidates], session_id
    )

    for worktree_path, branch in candidates:
        abs_path = os.path.abspath(worktree_path)
        # A live INDEPENDENT session owns this worktree (registry/process/marker,
        # path-correlated). Never list it — even when our own marker is on it:
        # that marker is a prior bad adoption (the observed stuck state), and
        # emitting it here would keep us servicing a sibling ticket's PR
        # (BOU-1540, PR #7 review, P1). The owner set already excludes us.
        if abs_path in independent:
            continue
        # 1) already ours (and not contested above)
        if _marker_session_id(worktree_path) == session_id:
            _emit(worktree_path)
            continue
        # 2) reconciliation — adopt an unowned PR worktree
        if not pr_map:
            continue
        pr = pr_map.get(branch)
        if pr is None:
            continue
        number, is_draft = pr
        if is_draft:
            continue
        # UNOWNED = no marker with a LIVE foreign pid. Use the strict pid check
        # (not _live_foreign_owner, which also wants a fresh heartbeat) so we do
        # NOT steal a session that JUST armed its PR but hasn't ticked yet
        # (PR #1949 review, P1). A dead pid (crashed session) or no marker at all
        # (sub-agent PR) is still adopted.
        if _marker_live_foreign_pid(worktree_path, session_id):
            continue
        if _write_arm_marker(worktree_path, session_id, int(eff_pid), int(number)):
            _emit(worktree_path)
    return result


def _collect_stop_gate_worktrees(session_id: str, cwd: str) -> list[str]:
    """Return marker-owned worktrees for passive Stop-hook gating.

    Unlike `_collect_owned_worktrees`, this does NOT reconcile/adopt unmarked
    open `@me` PR worktrees. Adoption is an explicit orchestration/recovery
    action for `list-owned`; a Stop hook runs when a session is trying to go
    idle and must only block on PRs already armed by that same session.
    """
    cwd = os.path.abspath(cwd)
    if not session_id:
        return []

    candidates = list(_iter_worktrees_with_branch(cwd))
    independent = _live_independent_owner_paths(
        [path for path, _branch in candidates], session_id
    )

    result: list[str] = []
    seen: set[str] = set()
    for worktree_path, _branch in candidates:
        abs_path = os.path.abspath(worktree_path)
        if abs_path in independent:
            continue
        if _marker_session_id(worktree_path) != session_id:
            continue
        if worktree_path not in seen:
            seen.add(worktree_path)
            result.append(worktree_path)
    return result


def _mark_maintenance_complete(maintenance, cwd: str, pr_number: int) -> None:  # type: ignore[no-untyped-def]
    """Best-effort: write COMPLETE to the on-disk maintenance state.

    Allows the orchestrator's already_queued guard to see a non-QUEUED state
    on the next poll and re-dispatch PIPELINE_HANDOFF.md when blockers remain
    (BOU-1408).  Swallows all errors — completion marking is advisory; missing
    it delays (but does not prevent) re-dispatch via blocker-set changes.
    """
    try:
        from .models import MaintenanceStatus  # noqa: PLC0415

        state = maintenance.load_state(cwd, pr_number)
        if state is not None:
            maintenance.mark_state(state, MaintenanceStatus.COMPLETE)
    except Exception:  # noqa: BLE001
        pass


def _commit_subject(message: str) -> str:
    """First non-empty line of a commit message, trimmed."""
    for line in message.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return message.strip()


def _completion_reply_body(
    marker: str,
    path: str | None,
    commits_by_file: dict[str, list[tuple[str, str]]],
    all_commits: list[tuple[str, str]],
) -> str:
    """Build a substantive completion reply that cites the fixing commit(s).

    Instead of a generic "addressed", name the commit(s) that touched the
    thread's file (falling back to all of this round's commits for non-inline
    threads) with their subject lines, so a reviewer can see WHAT addressed the
    comment, not merely that the thread was closed (ownership-coordination).
    """
    cites = commits_by_file.get(path, []) if path else []
    if not cites:
        cites = all_commits
    seen: set[str] = set()
    unique = [(sha, msg) for sha, msg in cites if not (sha in seen or seen.add(sha))]
    lines = [marker]
    if len(unique) == 1:
        sha, msg = unique[0]
        lines.append(f"Addressed by the local maintenance loop in {sha[:9]} — {_commit_subject(msg)}.")
    elif unique:
        lines.append("Addressed by the local maintenance loop in:")
        lines.extend(f"- `{sha[:9]}` {_commit_subject(msg)}" for sha, msg in unique[:6])
    else:
        lines.append("Addressed by the local maintenance loop.")
    return "\n".join(lines)


def _cmd_complete(args: argparse.Namespace) -> int:
    from . import github_api, maintenance  # noqa: PLC0415
    from .github_api import COMPLETE_MARKER  # noqa: PLC0415
    from .models import ReviewComment  # noqa: PLC0415

    cwd = os.path.abspath(args.cwd)
    pr_number_arg = args.pr

    # Resolve PR
    if pr_number_arg is not None:
        pr = _resolve_pr_by_number(int(pr_number_arg), cwd)
    else:
        pr = _resolve_pr_for_branch(cwd)

    if pr is _GH_UNAVAILABLE:
        print("could not list PRs (gh unavailable)")
        return 2
    if pr is None:
        print("no open PR for this branch")
        return 0

    resolved_pr_number = pr.number
    head_sha = pr.latest_commit_sha
    head_date = pr.latest_commit_date
    # The API's view of the head, captured BEFORE the local override below, so
    # the local-range helper can detect a stale origin/<branch> ref (one that
    # doesn't yet contain a commit the API already knows) and fall back (BOU-1479).
    api_head_sha = pr.latest_commit_sha

    # Prefer the LOCAL PR-branch head (origin/<branch>, updated on push) over the
    # GitHub API's lagging view, so resolving works the instant after a push
    # instead of waiting for the API to index the new head (BOU-1479). The
    # per-thread `head_date > created_at` guard below otherwise stays false in
    # that race even when the fix is already pushed.
    local_head_sha, local_head_date = github_api.get_local_pr_head(pr.branch, cwd)
    # Adopt the local head only when origin/<branch> is FRESH — i.e. it already
    # contains the API's reported head (the just-pushed case BOU-1479 targets). A
    # stale local ref (behind a push made elsewhere / not fetched here) would drag
    # head_date BACKWARDS, leaving threads created after the stale ref but before
    # the true API head open; in that case keep the API head/date.
    local_is_fresh = bool(local_head_sha) and (
        not api_head_sha
        or github_api._is_ancestor(api_head_sha, local_head_sha, cwd)
    )
    if local_is_fresh:
        head_sha = local_head_sha
        if local_head_date:
            head_date = local_head_date

    # Verify a fixing push actually landed before resolving anything. The loop
    # passes --baseline = the PR head SHA captured BEFORE the agent ran, so
    # `new_commits` is exactly what the agent pushed; without it we fall back to
    # all PR commits + the per-thread timestamp guard below. This keeps the
    # worker stateless (no stored ledger) while refusing to resolve threads when
    # the runtime exited 0 without pushing a fix (or pushed an unrelated change).
    baseline = args.baseline or ""
    new_commits = github_api.get_new_pr_commits(
        resolved_pr_number, baseline, head_sha, cwd, pr_branch=pr.branch,
        api_head_sha=api_head_sha)
    touched: set[str] = set()
    # file path -> fixing commits that touched it, so the completion reply can
    # cite the actual commit(s) that addressed each thread (not just a generic
    # "addressed"), making it clear to the reviewer WHAT changed.
    commits_by_file: dict[str, list[tuple[str, str]]] = {}
    for sha, msg in new_commits:
        try:
            files = github_api.get_commit_changed_files(sha, cwd)
        except Exception:  # noqa: BLE001
            files = []
        for changed in files:
            touched.add(changed)
            commits_by_file.setdefault(changed, []).append((sha, msg))

    # Re-fetch unresolved review threads statelessly from GitHub.
    threads = github_api.get_review_threads(resolved_pr_number, cwd)
    for thread in threads:
        if thread.is_resolved:
            continue
        path = thread.top.path
        # Addressed only if a commit landed after this comment AND (for inline
        # threads) touched the commented file. Otherwise leave it open — a
        # green runtime exit is not proof the feedback was handled.
        addressed = (
            bool(new_commits)
            and head_date > thread.top.created_at
            and (path is None or path in touched)
        )
        if not addressed:
            continue
        # Resolve FIRST; only post the completion marker if resolution actually
        # succeeded. resolve_review_thread returns False on a rejected/timed-out
        # mutation — posting COMPLETE_MARKER then would make later `check` calls
        # skip a thread GitHub still shows open, stranding it.
        try:
            if not github_api.resolve_review_thread(thread.node_id, cwd):
                print(
                    f"warning: could not resolve thread {thread.node_id}; leaving open for retry",
                    file=sys.stderr,
                )
                continue
            stub = ReviewComment(
                id=thread.top.database_id,
                author=thread.top.author,
                body=thread.top.body,
                path=path,
                line=thread.top.line,
                created_at=thread.top.created_at,
                is_inline=True,
                thread_id=thread.node_id,
            )
            body = _completion_reply_body(COMPLETE_MARKER, path, commits_by_file, new_commits)
            github_api.reply_to_review_comment(resolved_pr_number, stub, body, cwd)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: error completing thread {thread.node_id}: {exc}", file=sys.stderr)

    # Re-check live blockers AFTER resolving threads. Close the maintenance bead
    # only when nothing remains — closing it while CI is still failing (or other
    # comments are unaddressed) would orphan the remaining work with no open bead
    # for `bd ready` to surface.
    fresh = _resolve_pr_by_number(resolved_pr_number, cwd)
    if fresh is _GH_UNAVAILABLE or fresh is None:
        remaining = ["unknown"]  # can't confirm cleared → don't close
    else:
        remaining = maintenance.blockers_for_pr(fresh)

    # Mark the maintenance state COMPLETE so the orchestrator's already_queued
    # guard sees a non-QUEUED state on the next poll and re-dispatches when
    # blockers remain.  Without this the state stays QUEUED indefinitely and the
    # orchestrator never refreshes PIPELINE_HANDOFF.md (BOU-1408).
    _mark_maintenance_complete(maintenance, cwd, resolved_pr_number)

    if remaining:
        print(f"completed (bead left open; blockers remain: {', '.join(remaining)})")
        return 0

    # Best-effort close the open maintenance bead
    branch = pr.branch
    if branch:
        try:
            from .config import load as _load_config  # noqa: PLC0415
            from .tracker import get_tracker  # noqa: PLC0415
            tracker = get_tracker(_load_config(cwd))
            task_id = tracker.find_task(pr=resolved_pr_number, branch=branch, cwd=cwd)
            if task_id:
                tracker.close_task(task_id, cwd=cwd)
        except Exception:  # noqa: BLE001
            pass

    print("completed (bead closed; no blockers remain)")
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# stop-gate — block-to-address Stop hook core
# ---------------------------------------------------------------------------
#
# Replaces the old model-armed `/loop 3m /pr-maintenance-check` nudge. A Stop
# hook runs this; on pending review/CI work it prints the maintenance prompt to
# stderr and returns 2 (the harness surfaces that as a non-ignorable "keep
# going"), so the SAME session addresses its PR feedback before going idle —
# deterministic, session-scoped, no cron, no directive the model can ignore.
# Two guards keep it from over-firing (mirroring pre-completion-pr-status.py):
#   * a TIME rate-limit (GAIA_PR_WATCH_STOP_INTERVAL, default 180s) so the
#     expensive gh check runs at most once per window, not every turn; and
#   * a LOOP-BREAK counter (GAIA_PR_WATCH_STOP_LOOP_THRESHOLD, default 3) that
#     releases the gate (exit 0) after N identical pending states, so an item
#     the agent cannot resolve does not trap the session — it can then ask the
#     user. State lives in <cwd>/<state-dir>/pr-watch.stop-loop.json.


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get("PR_AGENT_OPS_" + name) or os.environ.get("GAIA_PR_WATCH_" + name) or ""
    try:
        return int(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _stop_state_path(cwd: str) -> str:
    return str(load_config(cwd).state_dir_for(cwd) / "pr-watch.stop-loop.json")


def _load_stop_state(cwd: str) -> dict:
    try:
        with open(_stop_state_path(cwd), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_stop_state(cwd: str, state: dict) -> None:
    try:
        path = _stop_state_path(cwd)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError:
        pass


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


def _stop_fingerprint(pending: list[tuple[str, str]]) -> str:
    """Stable hash of the pending (worktree, prompt) set — identical pending
    state across stops yields the same fingerprint so the loop-break counter can
    detect "no progress"."""
    h = hashlib.sha256()
    for path, text in sorted(pending):
        h.update(path.encode("utf-8"))
        h.update(b"\0")
        h.update(text.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _extract_pr_number(text: str) -> str:
    """Pull the trailing PR_NUMBER=<n> the check appends, or '' if absent."""
    for line in reversed(text.splitlines()):
        if line.startswith("PR_NUMBER="):
            return line[len("PR_NUMBER=") :].strip()
    return ""


def _build_stop_block(pending: list[tuple[str, str]]) -> str:
    lines = [
        "[pr-watch] Open PR(s) you own have pending review/CI work. Address it "
        "before stopping — commit and push to the EXISTING branch (do not open a "
        "new PR), then re-stop:\n"
    ]
    for path, text in pending:
        lines.append(f"───── worktree: {path} ─────")
        lines.append(text)
        # Detached PRs (worktree torn down) have no real cwd — emitting a
        # `gh ... --cwd "(no worktree) PR #N"` / `complete --cwd <fake>` command
        # would be non-executable. The detached text already carries recreate /
        # hand-off guidance, so skip the per-worktree completion block (PR #16
        # review, P2).
        if path.startswith("(no worktree)"):
            lines.append("")
            continue
        # Per-worktree completion command with the EXPLICIT --cwd so a sibling PR
        # resolved from the launch cwd doesn't mark maintenance state / close the
        # bead against the wrong worktree path (codex P2).
        pr_ref = _extract_pr_number(text) or "<N>"
        lines.append(
            f"FIRST, before changing anything, capture this worktree's pre-fix "
            f"baseline head: `gh pr view --json headRefOid -q .headRefOid` (run "
            f"from {path}). THEN fix + commit + push, and finally:\n"
            f"  agentic-pr-dash complete "
            f"--pr {pr_ref} --baseline <that-pre-fix-sha> --cwd {path}\n"
            f"(Passing the post-fix head as --baseline leaves threads unresolved.)"
        )
        lines.append("")
    return "\n".join(lines)


def _owned_open_pr_numbers(owned: list[str]) -> set[int]:
    """Collect PR numbers from the armed markers of owned worktrees.

    Used by the stop-gate waiter branch to determine whether the session has
    any open PRs that need a waiter. Reads markers only — no gh call needed
    because stale markers are pruned by Task A3 and the stop-gate collects
    from markers it already trusts.
    """
    numbers: set[int] = set()
    for wt in owned:
        marker = _read_marker(wt) or {}
        pr_raw = marker.get("pr", "")
        if str(pr_raw).isdigit():
            numbers.add(int(pr_raw))
    return numbers


def _build_waiter_block(open_prs: set[int], cwd: str, session_id: str) -> str:
    """Render the spawn-waiter prompt shown by the stop-gate when no pending work
    but the session owns at least one open non-draft PR and no live waiter exists.
    """
    pr_list = ", ".join(f"#{n}" for n in sorted(open_prs))
    cmd = load_config(cwd).await_command.format(cwd=cwd, session_id=session_id)
    return (
        f"[pr-watch] You own open PR(s) {pr_list} with no pending feedback right now. "
        f"Start the feedback waiter IN THE BACKGROUND (run_in_background / detached) so "
        f"arriving review comments wake this session, then stop again:\n"
        f"  {cmd}"
    )


def _cmd_stop_gate(args: argparse.Namespace) -> int:
    # Fail SAFE: this runs on every Stop. A transient git/gh error must never
    # block the stop (exit 2) or spew a traceback each turn — degrade to exit 0.
    try:
        return _stop_gate_impl(args)
    except Exception:  # noqa: BLE001
        return 0


def _stop_gate_impl(args: argparse.Namespace) -> int:
    cwd = os.path.abspath(args.cwd)

    # --- time rate-limit: skip the expensive gh check if we ran recently -------
    # ONLY when the previous run was CLEAN. If the last run found blockers
    # (state carries a fingerprint), a within-window stop must NOT release —
    # _check_worktree already refreshed this session's fix lease, so going idle
    # would leave the detached loop deferring to us while the work sits unhandled
    # (codex P1). Re-check instead (the loop-break counter still bounds blocking).
    interval = _env_int("STOP_INTERVAL", 180)
    state = _load_stop_state(cwd)
    now = time.time()
    last_pending = bool(state.get("fingerprint"))
    if interval > 0 and not last_pending and (now - float(state.get("ts", 0) or 0)) < interval:
        return 0
    # Record this run's timestamp up-front; preserve any loop counter.
    _save_stop_state(cwd, {**state, "ts": now})

    # --- scope to owned worktrees ----------------------------------------------
    # When this session has an identity, service ONLY what it owns. Do NOT fall
    # back to [cwd]: an empty result can mean the cwd is owned by another LIVE
    # session (collection skipped it on the foreign-pid check), and servicing it
    # would let two sessions fix the same PR (codex P2). Fall back to cwd only
    # when ownership is genuinely unknown (no session identity at all).
    session_id = args.session_id or _read_session_marker(cwd)
    if session_id:
        owned = _collect_stop_gate_worktrees(session_id, cwd)
    else:
        owned = [cwd]

    pending: list[tuple[str, str]] = []
    for worktree in owned:
        # Passive probe: the stop-gate only prints a prompt back to the
        # interactive session and never releases a claim, so it must NOT create
        # an agent-coordinator claim (codex P1) — that claim would outlive the
        # idle session and suppress this same work on the next Stop attempt.
        code, text = _check_worktree(worktree, session_id, claim=False)
        if code == 10:
            pending.append((worktree, text))

    # BOU-1587/1612: owned PRs whose worktree was torn down are STILL mandatory
    # work. They have no worktree to _check_worktree, so reconcile them from the
    # durable ledger and block on any detached blocker with live-path parity.
    if session_id:
        detached = [r for r in _detached_pr_records(session_id, cwd)
                    if _record_has_blockers(r)]
        detached.sort(key=lambda r: (0 if r["p1"] else 1, -r["unresolved_threads"], r["pr"]))
        for r in detached:
            pending.append(_detached_pending_entry(r))

    if not pending:
        # Waiter enforcement (BOU-1632): when no pending work but this session
        # owns open PRs and no live waiter exists, prompt to spawn one. The
        # loop-break counter reuses the same "need-waiter:<prs>" fingerprint
        # so 3 identical no-progress states release the gate.
        if (not getattr(args, "no_waiter", False)) and session_id:
            open_prs = _owned_open_pr_numbers(owned)
            if open_prs and not _await_alive(cwd, session_id):
                fingerprint = "need-waiter:" + ",".join(str(n) for n in sorted(open_prs))
                count = int(state.get("count", 0)) + 1 if state.get("fingerprint") == fingerprint else 1
                _save_stop_state(cwd, {"ts": now, "fingerprint": fingerprint, "count": count})
                threshold = _env_int("STOP_LOOP_THRESHOLD", 3)
                if count < threshold:
                    print(_build_waiter_block(open_prs, cwd, session_id), file=sys.stderr)
                    return 2
                _save_stop_state(cwd, {"ts": now})  # release after threshold
                return 0
        # Clear the loop counter but KEEP the timestamp so a clean idle session
        # does not re-run gh on every single turn.
        _save_stop_state(cwd, {"ts": now})
        return 0

    fingerprint = _stop_fingerprint(pending)
    count = int(state.get("count", 0)) + 1 if state.get("fingerprint") == fingerprint else 1
    _save_stop_state(cwd, {"ts": now, "fingerprint": fingerprint, "count": count})

    print(_build_stop_block(pending), file=sys.stderr)

    threshold = _env_int("STOP_LOOP_THRESHOLD", 3)
    if count >= threshold:
        print(
            f"[pr-watch] Same pending PR state seen {count}× with no progress — "
            f"releasing the stop gate so you can ask the user or take a safe "
            f"action. A later stop will re-enforce it.",
            file=sys.stderr,
        )
        _save_stop_state(cwd, {"ts": now})  # reset counter, keep the rate-limit ts
        return 0

    print(
        "[pr-watch] Address the items above (commit + push to each EXISTING "
        "branch), run the per-worktree `complete` command shown in that section, "
        "then try stopping again. If you cannot resolve an item yourself, tell "
        "the user.",
        file=sys.stderr,
    )
    return 2


def _iter_worktree_paths(cwd: str):
    """Yield abspaths of every worktree in this repo's pool (porcelain)."""
    try:
        out = subprocess.run(["git", "-C", cwd, "worktree", "list", "--porcelain"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return
    if out.returncode != 0:
        return
    for line in out.stdout.splitlines():
        if line.startswith("worktree "):
            yield os.path.abspath(line[len("worktree "):].strip())


def _pr_open_state(pr_number: int, cwd: str):
    """(state, url, has_failing_ci, failing_checks, review_decision, merge_state, mergeable) for a PR.

    state is 'open' | 'merged' | 'closed' | 'unknown'. Detached-PR live state for
    reconcile-prs / stop-gate (BOU-1587). gh-unavailable returns empty blocker state.
    """
    from . import github_api  # noqa: PLC0415
    import json as _json  # noqa: PLC0415

    unavailable = ("unknown", "", False, [], "", "", "")
    try:
        res = subprocess.run(
            [
                "gh", "pr", "view", str(pr_number),
                "--json", "state,url,isDraft,reviewDecision,mergeStateStatus,mergeable",
            ],
            cwd=cwd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return unavailable
    if res.returncode != 0:
        return unavailable
    try:
        d = _json.loads(res.stdout or "{}")
    except ValueError:
        return unavailable
    state = str(d.get("state", "unknown")).lower()  # OPEN/MERGED/CLOSED
    # A PR armed while open but later converted to draft is not-ready: surface it
    # as 'draft' so the detached path skips it, mirroring the live-worktree path's
    # draft guard (PR #16 review, P2).
    if state == "open" and bool(d.get("isDraft", False)):
        state = "draft"
    url = str(d.get("url", ""))
    checks = github_api.get_ci_checks(pr_number, cwd)
    failing = [c.name for c in checks
               if c.conclusion == "failure" and not github_api._is_infra_check(c.name)]
    review_decision = str(d.get("reviewDecision") or "")
    merge_state = str(d.get("mergeStateStatus") or "")
    mergeable = str(d.get("mergeable") or "")
    return (state, url, bool(failing), failing, review_decision, merge_state, mergeable)


def _unpack_pr_open_state(raw):
    """Normalize legacy tuples from tests/callers to the current 7-field shape."""
    if len(raw) == 4:
        state, url, has_fail, failing = raw
        return state, url, has_fail, failing, "", "", ""
    if len(raw) == 6:
        state, url, has_fail, failing, review_decision, merge_state = raw
        return state, url, has_fail, failing, review_decision, merge_state, ""
    state, url, has_fail, failing, review_decision, merge_state, mergeable = raw
    return state, url, has_fail, failing, review_decision, merge_state, mergeable


def _record_has_blockers(record: dict) -> bool:
    return bool(
        record["unresolved_threads"]
        or record["ci_failing"]
        or record.get("changes_requested")
        or record.get("merge_conflict")
    )


def _thread_is_p1(thread) -> bool:
    bodies = [thread.top.body] + [r.body for r in getattr(thread, "replies", [])]
    return any("p1" in (b or "").lower() for b in bodies)


def _session_is_live(session_id: str, cwd: str | None = None) -> bool:
    """True if `session_id` has a non-terminal, pid-live registry entry.

    Resolves the session registry from the TARGET `cwd` (not the process cwd) so
    a repo that configures its own `session_registry_path` is read correctly —
    otherwise a still-running owner in that repo can be missed and its PR wrongly
    adopted (PR #16 review, P2).
    """
    from . import session_registry  # noqa: PLC0415
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
    """Win an exclusive claim on an orphan PR. One live claimant wins; a claim
    held by a dead pid is taken over. Returns True on win (BOU-1587 Component G).

    The claim is scoped by ``repo`` so two repos' same-number PRs don't share one
    claim file (PR #16 review round 2, P1). The read-decide-write runs under an
    exclusive lock so two concurrent live claimants can't both observe "no claim"
    and both adopt the same PR (PR #16 review, P1)."""
    from . import session_ledger, session_registry  # noqa: PLC0415
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
                return False  # a live session owns it
        session_ledger.write_claim(pr_number, session_id, pid, repo)
        return True


def _adopt_orphan_prs(session_id: str, cwd: str, pid: int | None):
    """Claim PRs orphaned by DEAD sessions and adopt them into THIS session's ledger.

    Orphan = ledger entry of another session that is no longer live, whose worktree
    is gone and whose PR is still open with unresolved work. Exactly one live session
    wins each PR via an exclusive claim file (BOU-1587 Component G).
    """
    from . import session_ledger, github_api  # noqa: PLC0415

    eff_pid = pid if pid is not None else _resolve_owner_pid()
    present = set(_iter_worktree_paths(cwd))
    # Only consider other sessions' entries that belong to THIS repo: each entry
    # is queried via gh against `cwd`, so a PR from another repo would be resolved
    # against the wrong remote (PR #16 review round 2, P1). Legacy repo-less
    # entries pass the filter and degrade to the prior unscoped behavior.
    target_repo = _repo_slug(cwd)
    adopted = []
    for other in session_ledger.list_session_ids():
        if other == session_id:
            continue
        if _session_is_live(other, cwd):
            continue  # owner still alive — its own loop handles it
        for e in session_ledger.read(other, repo=target_repo):
            abs_wt = os.path.abspath(e.worktree) if e.worktree else ""
            if abs_wt and abs_wt in present and _worktree_is_for_entry(abs_wt, e):
                continue  # still THIS PR's live worktree → not orphaned
            state, url, has_fail, failing, review_decision, merge_state, mergeable = (
                _unpack_pr_open_state(_pr_open_state(e.pr, cwd))
            )
            if state in ("merged", "closed", "unknown", "draft"):
                continue
            threads = github_api.get_review_threads(e.pr, cwd)
            unresolved = [t for t in threads if not t.is_resolved and not t.is_outdated]
            changes_requested = str(review_decision).upper() == "CHANGES_REQUESTED"
            merge_conflict = (
                str(merge_state).upper() == "DIRTY"
                or str(mergeable).upper() == "CONFLICTING"
            )
            if not unresolved and not has_fail and not changes_requested and not merge_conflict:
                continue  # nothing to do
            if not _claim_pr(e.pr, session_id, int(eff_pid), e.repo):
                continue  # another live session won it
            session_ledger.append(session_id, e.pr, e.branch, e.worktree,
                                  e.baseline_sha, repo=e.repo)
            adopted.append({
                "pr": e.pr, "url": url or f"(pr {e.pr})", "branch": e.branch,
                "worktree_present": False, "unresolved_threads": len(unresolved),
                "ci_failing": has_fail, "failing_checks": failing,
                "changes_requested": changes_requested,
                "review_decision": review_decision,
                "merge_conflict": merge_conflict,
                "merge_state": merge_state,
                "mergeable": mergeable,
                "p1": any(_thread_is_p1(t) for t in unresolved), "state": state,
                "adopted_from": other,
            })
    return adopted


def _worktree_is_for_entry(path: str, entry) -> bool:
    """True if the worktree at `path` still belongs to this ledger entry's PR.

    A path-only "is it present?" check is wrong when bug-bash tears down a lane
    and later creates a DIFFERENT worktree at the same directory: the old PR would
    be treated as still-live and silently dropped from monitoring. Confirm the
    worktree's marker PR (or its current branch) matches the entry before
    suppressing the ledger record (PR #16 review, P1).
    """
    marker = _read_marker(path) or {}
    if str(marker.get("pr", "")) == str(entry.pr):
        return True
    if not marker:
        return False
    branch = _current_branch(path)
    return bool(branch) and entry.branch and branch == entry.branch


def _detached_pr_records(session_id: str, cwd: str) -> list[dict]:
    """Records for this session's ledger PRs whose worktree is GONE, with live
    GitHub state. Does NO worktree adoption, so it is safe for the Stop hook
    (which must never adopt unmarked worktrees). Prunes merged/closed PRs.
    """
    from . import session_ledger, github_api  # noqa: PLC0415

    present_worktrees = set(_iter_worktree_paths(cwd))
    independent_worktrees = _live_independent_owner_paths(present_worktrees, session_id)
    # Scope to THIS repo: each ledger PR is queried with gh against `cwd`, so a PR
    # from another repo would be checked against the wrong remote (and pruned when
    # `cwd`'s same-number PR is closed). Legacy repo-less entries still pass
    # (PR #16 review round 2, P1).
    target_repo = _repo_slug(cwd)
    records: list[dict] = []
    prune: set[int] = set()
    for e in session_ledger.read(session_id, repo=target_repo):
        abs_wt = os.path.abspath(e.worktree) if e.worktree else ""
        if (
            abs_wt
            and abs_wt in independent_worktrees
            and not _read_marker(abs_wt)
            and _current_branch(abs_wt) == e.branch
        ):
            continue  # marker-less same-branch worktree is owned by a live session
        if abs_wt and abs_wt in present_worktrees and _worktree_is_for_entry(abs_wt, e):
            continue  # still THIS PR's live worktree; handled by the worktree pass
        state, url, has_fail, failing, review_decision, merge_state, mergeable = (
            _unpack_pr_open_state(_pr_open_state(e.pr, cwd))
        )
        if state in ("merged", "closed"):
            prune.add(e.pr)
            continue
        if state == "draft":
            continue  # not-ready; skip but keep the ledger entry (may reopen)
        threads = github_api.get_review_threads(e.pr, cwd)
        unresolved = [t for t in threads if not t.is_resolved and not t.is_outdated]
        changes_requested = str(review_decision).upper() == "CHANGES_REQUESTED"
        merge_conflict = (
            str(merge_state).upper() == "DIRTY"
            or str(mergeable).upper() == "CONFLICTING"
        )
        records.append({
            "pr": e.pr, "url": url or f"(pr {e.pr})", "branch": e.branch,
            "worktree_present": False, "unresolved_threads": len(unresolved),
            "ci_failing": has_fail, "failing_checks": failing,
            "changes_requested": changes_requested,
            "review_decision": review_decision,
            "merge_conflict": merge_conflict,
            "merge_state": merge_state,
            "mergeable": mergeable,
            "p1": any(_thread_is_p1(t) for t in unresolved), "state": state,
        })
    if prune:
        session_ledger.prune(session_id, prune, repo=target_repo)
    return records


def _owned_pr_records(session_id: str, cwd: str, pid: int | None, adopt_orphans: bool):
    """Union of live-worktree PRs and detached ledger PRs, each with live state.

    Returns a list of dict records sorted severity-first (P1 -> thread count).
    Side effect: prunes merged/closed PRs from this session's ledger.
    """
    from . import github_api  # noqa: PLC0415

    records: dict[int, dict] = {}

    # 1) Detached ledger PRs (worktree gone).
    for rec in _detached_pr_records(session_id, cwd):
        records[rec["pr"]] = rec

    # 2) Live worktree-owned PRs (unchanged ownership semantics).
    for wt in _collect_owned_worktrees(session_id, cwd, pid):
        marker = _read_marker(wt) or {}
        pr_raw = marker.get("pr")
        if not pr_raw or not str(pr_raw).isdigit():
            continue
        pr = int(pr_raw)
        if pr in records:
            records[pr]["worktree_present"] = True
            continue
        state, url, has_fail, failing, review_decision, merge_state, mergeable = (
            _unpack_pr_open_state(_pr_open_state(pr, wt))
        )
        if state in ("merged", "closed"):
            continue
        threads = github_api.get_review_threads(pr, wt)
        unresolved = [t for t in threads if not t.is_resolved and not t.is_outdated]
        changes_requested = str(review_decision).upper() == "CHANGES_REQUESTED"
        merge_conflict = (
            str(merge_state).upper() == "DIRTY"
            or str(mergeable).upper() == "CONFLICTING"
        )
        records[pr] = {
            "pr": pr, "url": url or f"(pr {pr})", "branch": _current_branch(wt),
            "worktree_present": True, "unresolved_threads": len(unresolved),
            "ci_failing": has_fail, "failing_checks": failing,
            "changes_requested": changes_requested,
            "review_decision": review_decision,
            "merge_conflict": merge_conflict,
            "merge_state": merge_state,
            "mergeable": mergeable,
            "p1": any(_thread_is_p1(t) for t in unresolved), "state": state,
        }

    # 3) Orphan recovery (Component G) — claim PRs from dead sessions.
    if adopt_orphans:
        for rec in _adopt_orphan_prs(session_id, cwd, pid):
            records.setdefault(rec["pr"], rec)

    ordered = sorted(records.values(),
                     key=lambda r: (0 if r["p1"] else 1, -r["unresolved_threads"], r["pr"]))
    return ordered


def _cmd_reconcile_prs(args: argparse.Namespace) -> int:
    import json as _json  # noqa: PLC0415
    records = _owned_pr_records(args.session_id, os.path.abspath(args.cwd),
                                args.pid, adopt_orphans=args.adopt_orphans)
    for r in records:
        print(_json.dumps(r))
    return 0


# ---------------------------------------------------------------------------
# await — in-session background feedback waiter (BOU-1632)
# ---------------------------------------------------------------------------
#
# Spawned by the stop-gate when it has no pending work but the session owns at
# least one open non-draft PR. Polls owned worktrees for pending feedback and
# stamps the ownership heartbeat each tick (keeps the detached loop deferring
# while the waiter is alive). On finding work it exits 10 with the same
# stop-block the interactive stop-gate renders — the harness wakes the
# owning session so it can address the feedback in-context.
#
# Single-instance per session: pidfile ``pr-watch.await.pid`` in the state dir.
# A live same-session pidfile → exit 3. Dead pidfile → proceed (stale).
# Pidfile written on entry, removed on every exit path.


def _await_pidfile(cwd: str) -> str:
    return str(load_config(cwd).state_dir_for(cwd) / "pr-watch.await.pid")


def _read_await_pidfile(cwd: str) -> dict:
    """Return the pidfile contents as a dict, or {} on missing/corrupt."""
    try:
        with open(_await_pidfile(cwd), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_await_pidfile(cwd: str, data: dict) -> None:
    """Write the await pidfile atomically (best-effort)."""
    path = _await_pidfile(cwd)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def _remove_await_pidfile(cwd: str) -> None:
    """Remove the await pidfile (best-effort)."""
    try:
        os.remove(_await_pidfile(cwd))
    except OSError:
        pass


def _await_alive(cwd: str, session_id: str) -> bool:
    """True if a live waiter pidfile exists with a matching session_id."""
    data = _read_await_pidfile(cwd)
    if not data:
        return False
    return (
        data.get("session_id") == session_id
        and _pid_alive(str(data.get("pid", "")))
    )


def _detached_pending_entry(r: dict) -> tuple[str, str]:
    """Build the (label, text) pending entry for a detached PR record.

    Extracted from _stop_gate_impl so both stop-gate and await render
    detached-PR entries identically (DRY).
    """
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


def _cmd_await(args: argparse.Namespace) -> int:
    """Background feedback waiter — poll owned PRs, exit 10 when work arrives."""
    cwd = os.path.abspath(args.cwd)
    session_id = args.session_id or _read_session_marker(cwd)
    owner_pid = args.owner_pid if args.owner_pid else _resolve_owner_pid()

    # Single-instance guard: abort if a live waiter for this session already exists.
    existing = _read_await_pidfile(cwd)
    if (
        existing
        and _pid_alive(str(existing.get("pid", "")))
        and existing.get("session_id") == session_id
    ):
        print("[pr-watch] waiter already running for this session", file=sys.stderr)
        return 3

    _write_await_pidfile(cwd, {"pid": os.getpid(), "session_id": session_id})
    # max_wait == 0 means "one tick then exit" — set deadline to now so the
    # expiry check fires immediately after the first tick.
    now = time.time()
    if args.max_wait == 0:
        deadline: float | None = now  # expire immediately after the first tick
    elif args.max_wait > 0:
        deadline = now + args.max_wait
    else:
        deadline = None  # run forever until work found or pid dead
    try:
        while True:
            # Exit if the owning session pid is dead.
            if not _pid_alive(str(owner_pid)):
                return 0

            owned = _collect_stop_gate_worktrees(session_id, cwd) if session_id else [cwd]

            pending: list[tuple[str, str]] = []
            for worktree in owned:
                code, text = _check_worktree(worktree, session_id, claim=False)
                if code == 10:
                    pending.append((worktree, text))
                # Stamp the heartbeat for each owned worktree on every tick,
                # regardless of whether work was found — this is the coordination
                # contract that keeps the detached loop deferring while the waiter
                # is alive. _check_worktree already stamps it when claim=False and
                # the marker is ours, but stamp explicitly here too so that worktrees
                # where _check_worktree exits early (no PR, draft, etc.) are still
                # covered.
                _touch_owner_heartbeat(worktree, session_id, code == 10)

            # Include detached-ledger PRs (parity with stop-gate).
            if session_id:
                for r in _detached_pr_records(session_id, cwd):
                    if _record_has_blockers(r):
                        pending.append(_detached_pending_entry(r))

            if pending:
                print(_build_stop_block(pending))
                print(
                    "[pr-watch] Feedback arrived on PR(s) you own — address it now "
                    "(commit + push to the EXISTING branch, then run the per-worktree "
                    "complete command above)."
                )
                return 10

            if not owned and not getattr(args, "keep_alive_without_prs", False):
                return 0

            if deadline is not None and time.time() >= deadline:
                print(
                    "[pr-watch] waiter max-wait reached with no feedback; "
                    "will re-arm on next stop."
                )
                return 0

            time.sleep(max(args.interval, 1))
    finally:
        _remove_await_pidfile(cwd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentic-pr-dash",
        description="PR maintenance check — stateless read-only check and completion.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- check ---
    check_p = subparsers.add_parser(
        "check",
        help="Resolve branch→PR, compute blockers, print prompt. Read-only (no writes).",
    )
    check_p.add_argument(
        "--cwd", default=".", help="Worktree root (default: current directory)."
    )
    check_p.add_argument(
        "--session-id",
        default="",
        metavar="ID",
        help="Caller's session id (for the ownership gate). check DEFERS (exit 0) "
        "when the worktree's pr-watch.armed marker names a live session OTHER "
        "than this one — so a detached loop never fights a live in-session owner. "
        "Pass your own id to exclude yourself; omit to defer to any live owner.",
    )

    # --- complete ---
    complete_p = subparsers.add_parser(
        "complete",
        help="Re-fetch unresolved review threads from GitHub and resolve them statelessly.",
    )
    complete_p.add_argument(
        "--cwd", default=".", help="Worktree root (default: current directory)."
    )
    complete_p.add_argument(
        "--pr",
        type=int,
        default=None,
        metavar="N",
        help="PR number (default: resolve from current branch).",
    )
    complete_p.add_argument(
        "--baseline",
        default="",
        metavar="SHA",
        help="PR head SHA captured BEFORE the agent ran; only commits after it "
        "count as the fix when deciding which review threads to resolve.",
    )

    # --- list-owned ---
    list_owned_p = subparsers.add_parser(
        "list-owned",
        help="Print worktree paths whose pr-watch.armed marker matches --session-id.",
    )
    list_owned_p.add_argument(
        "--session-id",
        required=True,
        metavar="ID",
        help="Owning session id. Only worktrees whose marker carries a matching "
        "session_id= line are printed (one path per line).",
    )
    list_owned_p.add_argument(
        "--cwd",
        default=".",
        help="Directory to run `git worktree list` from (default: current directory).",
    )
    list_owned_p.add_argument(
        "--pid",
        type=int,
        default=None,
        metavar="PID",
        help="Owner pid stamped into markers ADOPTED by reconciliation (default: "
        "os.getppid() — the Claude session that ran this CLI via a shell). The "
        "pid lets the detached loop / other sessions defer while this owner is alive.",
    )

    # --- arm ---
    arm_p = subparsers.add_parser(
        "arm",
        help="Stamp a worktree's open non-draft PR with a pr-watch.armed marker for a session.",
    )
    arm_p.add_argument(
        "--cwd", default=".", help="Worktree root to arm (default: current directory)."
    )
    arm_p.add_argument(
        "--session-id",
        required=True,
        metavar="ID",
        help="Owning session id stamped into the marker (the parent orchestrator's id).",
    )
    arm_p.add_argument(
        "--pid",
        type=int,
        default=None,
        metavar="PID",
        help="Owner pid stamped into the marker (default: os.getppid()).",
    )
    arm_p.add_argument(
        "--pr",
        type=int,
        default=None,
        metavar="N",
        help="PR number to arm. When omitted, resolves the worktree branch's open "
        "@me PR via gh and refuses to arm a draft or a branch with no open PR.",
    )

    # --- stop-gate ---
    stop_gate_p = subparsers.add_parser(
        "stop-gate",
        help="Stop-hook gate: block (exit 2 + stderr prompt) when owned PRs have "
        "pending review/CI work, else exit 0. Time-rate-limited + loop-broken.",
    )
    stop_gate_p.add_argument(
        "--cwd", default=".", help="Worktree root (default: current directory)."
    )
    stop_gate_p.add_argument(
        "--session-id",
        default="",
        metavar="ID",
        help="Owning session id. Scopes the check to this session's worktrees; "
        "falls back to pr-watch.session, then to the cwd only.",
    )
    stop_gate_p.add_argument(
        "--pid",
        type=int,
        default=None,
        metavar="PID",
        help="Owner pid for marker adoption (default: os.getppid()).",
    )
    stop_gate_p.add_argument(
        "--no-waiter",
        action="store_true",
        default=False,
        help="Skip the waiter-enforcement branch (for codex/non-interactive callers "
        "that have no background-task wake channel).",
    )

    # --- reconcile-prs ---
    reconcile_p = subparsers.add_parser(
        "reconcile-prs",
        help="List every PR this session owns — live-worktree AND detached (ledger) "
             "PRs whose worktree was removed — with live review-thread/CI state, "
             "severity-first. Prunes merged/closed PRs (BOU-1587).",
    )
    reconcile_p.add_argument("--session-id", required=True, metavar="ID")
    reconcile_p.add_argument("--cwd", default=".")
    reconcile_p.add_argument("--pid", type=int, default=None, metavar="PID")
    reconcile_p.add_argument("--adopt-orphans", action="store_true",
                             help="Also claim PRs orphaned by DEAD sessions (Component G).")

    # --- await ---
    await_p = subparsers.add_parser(
        "await",
        help="Background feedback waiter: poll owned PRs and exit 10 when work arrives.",
    )
    await_p.add_argument(
        "--cwd", default=".", help="Launch cwd — state dir is resolved from here."
    )
    await_p.add_argument(
        "--session-id",
        default="",
        metavar="ID",
        help="Owning session id (falls back to pr-watch.session).",
    )
    await_p.add_argument(
        "--owner-pid",
        type=int,
        default=0,
        metavar="PID",
        help="Owning session pid; waiter exits 0 when it dies (default: "
        "walk ancestors for nearest claude/codex process).",
    )
    await_p.add_argument(
        "--interval",
        type=int,
        default=150,
        metavar="SECONDS",
        help="Poll interval in seconds (default: 150).",
    )
    await_p.add_argument(
        "--max-wait",
        type=int,
        default=21600,
        metavar="SECONDS",
        help="Maximum total wait seconds; 0 = one tick then exit (default: 21600).",
    )
    await_p.add_argument(
        "--keep-alive-without-prs",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args(argv)

    if args.command == "check":
        return _cmd_check(args)
    if args.command == "complete":
        return _cmd_complete(args)
    if args.command == "list-owned":
        return _cmd_list_owned(args)
    if args.command == "arm":
        return _cmd_arm(args)
    if args.command == "stop-gate":
        return _cmd_stop_gate(args)
    if args.command == "reconcile-prs":
        return _cmd_reconcile_prs(args)
    if args.command == "await":
        return _cmd_await(args)
    # Unreachable (subparsers required=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
