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
import shlex
import subprocess
import sys
from pathlib import Path

from agentic_pr_dash import ci_watch
from agentic_pr_dash.ci_watch import current_branch, get_pr_number, head_sha
from agentic_pr_dash.config import load as load_config
from agentic_pr_dash.codex_hooks.command_parser import (
    cd_target,
    effective_git_cwd,
    is_gh_pr_open,
    is_git_push,
    split_command_segments,
)
from agentic_pr_dash.codex_hooks.run_arm_pr_watch import (
    load_payload,
    normalized_payload,
)
from agentic_pr_dash._maintenance.markers import _read_marker, _read_session_marker
from agentic_pr_dash._maintenance.ownership_resolution import resolve_worktree
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


def find_prcreate_cwd(payload: dict) -> str | None:
    """Return the worktree of a *successful* ``gh pr create`` (or ``ready``/
    ``new``), else None.

    A PR opened in-session does not follow a ``git push`` in the *same* command
    (the push happened in an earlier tool call), so :func:`find_push_cwd` misses
    it — and the post-push waiter nudge never fires for the freshly-opened PR.
    This mirrors that segment walk for the PR-opening ``gh pr`` segment so that
    opening a PR also arms the in-session CI/feedback waiter.

    ``gh`` has no ``-C`` equivalent — it runs in the process cwd — so the
    effective cwd is whatever ``cd`` relocation precedes the segment. Success
    gating matches ``find_push_cwd``: act only when the create provably ran
    (whole command exited 0, or the create is not the trailing failed segment).
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

    create_cwd: str | None = None
    create_is_last = True
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
        if is_gh_pr_open(segment):
            create_cwd = eff_cwd
            create_is_last = True
            continue
        if create_cwd is not None:
            create_is_last = False

    if create_cwd is None:
        return None
    if failed and create_is_last:
        return None
    return create_cwd


def _pr_is_draft(pr_number: int, cwd: str) -> bool:
    """Best-effort ``isDraft`` lookup; on any failure treat as non-draft (False).

    Resolved through the shared host-global snapshot (BOU-2810) rather than its
    own ``gh pr view``: `isDraft` is already in every snapshot, so in the common
    case this costs no GraphQL at all. `resolve_pr_field` falls through to a real
    call when the PR is not in the snapshot (someone else's PR, or already
    closed), so behaviour is unchanged — only the traffic is.
    """
    from agentic_pr_dash import github_api  # noqa: PLC0415
    try:
        return bool(github_api.resolve_pr_field(pr_number, "isDraft", cwd))
    except Exception:  # noqa: BLE001 — best-effort by contract; never raise here
        return False


def _pr_is_draft_uncached(pr_number: int, cwd: str) -> bool:
    """Retained direct path (unused by default) for debugging a snapshot mismatch."""
    from agentic_pr_dash import github_api  # noqa: PLC0415
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "isDraft"],
            cwd=cwd, capture_output=True, text=True, timeout=20,
            env=github_api.automation_subprocess_env(),
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

    The nudge tells the agent to launch the session-scoped ``await`` waiter via
    ``run_in_background`` — so it must fire ONLY when that waiter will actually
    wake the session. Gates (ALL must hold):

    * **Wake channel.** The caller is the Claude interactive ``Bash`` path (raw,
      un-normalized ``tool_name == "Bash"``) AND ``PR_WATCH_NO_WAITER`` is not set.
      Codex has no background-task wake channel: in gaia it pushes via the direct
      launcher so the raw tool name is ``exec_command``; if a host instead wires
      this hook through ``codex_runtime.run_shared_hook`` (which normalizes
      ``exec_command`` → ``Bash`` before forwarding), that host sets
      ``PR_WATCH_NO_WAITER=1`` — the same opt-out the Stop-gate honors — so the
      nudge is suppressed regardless of payload normalization.
    * **Session scope.** ``pr-watch.session`` is present (non-empty): without it
      the rendered ``--session-id`` would be blank, an invalid command.
    * **Owned worktree.** This worktree's ``pr-watch.armed`` marker names this
      session — i.e. the waiter's ``_owned_worktrees_across_roots`` will actually
      include it. Otherwise the waiter would find no owned worktree and exit
      immediately, leaving CI un-watched.
    * **Live PR.** An owned, non-draft open PR exists for the pushed branch.
    * **No duplicate.** No waiter is already alive for this session.
    """
    codex_tool_names = {"exec_command", "functions.exec_command"}
    is_codex = raw_tool_name in codex_tool_names or os.environ.get("PR_WATCH_NO_WAITER") == "1"
    if raw_tool_name not in ("Bash", *codex_tool_names):
        return None
    branch = current_branch(Path(push_cwd))
    if branch in ("", "main", "master"):
        return None
    session_id = _read_session_marker(push_cwd)
    if not session_id:
        return None
    # Ownership of the pushed worktree, union across both sources (BOU-2223
    # Stage 3): a claim this session holds is enough even when the marker is
    # missing or stale, and vice versa. The session marker read above names
    # WHICH session pushed — session identity, not PR ownership.
    marker = _read_marker(push_cwd) or {}
    if marker.get("session_id") != session_id and not resolve_worktree(
        push_cwd, kind="post_push_divergence"
    ).owned_by(session_id):
        return None
    pr_number = get_pr_number(branch, Path(push_cwd))
    if pr_number is None:
        return None
    if _pr_is_draft(pr_number, push_cwd):
        return None
    if not is_codex and _await_alive(push_cwd, session_id):
        return None
    sha = head_sha(Path(push_cwd))
    if is_codex:
        monitor_python = shlex.quote(sys.executable)
        policy_path = os.environ.get("AGENTIC_PR_DASH_REVIEW_POLICY", "")
        if policy_path and not os.path.isabs(policy_path):
            policy_path = str(Path(push_cwd) / policy_path)
        if not policy_path:
            for filename in ("review-policy.yaml", "agent-review-policy.yaml"):
                candidate = Path(push_cwd) / "config" / filename
                if candidate.is_file():
                    policy_path = str(candidate)
                    break
        policy_arg = f" --policy {shlex.quote(policy_path)}" if policy_path else ""
        ledger_path = os.environ.get("AGENTIC_PR_DASH_REVIEW_LEDGER", "")
        if ledger_path and not os.path.isabs(ledger_path):
            ledger_path = str(Path(push_cwd) / ledger_path)
        if not ledger_path:
            candidate = Path(push_cwd) / ".agentic-review" / "ledger.json"
            if candidate.is_file():
                ledger_path = str(candidate)
        ledger_arg = f" --ledger {shlex.quote(ledger_path)}" if policy_path and ledger_path else ""
        return (
            f"[pr-watch] You pushed to PR #{pr_number} (HEAD {sha[:8]}). Codex has "
            "no asynchronous wake/resumption channel. Poll settlement in the "
            "FOREGROUND now until two clean observations 30 seconds apart or the "
            f"exact 30-minute bound:\n  {monitor_python} -m agentic_pr_dash.maintenance_check "
            f"monitor --cwd {shlex.quote(push_cwd)} --session-id {shlex.quote(session_id)} "
            f"--pr {pr_number} "
            "--max-wait 1800 "
            f"--poll-interval 30{policy_arg}{ledger_arg}"
        )
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

    # A ``git push`` arms the watcher against the pushed worktree; a
    # ``gh pr create`` (which never follows a push in the SAME command) arms
    # against the worktree the PR was opened from. Either should launch the
    # in-session waiter — without the pr-create branch, a PR opened after its
    # push silently never gets an in-session CI/feedback waiter.
    watch_cwd = find_push_cwd(payload) or find_prcreate_cwd(payload)
    if watch_cwd is None:
        return 0

    env = dict(os.environ)
    env["CI_WATCH_PROJECT_DIR"] = watch_cwd
    cfg = ci_watch.CIWatchConfig.from_env(env)
    ci_watch.arm_post_push_watch(cfg)

    # Proactively nudge Claude to launch its waiter, or Codex to run the bounded
    # foreground monitor (Codex has no wake channel). Advisory:
    # never block the push — any failure → no stdout, return 0.
    try:
        nudge = build_post_push_waiter_nudge(watch_cwd, payload.get("tool_name", ""))
        if nudge:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PostToolUse", "additionalContext": nudge}}))
    except Exception:  # noqa: BLE001 - advisory hook, must never block the push
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
