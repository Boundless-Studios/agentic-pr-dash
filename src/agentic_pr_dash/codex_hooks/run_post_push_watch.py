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

import json
import os
import subprocess
import sys
from pathlib import Path

from agentic_pr_dash import ci_watch
from agentic_pr_dash.ci_watch import current_branch, get_pr_number, head_sha
from agentic_pr_dash.config import load as load_config
from agentic_pr_dash.codex_hooks.command_parser import (
    cd_target,
    effective_git_cwd,
    is_git_push,
    split_command_segments,
)
from agentic_pr_dash.codex_hooks.run_arm_pr_watch import (
    load_payload,
    normalized_payload,
)
from agentic_pr_dash._maintenance.markers import _read_session_marker
from agentic_pr_dash._maintenance.waiter import _await_alive


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
    """Return the effective worktree of a *successful* ``git push``, else None.

    Walks compound command segments (honoring ``cd`` relocation and ``&&``/``||``
    guards) like the arming hook so ``cd ../wt && git push`` and
    ``git -C nested push`` resolve to the directory the push targeted.

    Unlike the (idempotent, low-harm) arm hook, this one kills a live watcher and
    starts polling the pushed SHA, so a false arm on a push that never landed
    replaces a valid watch with a bogus timeout. We therefore arm only when the
    push provably *succeeded*:

    * The whole command exited 0 → every executed segment, including the push,
      succeeded.
    * The command failed but the push is **not** the last executed segment
      (e.g. ``git push && gh pr create`` where ``gh pr create`` failed): a failed
      push would have short-circuited the ``&&`` chain, so the push must have
      landed. The trailing failed segment carries the non-zero exit, not the push.

    A lone ``git push`` (or one that is the final segment) that exits non-zero is
    a rejected/non-fast-forward push → return None; there is nothing new to
    watch. ``||``-guarded pushes (failure fallbacks) are likewise skipped.
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

    # Resolve each segment's effective cwd, recording the LAST git-push segment
    # and whether any executable (non-cd) segment follows it.
    push_cwd: str | None = None
    push_is_last = True
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
            # Failure-fallback segment: it runs only when the prior command
            # failed, so it neither is a push we watch nor proves a prior push
            # succeeded. Ignore it entirely.
            continue
        if is_git_push(segment):
            push_cwd = effective_git_cwd(segment, eff_cwd)
            push_is_last = True
            continue
        # A non-cd, non-``||`` segment that runs after a push proves the push did
        # not end the chain (a failed push short-circuits the ``&&`` that guards
        # this segment), so the push must have succeeded.
        if push_cwd is not None:
            push_is_last = False

    if push_cwd is None:
        return None
    if failed and push_is_last:
        return None
    return push_cwd


def _pr_is_draft(pr_number: int, cwd: str) -> bool:
    """Best-effort ``isDraft`` lookup; on any failure treat as non-draft (False)."""
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "isDraft"],
            cwd=cwd, capture_output=True, text=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if r.returncode != 0:
        return False
    try:
        return bool(json.loads(r.stdout or "{}").get("isDraft"))
    except json.JSONDecodeError:
        return False


def build_post_push_waiter_nudge(push_cwd: str, raw_tool_name: str) -> str | None:
    """Return the ``additionalContext`` nudge text, or None when no nudge should fire.

    Gates (ALL must hold): the caller has a background-task wake channel (raw
    ``tool_name == "Bash"`` — the Claude interactive path; Codex ``exec_command``
    has none and already runs ``--no-waiter``); an owned, non-draft open PR exists
    for the pushed branch; and no waiter is already alive for this session.
    """
    if raw_tool_name != "Bash":
        return None
    branch = current_branch(Path(push_cwd))
    if branch in ("", "main", "master"):
        return None
    pr_number = get_pr_number(branch, Path(push_cwd))
    if pr_number is None:
        return None
    if _pr_is_draft(pr_number, push_cwd):
        return None
    session_id = _read_session_marker(push_cwd)
    if _await_alive(push_cwd, session_id):
        return None
    sha = head_sha(Path(push_cwd))
    await_cmd = load_config(push_cwd).await_command.format(
        cwd=push_cwd, session_id=session_id
    )
    return (
        f"[pr-watch] You pushed to PR #{pr_number} (HEAD {sha[:8]}). Launch the "
        f"CI/feedback waiter in the BACKGROUND now (run_in_background / detached) so "
        f"a red check wakes this session, then continue:\n  {await_cmd}"
    )


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
    ci_watch.arm_post_push_watch(cfg)

    # Proactively nudge the agent to launch the exit-10 await waiter in the
    # background so a red check wakes the session this turn (BOU-1786). Advisory:
    # never block the push — any failure → no stdout, return 0.
    try:
        nudge = build_post_push_waiter_nudge(push_cwd, payload.get("tool_name", ""))
        if nudge:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PostToolUse", "additionalContext": nudge}}))
    except Exception:  # noqa: BLE001 - advisory hook, must never block the push
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
