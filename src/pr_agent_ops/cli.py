"""Unified ``pr-agent-ops`` command-line entry point.

Subcommands:

    check         Resolve branch->PR, compute blockers, print a fix prompt (read-only).
    complete      Resolve review threads the fix addressed; post completion replies.
    arm           Stamp a worktree's open PR with an ownership marker for a session.
    list-owned    Print worktree paths a session owns.
    loop          Run check/fix/complete continuously, dispatching to a configured agent.
    serve         Run the web dashboard.

``check/complete/arm/list-owned`` route into the stateless maintenance executor;
``loop`` and ``serve`` are runtime drivers. Run ``pr-agent-ops <cmd> --help`` for
per-subcommand options.
"""

from __future__ import annotations

import sys

_EXECUTOR_CMDS = {"check", "complete", "arm", "list-owned"}
_USAGE = __doc__


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_USAGE)
        return 0 if argv and argv[0] in ("-h", "--help", "help") else 2

    cmd, rest = argv[0], argv[1:]

    if cmd in _EXECUTOR_CMDS:
        from . import maintenance_check
        return maintenance_check.main([cmd, *rest])

    if cmd == "loop":
        from . import loop
        return loop.main(rest)

    if cmd == "serve":
        from .server import main as serve_main
        serve_main()
        return 0

    print(f"pr-agent-ops: unknown command {cmd!r}\n", file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
