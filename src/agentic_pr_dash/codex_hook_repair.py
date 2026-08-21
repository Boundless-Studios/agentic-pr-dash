#!/usr/bin/env python3
"""repair_global_codex_hooks.py — detect and repair stale peon-ping entries in a Codex hooks.json.

The "stale" pattern is any hook command that directly invokes the personal
peon-ping adapter:

    bash /path/to/.claude/hooks/peon-ping/adapters/codex.sh
    bash ~/.claude/hooks/peon-ping/adapters/codex.sh
    bash $HOME/.claude/hooks/peon-ping/adapters/codex.sh
    bash "$HOME/.claude/hooks/peon-ping/adapters/codex.sh"  (quoted)
    bash '/path/.claude/hooks/peon-ping/adapters/codex.sh'  (single-quoted)

This bypasses the repo-managed fail-open bounded wrapper
(`scripts/codex-hooks/run_peon_ping.py`, PR #2082) and exposes the session
to Codex's raw 10 s outer timeout whenever peon-ping stalls.

Also detected and normalized are peon-ping wrapper entries whose path is
cwd-dependent (e.g. uses ``$(git rev-parse --show-toplevel)``), or points
into a non-primary (worktree) path.  These break when Codex's cwd is a
different repo or when a worktree is deleted.

The repaired command delegates through the ABSOLUTE wrapper path supplied by
the host checkout, whose adapter knows which main-repo-root survives worktree
cleanup. This package deliberately does not infer that host path from its own
installation location:

    python3 /absolute/primary-checkout/scripts/codex-hooks/run_peon_ping.py

Usage (idempotent — safe to call on every SessionStart):

    python3 -m agentic_pr_dash.codex_hook_repair --wrapper PATH [--target PATH] [--dry-run]

    --wrapper PATH  Canonical host-checkout run_peon_ping.py path. May also be
                    supplied via $CODEX_PEON_PING_WRAPPER.
    --target PATH   Path to the hooks.json to inspect/repair.  Defaults to
                    $CODEX_HOME/hooks.json or ~/.codex/hooks.json.
    --dry-run       Print what would change but do not write.
    --help          Show this message and exit.

Exit codes:
    0   No stale entries found, or all stale entries successfully repaired.
    1   Write error (disk full, permission denied).
    2   Invalid usage.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path


# Pattern that matches the stale direct-adapter invocation.
# Covers absolute paths (/path/...), tilde paths (~/.../), and $HOME-prefixed
# paths, with or without a leading shell verb ("bash", "sh", or bare path).
# Handles optional single or double quoting of the path argument (e.g.
# ``bash "$HOME/.../codex.sh"`` or ``bash '/abs/path/codex.sh'``).
# Pattern that matches a peon-ping wrapper entry whose path relies on
# $(git rev-parse --show-toplevel) expansion — cwd-dependent and broken
# outside a git repo or in a different repo.
_CWD_DEPENDENT_WRAPPER_RE = re.compile(
    r"\$\(git\s+rev-parse\b[^)]*\)",
    re.IGNORECASE,
)


def _wrapper_command(wrapper: str | Path) -> str:
    """Build the replacement from the host-supplied canonical wrapper path."""
    return f'python3 "{Path(wrapper).expanduser().resolve()}"'


def _is_stale(command: str) -> bool:
    """Return True if *command* directly invokes the peon-ping adapter (codex.sh).

    This covers the direct-adapter bypass pattern in all its forms (absolute,
    tilde, $HOME, quoted).  It does NOT flag wrapper entries whose path is merely
    cwd-dependent; use ``_is_cwd_dependent_wrapper`` for that.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return any(token.lower().endswith("peon-ping/adapters/codex.sh") for token in tokens)


def _is_cwd_dependent_wrapper(command: str) -> bool:
    """Return True if *command* invokes run_peon_ping.py via a cwd-dependent path.

    A cwd-dependent path uses ``$(git rev-parse --show-toplevel)`` expansion,
    which resolves to the wrong location (or fails entirely) when Codex's cwd
    is a different repo or outside any git repo.  These entries must also be
    normalized to the stable absolute primary-checkout path.
    """
    if "run_peon_ping.py" not in command:
        return False
    return bool(_CWD_DEPENDENT_WRAPPER_RE.search(command))


def _is_stale_absolute_wrapper(command: str, primary_wrapper: str) -> bool:
    """Return True if *command* is a peon-ping wrapper whose absolute path is
    NOT the current primary-checkout path.

    This covers the case where a prior repair run (or a Codex session running
    from a feature worktree) wrote the WORKTREE's absolute path into the global
    ~/.codex/hooks.json instead of the primary-checkout path.  Once that worktree
    is deleted the path no longer exists; even while it exists it points at the
    wrong copy.

    Only fires when:
    - The command references ``run_peon_ping.py`` (so we never touch unrelated hooks).
    - The command does NOT already equal the canonical *primary_wrapper* (so we
      never churn an entry that is already correct).
    - The command does NOT contain ``$(git rev-parse`` (that case is handled by
      ``_is_cwd_dependent_wrapper`` already).
    """
    if "run_peon_ping.py" not in command:
        return False
    try:
        command_tokens = shlex.split(command)
        primary_tokens = shlex.split(primary_wrapper)
    except ValueError:
        return True
    canonical_paths = [token for token in primary_tokens if token.endswith("run_peon_ping.py")]
    invocation_paths = [token for token in command_tokens if token.endswith("run_peon_ping.py")]
    if canonical_paths and invocation_paths and any(
        Path(path).expanduser().resolve() == Path(canonical_paths[0]).expanduser().resolve()
        for path in invocation_paths
    ):
        return False
    # Cwd-dependent form — already handled by _is_cwd_dependent_wrapper.
    if _CWD_DEPENDENT_WRAPPER_RE.search(command):
        return False
    # Any remaining command that mentions run_peon_ping.py but differs from the
    # canonical wrapper is a stale absolute path (e.g. a deleted worktree or an
    # old primary checkout that has since moved).
    return True


def _needs_repair(command: str, primary_wrapper: str | None = None) -> bool:
    """Return True if *command* needs to be replaced with the canonical wrapper.

    *primary_wrapper* is the value of :func:`_wrapper_command()`.  When omitted
    (e.g. from callers that pre-date this parameter) it is computed lazily so
    that the call is still correct but incurs a subprocess round-trip.
    """
    if _is_stale(command) or _is_cwd_dependent_wrapper(command):
        return True
    if primary_wrapper is None:
        return False  # host path is required; never infer it from this package
    return _is_stale_absolute_wrapper(command, primary_wrapper)


def _repair_config(config: dict, replacement: str) -> tuple[dict, int]:
    """Walk *config* and replace stale peon-ping adapter invocations.

    Replaces both:
    - Direct adapter invocations (codex.sh bypasses)
    - Cwd-dependent wrapper entries ($(git rev-parse ...) forms)

    Returns *(repaired_config, count)* where *count* is the number of entries
    that were rewritten.  The original dict is not mutated.
    """
    import copy

    repaired = copy.deepcopy(config)
    count = 0
    hooks_by_event: dict = repaired.get("hooks", {})
    for _event_name, groups in hooks_by_event.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks", []):
                if not isinstance(hook, dict):
                    continue
                cmd = hook.get("command", "")
                if isinstance(cmd, str) and _needs_repair(cmd, primary_wrapper=replacement):
                    hook["command"] = replacement
                    count += 1

    return repaired, count


def default_target_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME", "")
    if codex_home:
        return Path(codex_home) / "hooks.json"
    return Path.home() / ".codex" / "hooks.json"


def main(argv: list[str] | None = None) -> int:
    # Defer everything that's not argparse until after parse_args() so that
    # `--help` works without a backend/codex environment (repo convention).
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target",
        metavar="PATH",
        default=None,
        help="Path to the hooks.json file to repair (default: $CODEX_HOME/hooks.json "
        "or ~/.codex/hooks.json)",
    )
    parser.add_argument(
        "--wrapper",
        metavar="PATH",
        default=os.environ.get("CODEX_PEON_PING_WRAPPER"),
        help="Absolute host-checkout path to scripts/codex-hooks/run_peon_ping.py",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report stale entries but do not write changes",
    )
    args = parser.parse_args(argv)

    target = Path(args.target) if args.target else default_target_path()

    if not target.exists():
        # Nothing to repair — file may not exist on a fresh machine.
        return 0

    try:
        raw = target.read_text(encoding="utf-8")
        config = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"repair_global_codex_hooks: cannot read {target}: {exc}",
            file=sys.stderr,
        )
        return 0  # best-effort: never fail the SessionStart chain

    if not isinstance(config, dict) or not isinstance(config.get("hooks", {}), dict):
        print(
            f"repair_global_codex_hooks: invalid hooks configuration in {target}; skipping",
            file=sys.stderr,
        )
        return 0
    if not args.wrapper:
        print(
            "repair_global_codex_hooks: --wrapper (or CODEX_PEON_PING_WRAPPER) "
            "must be supplied by the host checkout; skipping",
            file=sys.stderr,
        )
        return 0

    replacement = _wrapper_command(args.wrapper)
    repaired, count = _repair_config(config, replacement)

    if count == 0:
        return 0  # already clean

    print(
        f"repair_global_codex_hooks: found {count} stale peon-ping adapter "
        f"entry/entries in {target}; "
        + ("would repair (--dry-run)" if args.dry_run else "repairing"),
        file=sys.stderr,
    )

    if args.dry_run:
        return 0

    try:
        new_text = json.dumps(repaired, indent=2) + "\n"
        target_mode = target.stat().st_mode
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, target_mode)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(new_text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        finally:
            temp_path.unlink(missing_ok=True)
    except OSError as exc:
        print(
            f"repair_global_codex_hooks: failed to write {target}: {exc}",
            file=sys.stderr,
        )
        return 1

    print(
        f"repair_global_codex_hooks: repaired {count} entry/entries in {target}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
