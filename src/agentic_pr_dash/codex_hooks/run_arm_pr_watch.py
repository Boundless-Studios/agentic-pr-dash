"""Codex hook adapter for PR-watch ownership arming.

The generic hook behavior is: parse a Codex hook payload, identify events that
should register PR maintenance ownership, and delegate the actual marker write
to ``agentic_pr_dash maintenance_check arm``. Repo-local shims may keep policy
settings and fallbacks, but the marker-writing source of truth lives upstream.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from agentic_pr_dash import maintenance_check
from agentic_pr_dash.config import load as load_config

_TRUE_VALUES = {"1", "true", "TRUE", "True", "yes", "YES", "on", "ON"}


def load_payload() -> dict:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - hooks are advisory
        return {}
    return payload if isinstance(payload, dict) else {}


def normalized_payload(payload: dict) -> dict:
    """Map Codex ``exec_command`` payloads to the Bash shape Claude hooks use.

    A Codex ``exec_command`` may carry its own ``tool_input.workdir`` — the
    directory the command actually ran in (e.g. ``gh pr create`` invoked against
    a sibling worktree). That executed workdir, when present, is the correct cwd
    to arm: the top-level payload ``cwd`` / hook process cwd can point at a
    different worktree and make ``maintenance_check arm`` resolve the wrong (or
    no) PR. Surface it as the normalized ``cwd`` so the workdir wins.
    """
    tool_name = payload.get("tool_name")
    if tool_name in {"exec_command", "functions.exec_command"}:
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        command = tool_input.get("cmd", "")
        normalized = dict(payload)
        normalized["tool_name"] = "Bash"
        normalized["tool_input"] = {"command": command}
        workdir = tool_input.get("workdir")
        if isinstance(workdir, str) and workdir:
            normalized["cwd"] = workdir
        return normalized
    return payload


def normalized_cwd(payload: dict) -> str:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return os.path.abspath(cwd)
    return os.getcwd()


def session_id_from_payload(payload: dict) -> str:
    raw = payload.get("session_id")
    if isinstance(raw, str) and raw:
        return raw
    return os.environ.get("CODEX_SESSION_ID") or os.environ.get("GAIA_SESSION_ID") or ""


def _strip_optional_quotes(value: str) -> str:
    return value.strip().strip('"').strip("'")


def _configured_autoloop_flag() -> str:
    flag = os.environ.get("AGENTIC_PR_DASH_PR_WATCH_AUTOLOOP") or os.environ.get("GAIA_PR_WATCH_AUTOLOOP", "")
    if flag:
        return flag

    conf = os.environ.get("WORKTREE_CONSOLE_CONFIG") or str(Path.home() / ".config" / "gaia" / "worktree-console.conf")
    try:
        lines = Path(conf).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        key, sep, value = line.partition("=")
        if sep and key.strip() == "WC_PR_WATCH_AUTOLOOP":
            return _strip_optional_quotes(value)
    return ""


def pr_watch_autoloop_enabled() -> bool:
    return _configured_autoloop_flag() in _TRUE_VALUES


# Command parsing lives in command_parser.py; re-exported here so existing
# importers (run_post_push_watch, tests) and this module's ``main`` keep
# resolving these names from ``run_arm_pr_watch``. Absolute import (matching the
# module-top imports) so the hook still works when invoked as a script path
# (``python src/agentic_pr_dash/codex_hooks/run_arm_pr_watch.py``), where
# ``__package__`` is unset and a relative import would raise ImportError.
from agentic_pr_dash.codex_hooks.command_parser import (  # noqa: E402
    _names_explicit_repo,
    _skip_command_prefixes,
    cd_target,
    effective_git_cwd,
    is_gh_pr_open,
    is_git_push,
    parse_gh_pr_arm_target,
    split_command_segments,
)


def _command_failed(payload: dict) -> bool:
    """Best-effort: True only when the tool reports a non-zero exit code.

    A PostToolUse fires after the command ran; the exit code (when the harness
    provides it) tells us whether a ``&&``-guarded later segment actually
    executed. Absent/unparseable exit info → ``False`` (assume it ran), so the
    common ``git push && gh pr create`` still arms.
    """
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


def _arm(cwd: str, session_id: str, *, pr: str | None = None, branch: str | None = None) -> None:
    # Deliberately omit --pid: when this hook runs through a repo-local shell
    # shim, os.getppid() is the short-lived shell that exits the instant the
    # hook returns, so a marker stamped with it looks dead immediately and a
    # sibling/detached loop can adopt the same PR. Letting `arm` resolve the pid
    # walks the ancestry to the durable claude/codex session instead.
    argv = ["arm", "--cwd", cwd, "--session-id", session_id]
    if pr is not None:
        argv += ["--pr", str(pr)]
    elif branch is not None:
        argv += ["--branch", branch]
    result = maintenance_check.main(argv)
    if result != 0:
        print(f"run_arm_pr_watch.py: arm returned {result}; continuing", file=sys.stderr)


def _write_session_marker(cwd: str, session_id: str) -> None:
    """Stamp the worktree's owning-session self-id at SessionStart (BOU-1442).

    Written UNCONDITIONALLY — independent of the pr-watch opt-in — because it is
    just "who launched here" (no PR side effects): `arm`/`list-owned` and
    sub-agent PR registration read it to learn the parent session id. The
    opt-in still gates the `.armed` marker below.
    """
    if not session_id:
        return
    try:
        marker = load_config(cwd).session_marker_for(cwd)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{session_id}\n", encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    payload = load_payload()
    phase = sys.argv[1] if len(sys.argv) > 1 else payload.get("hook_event_name", "")
    session_id = session_id_from_payload(payload)
    if not session_id:
        return 0

    if phase == "SessionStart":
        cwd = normalized_cwd(payload)
        _write_session_marker(cwd, session_id)
        if pr_watch_autoloop_enabled():
            _arm(cwd, session_id)
        return 0

    if phase != "PostToolUse":
        return 0

    # Normalize first so a Codex exec_command's own workdir wins over the
    # top-level/hook-process cwd when choosing where to arm.
    normalized = normalized_payload(payload)
    if normalized.get("tool_name") != "Bash":
        return 0
    base_cwd = normalized_cwd(normalized)
    tool_input = normalized.get("tool_input") if isinstance(normalized.get("tool_input"), dict) else {}
    command = tool_input.get("command", "")
    if not isinstance(command, str):
        return 0

    # Walk each top-level segment so a `gh pr ...` after a separator (e.g.
    # `git push && gh pr create`) is still detected, and a preceding `cd`
    # relocates the effective arm cwd. Honor shell conditionals: a `||`-guarded
    # segment only runs on the previous command's failure, and a `&&`-guarded
    # one only on success — don't record ownership for a PR action the shell
    # never executed.
    autoloop = pr_watch_autoloop_enabled()
    failed = _command_failed(payload)
    eff_cwd = base_cwd
    for op, segment in split_command_segments(command):
        destination = cd_target(segment)
        if destination is not None:
            # Expand ~ / ~user first: `cd ~/wt` runs gh from the home-relative
            # worktree, not `<old cwd>/~/wt`.
            destination = os.path.expanduser(destination)
            eff_cwd = (
                destination
                if os.path.isabs(destination)
                else str((Path(eff_cwd) / destination).resolve())
            )
            continue
        if op == "||":
            continue  # only-on-failure guard: the PR action wasn't intended
        if op == "&&" and failed:
            continue  # prior command failed → this segment never ran
        target = parse_gh_pr_arm_target(segment)
        if target is not None:
            pr_number, branch = target
            _arm(eff_cwd, session_id, pr=pr_number, branch=branch)
            continue
        if autoloop and is_git_push(segment):
            _arm(effective_git_cwd(segment, eff_cwd), session_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
