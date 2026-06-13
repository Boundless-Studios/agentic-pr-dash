"""Codex hook adapter for PR-watch ownership arming.

The generic hook behavior is: parse a Codex hook payload, identify events that
should register PR maintenance ownership, and delegate the actual marker write
to ``agentic_pr_dash maintenance_check arm``. Repo-local shims may keep policy
settings and fallbacks, but the marker-writing source of truth lives upstream.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from agentic_pr_dash import maintenance_check

_TRUE_VALUES = {"1", "true", "TRUE", "True", "yes", "YES", "on", "ON"}


def load_payload() -> dict:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - hooks are advisory
        return {}
    return payload if isinstance(payload, dict) else {}


def normalized_payload(payload: dict) -> dict:
    """Map Codex ``exec_command`` payloads to the Bash shape Claude hooks use."""
    tool_name = payload.get("tool_name")
    if tool_name in {"exec_command", "functions.exec_command"}:
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        command = tool_input.get("cmd", "")
        normalized = dict(payload)
        normalized["tool_name"] = "Bash"
        normalized["tool_input"] = {"command": command}
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


def _skip_command_prefixes(tokens: list[str]) -> int:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "env":
            index += 1
            while index < len(tokens) and "=" in tokens[index]:
                index += 1
            continue
        if "=" in token:
            index += 1
            continue
        if Path(token).name in {"command", "builtin"}:
            index += 1
            continue
        break
    return index


def is_gh_pr_open(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False

    index = _skip_command_prefixes(tokens)
    if index >= len(tokens):
        return False
    token = tokens[index]
    if token != "gh" and not token.endswith("/gh"):
        return False
    index += 1

    value_flags = {"-R", "--repo"}
    while index < len(tokens) and tokens[index].startswith("-"):
        index += 2 if tokens[index] in value_flags else 1

    if index >= len(tokens) or tokens[index] != "pr":
        return False
    index += 1
    while index < len(tokens):
        token = tokens[index]
        if token in value_flags:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token in {"create", "ready", "new"}
    return False


def is_git_push(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False

    index = _skip_command_prefixes(tokens)
    if index >= len(tokens):
        return False
    token = tokens[index]
    if token != "git" and not token.endswith("/git"):
        return False

    index += 1
    option_value_flags = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
    while index < len(tokens):
        token = tokens[index]
        if token == "push":
            return True
        if token.startswith("-"):
            option_name = token.split("=", 1)[0]
            if option_name in option_value_flags:
                index += 2 if "=" not in token else 1
                continue
            if token.startswith("-C") and token != "-C":
                index += 1
                continue
            if token.startswith("-c") and token != "-c":
                index += 1
                continue
            if (
                token.startswith("--git-dir=")
                or token.startswith("--work-tree=")
                or token.startswith("--namespace=")
            ):
                index += 1
                continue
            return False
        return False
    return False


def effective_git_cwd(command: str, base_cwd: str) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return base_cwd

    index = _skip_command_prefixes(tokens)
    env_work_tree = None
    for prefix in tokens[:index]:
        if prefix.startswith("GIT_WORK_TREE="):
            env_work_tree = prefix.split("=", 1)[1]

    if index >= len(tokens):
        return base_cwd
    token = tokens[index]
    if token != "git" and not token.endswith("/git"):
        return base_cwd
    index += 1

    cwd = base_cwd
    work_tree = env_work_tree
    while index < len(tokens):
        token = tokens[index]
        if token == "push":
            break
        if token == "-C" and index + 1 < len(tokens):
            cwd = str((Path(cwd) / tokens[index + 1]).resolve())
            index += 2
            continue
        if token.startswith("-C") and token != "-C":
            cwd = str((Path(cwd) / token[2:]).resolve())
            index += 1
            continue
        if token == "--work-tree" and index + 1 < len(tokens):
            work_tree = tokens[index + 1]
            index += 2
            continue
        if token.startswith("--work-tree="):
            work_tree = token[len("--work-tree=") :]
            index += 1
            continue
        index += 1

    if work_tree is not None:
        return str((Path(cwd) / work_tree).resolve())
    return cwd


def _arm(cwd: str, session_id: str) -> None:
    result = maintenance_check.main(
        [
            "arm",
            "--cwd",
            cwd,
            "--session-id",
            session_id,
            "--pid",
            str(os.getppid()),
        ]
    )
    if result != 0:
        print(f"run_arm_pr_watch.py: arm returned {result}; continuing", file=sys.stderr)


def main() -> int:
    payload = load_payload()
    phase = sys.argv[1] if len(sys.argv) > 1 else payload.get("hook_event_name", "")
    session_id = session_id_from_payload(payload)
    if not session_id:
        return 0

    cwd = normalized_cwd(payload)
    if phase == "SessionStart":
        if pr_watch_autoloop_enabled():
            _arm(cwd, session_id)
        return 0

    if phase != "PostToolUse":
        return 0

    normalized = normalized_payload(payload)
    if normalized.get("tool_name") != "Bash":
        return 0
    tool_input = normalized.get("tool_input") if isinstance(normalized.get("tool_input"), dict) else {}
    command = tool_input.get("command", "")
    if not isinstance(command, str):
        return 0

    if is_gh_pr_open(command):
        _arm(cwd, session_id)
    elif is_git_push(command) and pr_watch_autoloop_enabled():
        _arm(effective_git_cwd(command, cwd), session_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
