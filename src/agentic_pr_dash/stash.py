"""Race-safe labeled-stash operations (BOU-2155, ported from gaia BOU-2031).

``git stash`` refs are shared across EVERY worktree of a repo, and
``stash@{n}`` is a stack INDEX that shifts whenever any worktree pushes or
drops. Reading ``git stash list`` in one step and running
``git stash apply stash@{n}`` in a later step therefore races: the index you
resolved may point at someone else's entry by the time you use it. In a
multi-agent setup (this package's maintenance executors run in sibling
worktrees) a bare ``git stash pop`` grabbed a foreign session's WIP three
times in one evening.

This module closes that window with labeled-stash discipline:

* ``push`` REQUIRES a non-empty ``-m/--message`` label — anonymous stash
  entries cannot be safely re-identified on a shared stack.
* ``apply <label>`` / ``drop <label>`` REQUIRE a non-empty, non-blank label
  (an empty string — e.g. an unset ``$LABEL`` shell variable — would
  substring-match EVERY entry) and resolve the label to a
  (commit hash, stash ref) pair in ONE ``git stash list`` invocation, so the
  hash is pinned at the same instant the index is read (TOCTOU guard). The
  label is matched as a FIXED substring against the ``stash@{n}: On
  <branch>: <label>`` portion only (never the hash hex), and exactly one
  entry must match — zero or ambiguous matches fail closed.
* ``apply`` acts on the pinned COMMIT HASH (``git stash apply`` accepts a
  stash commit id directly), so it is immune to index shifts entirely.
* ``drop`` only accepts a ``stash@{n}`` reflog ref, so it (a) re-verifies
  ``git rev-parse stash@{n}`` still equals the pinned hash IMMEDIATELY
  before dropping, and (b) POST-VERIFIES the ``Dropped <ref> (<hash>)``
  output: if the hash actually dropped is not the pinned one (the stack
  shifted inside the drop window — git has no compare-and-swap drop), the
  foreign entry is RESTORED via ``git stash store`` and the command aborts
  loudly.

Residual (accepted): git offers no compare-and-swap for stash refs, so the
sub-millisecond window between the pre-drop re-verify and the drop itself
cannot be eliminated — but a wrong drop is now detected and rolled back,
so the worst case is a transiently-missing-then-restored foreign entry,
never a silently lost one. Apply is fully race-free.

Usage::

    agentic-pr-dash stash push -m "<branch>: <purpose>" [-u] [--cwd DIR]
    agentic-pr-dash stash apply "<label-substring>" [--cwd DIR]
    agentic-pr-dash stash drop "<label-substring>" [--cwd DIR]
    agentic-pr-dash stash list [--cwd DIR]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys


class StashError(Exception):
    """Fail-closed condition: zero/ambiguous label match or a detected shift."""


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    """Run a git command. Module-level so tests can monkeypatch or wrap it."""
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)


def _resolve_label(label: str, cwd: str) -> tuple[str, str, str]:
    """Resolve *label* -> ``(commit_hash, stash_ref, entry)`` in ONE list call.

    The single ``git stash list`` invocation pins the entry's commit hash at
    the same instant the index is read — the TOCTOU guard at the core of the
    ported semantics. Raises :class:`StashError` on zero or ambiguous matches.
    """
    if not label.strip():
        # An empty/blank label (e.g. an unset $LABEL shell variable) would
        # substring-match EVERY entry — and with exactly one entry on the
        # stack it would "unambiguously" resolve to someone's WIP. Fail closed
        # before touching the stack.
        raise StashError(
            "empty/blank label — refusing to resolve. An empty label would match "
            "every stash entry (is a shell variable unset?). Pass the exact label "
            "substring."
        )
    result = _git(["stash", "list", "--format=%H %gd: %gs"], cwd)
    if result.returncode != 0:
        raise StashError(f"git stash list failed: {result.stderr.strip()}")

    matches: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        # Match only against the "stash@{n}: On <branch>: <label>" portion,
        # never the hash hex, as a fixed substring (no regex semantics).
        _, _, rest = line.partition(" ")
        if label in rest:
            matches.append(line)

    if not matches:
        listing = _git(["stash", "list"], cwd).stdout
        raise StashError(
            f"no stash entry matches label {label!r}. Current stash:\n{listing.rstrip()}"
        )
    if len(matches) > 1:
        entries = "\n".join(m.partition(" ")[2] for m in matches)
        raise StashError(
            f"label {label!r} is ambiguous ({len(matches)} matches) — refine it:\n{entries}"
        )

    line = matches[0]
    stash_hash, _, entry = line.partition(" ")  # "<hash> stash@{n}: On ...: <label>"
    ref = entry.split(":", 1)[0]                # "stash@{n}"
    return stash_hash, ref, entry


def _cmd_push(message: str, include_untracked: bool, cwd: str) -> int:
    if not message.strip():
        print("stash push: -m/--message label must be non-empty", file=sys.stderr)
        return 2
    args = ["stash", "push"]
    if include_untracked:
        args.append("--include-untracked")
    args += ["-m", message]
    result = _git(args, cwd)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def _cmd_apply(label: str, cwd: str) -> int:
    stash_hash, _ref, entry = _resolve_label(label, cwd)
    # `git stash apply` accepts a stash COMMIT id directly — immune to index
    # shifts, no re-validation needed.
    print(f"stash: git stash apply {stash_hash}  ({entry})")
    result = _git(["stash", "apply", stash_hash], cwd)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def _parse_dropped_hash(stdout: str) -> str | None:
    """Extract the commit hash from git's ``Dropped <ref> (<hash>)`` output.

    Matches only the parenthesized hex tail so a localized "Dropped" prefix
    still parses. Returns ``None`` if no hash can be found — callers must
    treat that as unconfirmed and fail closed.
    """
    found = re.findall(r"\(([0-9a-f]{7,64})\)", stdout)
    return found[-1] if found else None


def _cmd_drop(label: str, cwd: str) -> int:
    stash_hash, ref, entry = _resolve_label(label, cwd)
    # `git stash drop` only accepts a stash@{n} reflog ref, so re-verify the
    # ref still points at the pinned hash immediately before dropping; abort
    # loudly if the shared stack shifted underneath us.
    current_res = _git(["rev-parse", "--verify", "--quiet", ref], cwd)
    current = current_res.stdout.strip() if current_res.returncode == 0 else ""
    if current != stash_hash:
        raise StashError(
            f"ABORT — {ref} no longer points at {stash_hash} (now: {current or '<gone>'}).\n"
            "The shared stash stack shifted (another worktree pushed/dropped) between "
            "resolution and drop. Re-run to re-resolve the label."
        )
    print(f"stash: git stash drop {ref}  ({entry})")
    result = _git(["stash", "drop", ref], cwd)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        return result.returncode
    # POST-verify: git has no compare-and-swap drop, so the stack can still
    # shift between the re-verify above and the drop itself. `git stash drop`
    # prints the commit hash it actually removed — confirm it is the pinned
    # one, and if a foreign entry was dropped instead, restore it and abort.
    dropped = _parse_dropped_hash(result.stdout)
    if dropped is None:
        raise StashError(
            f"drop succeeded but its output did not confirm which entry was removed "
            f"(expected 'Dropped {ref} (<hash>)'). Inspect `git stash list` manually — "
            f"the pinned target was {stash_hash}."
        )
    if not stash_hash.startswith(dropped):
        subject_res = _git(["log", "-1", "--format=%s", dropped], cwd)
        subject = subject_res.stdout.strip() or f"restored foreign stash {dropped[:12]}"
        restore = _git(["stash", "store", "-m", subject, dropped], cwd)
        if restore.returncode == 0:
            rollback = f"The foreign entry was RESTORED via `git stash store` ({subject!r})."
        else:
            rollback = (
                f"ROLLBACK FAILED ({restore.stderr.strip() or 'unknown error'}) — recover it "
                f"manually: git stash store -m {subject!r} {dropped}"
            )
        raise StashError(
            f"ABORT — the drop removed {dropped}, not the pinned target {stash_hash} "
            f"(the shared stack shifted inside the drop window). {rollback}\n"
            f"The target entry ({entry}) was NOT dropped — re-run to re-resolve the label."
        )
    return 0


def _cmd_list(cwd: str) -> int:
    result = _git(["stash", "list"], cwd)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentic-pr-dash stash",
        description="Race-safe labeled-stash operations for shared (cross-worktree) stash stacks.",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_push = sub.add_parser("push", help="Stash changes under a REQUIRED label")
    p_push.add_argument("-m", "--message", required=True, help="Non-empty stash label (required)")
    p_push.add_argument(
        "-u", "--include-untracked", action="store_true", help="Also stash untracked files"
    )
    p_push.add_argument("--cwd", default=".", help="Repo/worktree directory (default: .)")

    p_apply = sub.add_parser("apply", help="Apply the single entry matching a label, by commit hash")
    p_apply.add_argument("label", help="Label substring; must match exactly one entry")
    p_apply.add_argument("--cwd", default=".", help="Repo/worktree directory (default: .)")

    p_drop = sub.add_parser("drop", help="Drop the single entry matching a label (re-verified)")
    p_drop.add_argument("label", help="Label substring; must match exactly one entry")
    p_drop.add_argument("--cwd", default=".", help="Repo/worktree directory (default: .)")

    p_list = sub.add_parser("list", help="List stash entries (read-only)")
    p_list.add_argument("--cwd", default=".", help="Repo/worktree directory (default: .)")

    args = parser.parse_args(argv)

    try:
        if args.action == "push":
            return _cmd_push(args.message, args.include_untracked, args.cwd)
        if args.action == "apply":
            return _cmd_apply(args.label, args.cwd)
        if args.action == "drop":
            return _cmd_drop(args.label, args.cwd)
        return _cmd_list(args.cwd)
    except StashError as exc:
        print(f"stash: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
