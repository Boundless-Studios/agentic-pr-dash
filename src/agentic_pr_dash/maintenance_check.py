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
import os
import subprocess  # noqa: F401  — kept for mc.subprocess patch seam used by tests
import sys
import time

from .config import load as load_config

# ---------------------------------------------------------------------------
# Re-exports from maintenance subpackage (facade — keeps mc.X patchable by tests)
# ---------------------------------------------------------------------------

# _common
from ._maintenance._common import (  # noqa: F401, E402
    _parse_iso,
    _env_int,
    _fix_lease_seconds,
    _pid_alive,
    _resolve_owner_pid,
    _current_branch,
    _repo_slug,
)

# pr_state
from ._maintenance.pr_state import (  # noqa: F401, E402
    _GH_UNAVAILABLE,
    _gh_unavailable_message,
    _resolve_pr_for_branch,
    _resolve_pr_by_number,
    _pr_draft_status,
    _pr_head_branch,
    _gh_pr_list_json,
    _resolve_open_pr_for_branch,
    _list_my_open_prs,
    _unresolved_review_threads,
    pr_has_unresolved_review_threads,
    _pr_open_state,
    _unpack_pr_open_state,
    _thread_is_p1,
)

# markers
from ._maintenance.markers import (  # noqa: F401, E402
    _HEARTBEAT_TTL_SECONDS,
    _DEFAULT_FIX_LEASE_SECONDS,
    _DEFAULT_HEARTBEAT_MIN_INTERVAL_SECONDS,
    _marker_path,
    _read_marker,
    _heartbeat_ttl_seconds,
    _heartbeat_fresh,
    _fix_lease_active,
    _live_foreign_owner,
    _marker_live_foreign_pid,
    _heartbeat_min_interval_seconds,
    _touch_owner_heartbeat,
    _write_arm_marker,
    _marker_session_id,
    _read_session_marker,
    _prune_stale_marker,
    _session_is_live,
    _claim_pr,
)

# worktrees
from ._maintenance.worktrees import (  # noqa: F401, E402
    _iter_worktrees_with_branch,
    _iter_worktree_paths,
    _resolve_maintenance_roots,
    _maint_roots_for,
    _owned_worktrees_across_roots,
    _detached_records_across_roots,
    _self_pid_chain,
    _live_independent_owner_paths,
    _collect_owned_worktrees,
    _collect_stop_gate_worktrees,
    _worktree_is_for_entry,
)

# stop_gate
from ._maintenance.stop_gate import (  # noqa: F401, E402
    _stop_state_path,
    _load_stop_state,
    _save_stop_state,
    _stop_fingerprint,
    _extract_pr_number,
    _build_stop_block,
    _owned_open_pr_numbers,
    _build_waiter_block,
    _stop_gate_impl,
    _record_has_blockers,
)

# completion
from ._maintenance.completion import (  # noqa: F401, E402
    _commit_subject,
    _completion_reply_body,
    _mark_maintenance_complete,
    _review_comments_from_threads,
    _candidate_file_refs,
    _ref_matches_touched,
    _thread_points_elsewhere,
    _FILE_REF_RE,
    _MODULE_REF_STOPWORDS,
)

# reconcile
from ._maintenance.reconcile import (  # noqa: F401, E402
    _adopt_orphan_prs,
    _detached_pr_records,
    _owned_pr_records,
    _owned_pr_records_all_roots,
)

# waiter
from ._maintenance.waiter import (  # noqa: F401, E402
    _await_pidfile,
    _read_await_pidfile,
    _write_await_pidfile,
    _remove_await_pidfile,
    _await_alive,
    _detached_loop_alive,
    _detached_pending_entry,
)


# ---------------------------------------------------------------------------
# worktree_check
# ---------------------------------------------------------------------------
from ._maintenance.worktree_check import _check_worktree  # noqa: F401, E402


# ---------------------------------------------------------------------------
# CLI functions (stay in maintenance_check.py)
# ---------------------------------------------------------------------------


def _cmd_check(args: argparse.Namespace) -> int:
    code, text = _check_worktree(args.cwd, args.session_id or "")
    print(text)
    return code


def _cmd_arm(args: argparse.Namespace) -> int:
    """Explicitly register a worktree's open non-draft PR under a session."""
    cwd = os.path.abspath(args.cwd)
    session_id = args.session_id
    pid = args.pid if args.pid is not None else _resolve_owner_pid()

    pr_number = args.pr
    if pr_number is None:
        explicit_branch = getattr(args, "branch", None)
        if explicit_branch:
            explicit_branch = explicit_branch.split(":", 1)[-1]
        current_branch = _current_branch(cwd)
        if explicit_branch and explicit_branch != current_branch:
            print(
                f"branch {explicit_branch} is not checked out in {cwd}; not arming"
            )
            return 0
        branch = explicit_branch or current_branch
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
        status = _pr_draft_status(cwd, int(pr_number))
        if status is None:
            print(f"could not verify PR #{pr_number} is non-draft (gh unavailable); not arming")
            return 0
        if status:
            print(f"PR #{pr_number} is a draft; not arming")
            return 0
        head_branch = _pr_head_branch(cwd, int(pr_number))
        if head_branch is None:
            print(f"could not verify PR #{pr_number}'s head branch (gh unavailable); not arming")
            return 0
        if head_branch != _current_branch(cwd):
            print(
                f"PR #{pr_number} (head {head_branch}) is not checked out in {cwd}; not arming"
            )
            return 0

    if _write_arm_marker(cwd, session_id, int(pid), int(pr_number)):
        print(f"armed PR #{pr_number} for session {session_id} in {cwd}")
        return 0
    print(f"could not write arm marker in {cwd}", file=sys.stderr)
    return 1


def _cmd_list_owned(args: argparse.Namespace) -> int:
    """Print worktree paths this session owns — markered OR reconciled-and-adopted."""
    anchor = os.path.abspath(os.path.expanduser(args.cwd))
    seen: set[str] = set()
    anchor_failed = False
    resolved = _resolve_maintenance_roots(args.cwd)
    roots = resolved if anchor in resolved else [anchor, *resolved]
    for root in roots:
        try:
            probe = subprocess.run(
                ["git", "-C", root, "worktree", "list", "--porcelain"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            print(f"list-owned: worktree probe failed/timed out: {root}", file=sys.stderr)
            if root == anchor:
                anchor_failed = True
            continue
        if probe.returncode != 0:
            print(f"list-owned: not a git worktree: {root}", file=sys.stderr)
            if root == anchor:
                anchor_failed = True
            continue
        for path in _collect_owned_worktrees(args.session_id, root, args.pid):
            if path not in seen:
                seen.add(path)
                print(path)
    return 3 if anchor_failed else 0


def _cmd_complete(args: argparse.Namespace) -> int:
    from . import github_api, maintenance  # noqa: PLC0415
    from .github_api import COMPLETE_MARKER  # noqa: PLC0415
    from .models import ReviewComment  # noqa: PLC0415

    cwd = os.path.abspath(args.cwd)
    pr_number_arg = args.pr

    if pr_number_arg is not None:
        pr = _resolve_pr_by_number(int(pr_number_arg), cwd)
    else:
        pr = _resolve_pr_for_branch(cwd)

    if pr is _GH_UNAVAILABLE:
        print(_gh_unavailable_message(cwd))
        return 2
    if pr is None:
        print("no open PR for this branch")
        return 0

    resolved_pr_number = pr.number
    head_sha = pr.latest_commit_sha
    head_date = pr.latest_commit_date
    api_head_sha = pr.latest_commit_sha

    local_head_sha, local_head_date = github_api.get_local_pr_head(pr.branch, cwd)
    local_is_fresh = bool(local_head_sha) and (
        not api_head_sha
        or github_api._is_ancestor(api_head_sha, local_head_sha, cwd)
    )
    if local_is_fresh:
        head_sha = local_head_sha
        if local_head_date:
            head_date = local_head_date

    baseline = args.baseline or ""
    new_commits = github_api.get_new_pr_commits(
        resolved_pr_number, baseline, head_sha, cwd, pr_branch=pr.branch,
        api_head_sha=api_head_sha)
    touched: set[str] = set()
    commits_by_file: dict[str, list[tuple[str, str]]] = {}
    for sha, msg in new_commits:
        try:
            files = github_api.get_commit_changed_files(sha, cwd)
        except Exception:  # noqa: BLE001
            files = []
        for changed in files:
            touched.add(changed)
            commits_by_file.setdefault(changed, []).append((sha, msg))

    threads = github_api.get_review_threads(resolved_pr_number, cwd)
    for thread in threads:
        if thread.is_resolved:
            continue
        path = thread.top.path
        addressed = (
            bool(new_commits)
            and head_date > thread.top.created_at
            and (path is None or path in touched)
        )
        if not addressed:
            continue
        if _thread_points_elsewhere(thread.top.body, path, touched):
            print(
                f"info: leaving thread {thread.node_id} open — body references a "
                f"file/module not touched by the fixing commits (ambiguous "
                f"resolution); needs manual confirmation",
                file=sys.stderr,
            )
            continue
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

    fresh = _resolve_pr_by_number(resolved_pr_number, cwd)
    if fresh is _GH_UNAVAILABLE or fresh is None:
        remaining = ["unknown"]
    else:
        remaining = maintenance.blockers_for_pr(fresh)

    _mark_maintenance_complete(maintenance, cwd, resolved_pr_number)

    if remaining:
        print(f"completed (bead left open; blockers remain: {', '.join(remaining)})")
        return 0

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


def _cmd_stop_gate(args: argparse.Namespace) -> int:
    try:
        return _stop_gate_impl(args)
    except Exception:  # noqa: BLE001
        return 0


def _cmd_reconcile_prs(args: argparse.Namespace) -> int:
    import json as _json  # noqa: PLC0415
    records = _owned_pr_records_all_roots(args.session_id, os.path.abspath(args.cwd),
                                          args.pid, adopt_orphans=args.adopt_orphans)
    for r in records:
        print(_json.dumps(r))
    return 0


def _cmd_await(args: argparse.Namespace) -> int:
    """Background feedback waiter — poll owned PRs, exit 10 when work arrives."""
    cwd = os.path.abspath(args.cwd)
    session_id = args.session_id or _read_session_marker(cwd)
    owner_pid = args.owner_pid if args.owner_pid else _resolve_owner_pid()

    existing = _read_await_pidfile(cwd, session_id)
    if (
        existing
        and _pid_alive(str(existing.get("pid", "")))
        and existing.get("session_id") == session_id
    ):
        print("[pr-watch] waiter already running for this session", file=sys.stderr)
        return 3

    _write_await_pidfile(cwd, {"pid": os.getpid(), "session_id": session_id}, session_id)
    now = time.time()
    if args.max_wait == 0:
        deadline: float | None = now
    elif args.max_wait > 0:
        deadline = now + args.max_wait
    else:
        deadline = None
    try:
        while True:
            if not _pid_alive(str(owner_pid)):
                return 0

            owned = _owned_worktrees_across_roots(session_id, cwd) if session_id else [cwd]

            pending: list[tuple[str, str]] = []
            for worktree in owned:
                code, text = _check_worktree(worktree, session_id, claim=False)
                if code == 10:
                    pending.append((worktree, text))
                _touch_owner_heartbeat(worktree, session_id, code == 10)

            _detached_this_tick: list[dict] = []
            if session_id:
                _detached_this_tick = _detached_records_across_roots(session_id, cwd)
                for r in _detached_this_tick:
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

            has_open_detached = any(
                r.get("state") not in ("merged", "closed", "draft", "unknown")
                for r in _detached_this_tick
            )
            if not owned and not has_open_detached and not getattr(args, "keep_alive_without_prs", False):
                return 0

            if deadline is not None and time.time() >= deadline:
                print(
                    "[pr-watch] waiter max-wait reached with no feedback; "
                    "will re-arm on next stop."
                )
                return 0

            time.sleep(max(args.interval, 1))
    finally:
        _remove_await_pidfile(cwd, session_id)


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
    arm_p.add_argument(
        "--branch",
        default=None,
        metavar="BRANCH",
        help="Head branch whose open PR to arm (e.g. `gh pr create --head <branch>` "
        "from a sibling worktree). Ignored when --pr is given; when both are "
        "omitted the worktree's current branch is resolved.",
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
