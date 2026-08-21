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

The repaired command delegates through the wrapper using the ABSOLUTE path
resolved to the PRIMARY checkout (the main-repo-root that survives worktree
cleanup) so the command works correctly when Codex runs in a different working
directory or a non-git repo:

    python3 /absolute/primary-checkout/scripts/codex-hooks/run_peon_ping.py

Usage (idempotent — safe to call on every SessionStart):

    python3 scripts/codex-hooks/repair_global_codex_hooks.py [--target PATH] [--dry-run]

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
import subprocess
import sys
from pathlib import Path


# Pattern that matches the stale direct-adapter invocation.
# Covers absolute paths (/path/...), tilde paths (~/.../), and $HOME-prefixed
# paths, with or without a leading shell verb ("bash", "sh", or bare path).
# Handles optional single or double quoting of the path argument (e.g.
# ``bash "$HOME/.../codex.sh"`` or ``bash '/abs/path/codex.sh'``).
_STALE_ADAPTER_RE = re.compile(
    r"""(?:bash\s+|sh\s+)?(?:["']?)(?:/|~|\$HOME/)(?:[^\s"']*?)peon-ping/adapters/codex\.sh(?:["']?)""",
    re.IGNORECASE,
)

# Pattern that matches a peon-ping wrapper entry whose path relies on
# $(git rev-parse --show-toplevel) expansion — cwd-dependent and broken
# outside a git repo or in a different repo.
_CWD_DEPENDENT_WRAPPER_RE = re.compile(
    r"\$\(git\s+rev-parse\b[^)]*\)",
    re.IGNORECASE,
)


def _primary_checkout_root() -> Path:
    """Return the primary checkout root (main-repo-root) that survives worktree cleanup.

    When this script runs from a feature worktree,
    ``git rev-parse --git-common-dir`` returns the path to the PRIMARY repo's
    ``.git`` directory (e.g. ``/path/to/primary-repo/.git``).  The primary
    checkout root is the parent of that directory.

    When running from the primary checkout itself, ``--git-common-dir`` returns
    a relative ``.git``, so we resolve it against THIS SCRIPT'S directory
    (not the process cwd, which may be a completely different repo when this
    runs as a Codex SessionStart hook).

    Falls back to the directory containing this script if git is unavailable or
    returns an unexpected value.
    """
    # Anchor git resolution to THIS script's directory, never the process cwd.
    # If process cwd is a different git repo (e.g. another Codex session working
    # directory), ``git rev-parse`` without -C would resolve THAT repo's root
    # and bake its path into the global hook — exactly the bug we're fixing.
    script_dir = Path(__file__).resolve().parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            cwd=str(script_dir),
        )
        if result.returncode == 0:
            common_dir = result.stdout.strip()
            if common_dir:
                # Resolve relative paths (e.g. ".git" from the primary checkout)
                # against THIS SCRIPT'S directory (not process cwd).
                common_dir_path = Path(common_dir)
                if not common_dir_path.is_absolute():
                    common_dir_path = script_dir / common_dir_path
                common_dir_path = common_dir_path.resolve()
                # The primary checkout root is the parent of the .git dir.
                primary_root = common_dir_path.parent
                return primary_root
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Fall back: use the directory containing this script (the current checkout).
    return script_dir.parent.parent


def _wrapper_command() -> str:
    """Return the canonical fail-open wrapper command as a STABLE absolute path.

    The path is anchored to the PRIMARY checkout root (main-repo-root) so that
    it survives worktree cleanup.  Writing this absolute path into the GLOBAL
    ~/.codex/hooks.json means:

    - The command works when Codex's cwd is a different repo (or no git repo).
    - The path remains valid after ``cleanup-merged-worktrees.sh`` deletes the
      feature worktree where the repair originally ran.
    - No ``$(git rev-parse --show-toplevel)`` expansion that depends on cwd.
    """
    primary_root = _primary_checkout_root()
    wrapper = primary_root / "scripts" / "codex-hooks" / "run_peon_ping.py"
    return f'python3 "{wrapper}"'


def _is_stale(command: str) -> bool:
    """Return True if *command* directly invokes the peon-ping adapter (codex.sh).

    This covers the direct-adapter bypass pattern in all its forms (absolute,
    tilde, $HOME, quoted).  It does NOT flag wrapper entries whose path is merely
    cwd-dependent; use ``_is_cwd_dependent_wrapper`` for that.
    """
    return bool(_STALE_ADAPTER_RE.search(command))


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
    # Already the correct absolute wrapper — leave it alone.
    if command == primary_wrapper:
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
        primary_wrapper = _wrapper_command()
    return _is_stale_absolute_wrapper(command, primary_wrapper)


def _repair_config(config: dict) -> tuple[dict, int]:
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
    replacement = _wrapper_command()

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

    repaired, count = _repair_config(config)

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
        target.write_text(new_text, encoding="utf-8")
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
