"""Runtime-agnostic PR maintenance check CLI — stateless, read-only worker.

Subcommands:
  check    — resolve branch→PR, compute live blockers, print prompt, exit 10
             if work exists, else exit 0. READ-ONLY: writes no files.
  complete — re-fetch unresolved review threads from GitHub and resolve them
             statelessly (no ledger required). Best-effort close the bead.

Run via the CLI:  agentic-pr-dash <subcommand> [args]
Or as a module:   python -m agentic_pr_dash <subcommand> [args]

Module-level imports are stdlib only (plus the dependency-free ``config``
module); heavy deps are deferred into subcommand functions so that ``--help``
works without the optional web extras installed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

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

    return PRData(
        number=pr_number,
        title=raw.get("title", ""),
        branch=branch,
        base_branch=raw.get("baseRefName", "main"),
        url=raw.get("url", ""),
        is_draft=bool(raw.get("isDraft", False)),
        merge_state=merge_state,
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

    return PRData(
        number=pr_number,
        title=(raw or {}).get("title", ""),
        branch=(raw or {}).get("headRefName", ""),
        base_branch=(raw or {}).get("baseRefName", "main"),
        url=(raw or {}).get("url", ""),
        is_draft=bool((raw or {}).get("isDraft", False)),
        merge_state=merge_state,
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
    # Legacy override kept as a fallback so existing installs keep working.
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


def _heartbeat_fresh(heartbeat: str) -> bool:
    """True if the owner's loop heartbeat is within the short alive-TTL of now —
    proof the in-session loop is still ticking, not merely armed or stopped.
    """
    ts = _parse_iso(heartbeat)
    if ts is None:
        return False
    from datetime import datetime, timezone  # noqa: PLC0415

    return (datetime.now(timezone.utc) - ts).total_seconds() <= _HEARTBEAT_TTL_SECONDS


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
    if _heartbeat_fresh(fields.get("heartbeat", "")) or _fix_lease_active(fields.get("fix_lease_until", "")):
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
    """Write the `pr-watch.armed` ownership marker (the single writer).

    Produces the EXACT same marker `.claude/hooks/arm-pr-watch.sh` writes
    (pr=/armed_at=/session_id=/pid=), atomically (tempfile + os.replace) so a
    concurrent reader never sees a torn marker. Also writes `pr-watch.session`
    so the worktree self-identifies. Reused by both the explicit `arm` subcommand
    and `list-owned` reconciliation (BOU-1442). Returns True on success.
    """
    import tempfile  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    cfg = load_config(cwd)
    state_dir = str(cfg.state_dir_for(cwd))
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
        if branch:
            out[branch] = (int(entry.get("number")), bool(entry.get("isDraft", False)))
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


def _cmd_check(args: argparse.Namespace) -> int:
    from . import github_api, maintenance  # noqa: PLC0415

    cwd = os.path.abspath(args.cwd)
    self_session_id = args.session_id or ""

    # Ownership gate — defer to a live, actively-looping in-session owner before
    # doing any work (cheap, before resolving the PR). See _live_foreign_owner.
    owner = _live_foreign_owner(cwd, self_session_id)
    if owner is not None:
        print(f"deferring to live PR-watch owner session {owner}")
        return 0

    # Resolve PR
    pr = _resolve_pr_for_branch(cwd)

    if pr is _GH_UNAVAILABLE:
        print("could not list PRs (gh unavailable)")
        return 2
    if pr is None:
        print("no open PR for this branch")
        return 0

    # Never service a DRAFT — the author marked it not-ready. This guards the
    # case where a PR was armed while open and later converted to draft
    # (arm/reconcile already refuse to arm a draft in the first place; this is
    # defense-in-depth on the read path, PR #1949 review).
    if pr.is_draft:
        print("PR is a draft; nothing pending")
        return 0

    # Check for blockers — no state written, purely read
    blockers = maintenance.blockers_for_pr(pr)

    # If we ARE the owner, refresh ownership stamps: heartbeat always (loop is
    # alive), and the long fix lease ONLY when there's work (we're about to fix).
    _touch_owner_heartbeat(cwd, self_session_id, bool(blockers))

    if not blockers:
        print("nothing pending")
        return 0

    # Fetch failed CI logs
    logs: dict[str, str] = {}
    if pr.failing_checks:
        logs = github_api.get_failed_logs(pr.latest_commit_sha, pr.failing_checks, cwd)

    # Print the maintenance prompt and exit 10 — NO writes
    prompt = maintenance.build_maintenance_prompt(pr, failed_logs=logs)
    print(prompt, end="")
    print(f"\nPR_NUMBER={pr.number}")
    return 10


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
    session_id = args.session_id
    cwd = os.path.abspath(args.cwd)
    if not session_id:
        return 0
    pid = args.pid if args.pid is not None else _resolve_owner_pid()

    # One repo-wide gh call; {} when gh is unavailable → reconciliation is simply
    # skipped (the markered pass below still works deps-free).
    pr_map = _list_my_open_prs(cwd)

    seen: set[str] = set()

    def _emit(path: str) -> None:
        if path not in seen:
            seen.add(path)
            print(path)

    # `git worktree list` is already scoped to THIS repo's worktree pool (wherever
    # they live on disk — a sibling `<repo>-worktrees/` dir for bug-bash, or
    # alongside the launch worktree for feature-pipeline), so it is the correct
    # candidate set. We deliberately do NOT additionally filter by the caller's
    # parent directory: bug-bash runs this from the MAIN repo while its sub-agent
    # worktrees live under `<repo>-worktrees/`, so a dirname filter would silently
    # skip exactly the PRs we must adopt. Cross-session safety comes from the
    # UNOWNED gate below (a live foreign owner is never stolen), not from paths.
    for worktree_path, branch in _iter_worktrees_with_branch(cwd):
        # 1) already ours
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
        if _write_arm_marker(worktree_path, session_id, int(pid), int(number)):
            _emit(worktree_path)
    return 0


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

    # Verify a fixing push actually landed before resolving anything. The loop
    # passes --baseline = the PR head SHA captured BEFORE the agent ran, so
    # `new_commits` is exactly what the agent pushed; without it we fall back to
    # all PR commits + the per-thread timestamp guard below. This keeps the
    # worker stateless (no stored ledger) while refusing to resolve threads when
    # the runtime exited 0 without pushing a fix (or pushed an unrelated change).
    baseline = args.baseline or ""
    new_commits = github_api.get_new_pr_commits(resolved_pr_number, baseline, head_sha, cwd)
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

    # Best-effort close the open tracked task via the configured tracker.
    branch = pr.branch
    if branch:
        from .config import load as _load_config  # noqa: PLC0415
        from .tracker import get_tracker  # noqa: PLC0415

        tracker = get_tracker(_load_config(cwd))
        task_id = tracker.find_task(pr=resolved_pr_number, branch=branch, cwd=cwd)
        if task_id:
            tracker.close_task(task_id, cwd=cwd)

    print("completed (task closed; no blockers remain)")
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


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

    args = parser.parse_args(argv)

    if args.command == "check":
        return _cmd_check(args)
    if args.command == "complete":
        return _cmd_complete(args)
    if args.command == "list-owned":
        return _cmd_list_owned(args)
    if args.command == "arm":
        return _cmd_arm(args)
    # Unreachable (subparsers required=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
