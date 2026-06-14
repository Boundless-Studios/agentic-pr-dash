"""Codex/Claude hook adapter: arm the post-push CI watcher.

Generic behavior: parse a hook payload (Claude ``Bash`` or Codex
``exec_command``), detect a ``git push``, and arm the background CI watcher
(:mod:`agentic_pr_dash.ci_watch`) against the worktree the push ran in. Both
runtimes route through this one module — gaia keeps no local copy.

The watcher itself is advisory and detached: this hook does a fast CI snapshot,
spawns the background poller, and returns 0 immediately so the push/turn is
never blocked. Project-specific surfaces (gaia's beads gate-bead, iTerm status,
results-file location) are supplied as configuration via :class:`CIWatchConfig`
(env-driven), not hard-coded here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from agentic_pr_dash import ci_watch
from agentic_pr_dash.codex_hooks.run_arm_pr_watch import (
    cd_target,
    effective_git_cwd,
    is_git_push,
    load_payload,
    normalized_payload,
    split_command_segments,
)


def _command_failed(payload: dict) -> bool:
    response = payload.get("tool_response")
    candidates = []
    if isinstance(response, dict):
        for key in ("exit_code", "exitCode", "returncode", "code", "status"):
            if key in response:
                candidates.append(response.get(key))
    for key in ("exit_code", "exitCode", "returncode"):
        if key in payload:
            candidates.append(payload.get(key))
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value != 0
        if isinstance(value, str) and value.lstrip("-").isdigit():
            return int(value) != 0
    return False


def _normalized_cwd(payload: dict) -> str:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return os.path.abspath(cwd)
    return os.getcwd()


def find_push_cwd(payload: dict) -> str | None:
    """Return the effective worktree of a ``git push`` in this payload, else None.

    Walks compound command segments (honoring ``cd`` relocation and ``&&``/``||``
    guards) exactly like the arming hook so ``cd ../wt && git push`` and
    ``git -C nested push`` resolve to the directory the push actually targeted.
    A push that the shell never ran (failed ``&&`` guard, ``||`` guard) returns
    None — there's nothing new to watch.
    """
    normalized = normalized_payload(payload)
    if normalized.get("tool_name") != "Bash":
        return None
    tool_input = normalized.get("tool_input") if isinstance(normalized.get("tool_input"), dict) else {}
    command = tool_input.get("command", "")
    if not isinstance(command, str) or not command:
        return None

    failed = _command_failed(payload)
    eff_cwd = _normalized_cwd(normalized)
    for op, segment in split_command_segments(command):
        destination = cd_target(segment)
        if destination is not None:
            destination = os.path.expanduser(destination)
            eff_cwd = (
                destination
                if os.path.isabs(destination)
                else str((Path(eff_cwd) / destination).resolve())
            )
            continue
        if op == "||":
            continue
        if op == "&&" and failed:
            continue
        if is_git_push(segment):
            return effective_git_cwd(segment, eff_cwd)
    return None


def main() -> int:
    payload = load_payload()
    phase = sys.argv[1] if len(sys.argv) > 1 else payload.get("hook_event_name", "")
    if phase and phase != "PostToolUse":
        return 0

    push_cwd = find_push_cwd(payload)
    if push_cwd is None:
        return 0

    env = dict(os.environ)
    env["CI_WATCH_PROJECT_DIR"] = push_cwd
    cfg = ci_watch.CIWatchConfig.from_env(env)
    return ci_watch.arm_post_push_watch(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
