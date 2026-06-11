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
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .config import load as load_config

CHECK_WORK_FOUND = 10


def _discover_cwds(args) -> list[str]:
    """Worktrees to service this tick."""
    if args.no_discover_worktrees:
        return list(args.cwd)
    if args.session_id:
        # Scope to worktrees this session owns.
        out = subprocess.run(
            [sys.executable, "-m", "agentic_pr_dash", "list-owned",
             "--session-id", args.session_id, "--pid", str(_loop_pid())],
            capture_output=True, text=True,
        )
        paths = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
        # Distinguish "owns nothing this tick" (rc 0, empty) from "discovery
        # failed" (non-zero rc). An EMPTY owned set is authoritative — service
        # nothing — and must NOT fall back to args.cwd: when the live-owner gate
        # legitimately excludes the only candidate, falling back would route the
        # loop to service a foreign worktree by cwd (BOU-1540, PR #7 review, P1).
        # Only a genuine command failure falls back to the explicit --cwd values.
        if out.returncode != 0:
            return list(args.cwd)
        return paths
    # Every worktree on the machine.
    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=args.cwd[0],
        capture_output=True, text=True,
    )
    paths = [ln.split(" ", 1)[1].strip() for ln in out.stdout.splitlines() if ln.startswith("worktree ")]
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


def _tick(args, executor: str) -> None:
    for cwd in _discover_cwds(args):
        if not Path(cwd).is_dir():
            continue
        check = subprocess.run(
            [sys.executable, "-m", "agentic_pr_dash", "check",
             "--cwd", cwd, "--session-id", args.session_id or ""],
            capture_output=True, text=True,
        )
        if check.returncode != CHECK_WORK_FOUND:
            continue  # 0 = clean/deferred, 2 = gh unavailable
        pr = _parse_pr_number(check.stdout)
        prompt = check.stdout
        baseline = _baseline_sha(cwd, pr)
        print(f"[agentic-pr-dash] PR #{pr} in {cwd} needs work — dispatching executor", file=sys.stderr)
        rc = _run_executor(executor, prompt, cwd)
        if rc != 0:
            print(f"[agentic-pr-dash] executor exited {rc}; leaving PR #{pr} for next tick", file=sys.stderr)
            continue
        complete_args = [sys.executable, "-m", "agentic_pr_dash", "complete", "--cwd", cwd, "--baseline", baseline]
        if pr is not None:
            complete_args += ["--pr", str(pr)]
        subprocess.run(complete_args)


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser(prog="agentic-pr-dash loop", description=__doc__)
    parser.add_argument("--interval", type=int, default=600, help="Seconds between ticks (default 600).")
    parser.add_argument("--cwd", action="append", default=None, help="Worktree root (repeatable; default '.').")
    parser.add_argument("--session-id", default="", help="Scope discovery to worktrees this session owns.")
    parser.add_argument("--no-discover-worktrees", action="store_true", help="Use only --cwd values; don't enumerate worktrees.")
    parser.add_argument("--once", action="store_true", help="Run a single tick and exit.")
    parser.add_argument("--executor", default=None, help="Override the configured executor command.")
    args = parser.parse_args(argv)
    args.cwd = args.cwd or ["."]

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

    if args.once:
        _tick(args, executor)
        return 0
    while True:
        _tick(args, executor)
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
