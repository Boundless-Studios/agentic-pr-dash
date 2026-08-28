"""Unified ``agentic-pr-dash`` command-line entry point.

Subcommands:

    check         Resolve branch->PR, compute blockers, print a fix prompt (read-only).
    finalize      Require review settlement and two stable green PR observations.
    complete      Resolve review threads the fix addressed; post completion replies.
    arm           Stamp a worktree's open PR with an ownership marker for a session.
    list-owned    Print worktree paths a session owns.
    stop-gate     Stop-hook core: block idling while owned PRs have pending work.
    reconcile-prs List every PR a session owns (live + detached ledger), with state.
    record        Record a session lifecycle event (for the dashboard's live view).
    session-report Ingest harness StatusReport v1 JSON from stdin.
    loop          Run check/fix/complete continuously, dispatching to a configured agent.
    serve         Run the web dashboard.
    observe       Inspect the observability event store (comment scans, dispatches, etc.).
    stash         Race-safe labeled-stash push/apply/drop/list (shared cross-worktree stack).
    lifecycle-hook Run the local-only cross-runtime PR convergence hook.
    delivery-checklist Project local and exact-head remote completion evidence.

``check/finalize/complete/arm/list-owned/stop-gate/reconcile-prs`` route into
the stateless maintenance executor; ``record`` / ``session-report`` into the
session registry; ``loop`` and ``serve`` are runtime drivers. Run
``agentic-pr-dash <cmd> --help`` for per-subcommand options.
"""

from __future__ import annotations

import sys

_EXECUTOR_CMDS = {
    "check",
    "finalize",
    "complete",
    "arm",
    "list-owned",
    "stop-gate",
    "reconcile-prs",
    "await",
}
_USAGE = __doc__


# ---------------------------------------------------------------------------
# observe subcommand helpers
# ---------------------------------------------------------------------------


def _observe_get_event_store(cwd: str):
    """Indirection layer so tests can monkeypatch this without re-importing."""
    from .observability import get_event_store
    return get_event_store(cwd)


def _humanize_age(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _short_detail(details: dict) -> str:
    if not details:
        return ""
    for key in ("executor", "reason", "state", "message", "event", "decisions"):
        if key in details:
            v = details[key]
            if isinstance(v, list):
                return f"{key}=[{len(v)} items]"
            return f"{key}={v!r}"
    k, v = next(iter(details.items()))
    return f"{k}={v!r}"


def _observe_main(argv: list[str]) -> int:
    import argparse
    from datetime import UTC, datetime

    parser = argparse.ArgumentParser(prog="agentic-pr-dash observe")
    parser.add_argument("--pr", type=int, default=None, help="Filter by PR number")
    parser.add_argument("--kind", type=str, default=None, help="Filter by event kind")
    parser.add_argument("--since", type=str, default=None, help="ISO 8601 datetime lower bound (inclusive)")
    parser.add_argument("--limit", type=int, default=50, help="Max events to return (default 50)")
    parser.add_argument("--cwd", type=str, default=".", help="Worktree directory for config resolution")
    args = parser.parse_args(argv)

    since_dt = None
    if args.since:
        since_dt = datetime.fromisoformat(args.since)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=UTC)

    store = _observe_get_event_store(args.cwd)

    # Special view: --pr given AND --kind NOT given → show latest comment_scan as a table.
    if args.pr is not None and args.kind is None:
        scans = store.query(pr_number=args.pr, kind="comment_scan", limit=1)
        if not scans:
            print(f"no comment_scan recorded for PR {args.pr}")
            return 0
        ev = scans[0]
        decisions = ev.details.get("decisions", [])
        picked = ev.details.get("picked", 0)
        total = len(decisions)
        print(f"comment_scan  PR #{args.pr}  {ev.ts.isoformat()}  picked {picked} of {total}")
        print(f"{'DECISION':<22}  {'AUTHOR':<20}  {'AGE':<8}  {'THREAD':<14}  MARKER_STATE")
        print("-" * 82)
        for d in decisions:
            age_s = d.get("age_seconds") or d.get("claim_age_seconds") or 0
            age_h = _humanize_age(age_s)
            thread_id = str(d.get("thread_id", ""))
            thread_short = thread_id[-12:] if len(thread_id) > 12 else thread_id
            print(
                f"{d.get('decision', ''):<22}  "
                f"{d.get('author', ''):<20}  "
                f"{age_h:<8}  "
                f"{thread_short:<14}  "
                f"{d.get('marker_state', '')}"
            )
        print(f"\npicked {picked} of {total}")
        return 0

    # Generic listing: apply all filters.
    events = store.query(pr_number=args.pr, kind=args.kind, since=since_dt, limit=args.limit)
    for ev in events:
        pr_str = f"PR#{ev.pr_number}" if ev.pr_number is not None else "PR#-"
        short = _short_detail(ev.details)
        # actor is what disambiguates same-`kind` events from different surfaces —
        # notably dashboard-queue's "wrote a work order" vs loop-executor's "ran
        # the executor and pushed", both of which are kind="dispatch" (BOU-2490).
        # Rows written before BOU-2490 have no actor; show that rather than guess.
        actor = ev.actor or "unknown"
        print(f"{ev.ts.isoformat()}  {ev.kind:<16}  {actor:<15}  {pr_str}  {short}")
    return 0


# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_USAGE)
        return 0 if argv and argv[0] in ("-h", "--help", "help") else 2

    cmd, rest = argv[0], argv[1:]

    if cmd in _EXECUTOR_CMDS:
        from . import maintenance_check
        return maintenance_check.main([cmd, *rest])

    if cmd == "record":
        from . import session_registry
        return session_registry.main([cmd, *rest])

    if cmd == "session-report":
        from . import session_registry
        return session_registry.main(["report", *rest])

    if cmd == "loop":
        from . import loop
        return loop.main(rest)

    if cmd == "serve":
        from .server import main as serve_main
        serve_main()
        return 0

    if cmd == "observe":
        return _observe_main(rest)

    if cmd == "stash":
        from . import stash
        return stash.main(rest)

    if cmd == "lifecycle-hook":
        from .codex_hooks import run_pr_convergence
        return run_pr_convergence.main(rest)

    if cmd == "delivery-checklist":
        from . import delivery_checklist
        return delivery_checklist.main(rest)

    print(f"agentic-pr-dash: unknown command {cmd!r}\n", file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
