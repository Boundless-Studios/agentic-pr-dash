"""Upstream codex hook: generic Bash pre-execution policy engine.

Applies repo-context-aware Bash policy checks before a command executes:

1. Generic AGENTS.md sibling-search guard (always active, repo-agnostic).
2. A caller-injected roster of shared bash hooks (run for every Bash command).
3. A caller-injected roster of commit-only hooks (run only for ``git commit``).

**BOU-1575 repo-context fix**: the engine carries NO gaia-specific hook paths.
The gaia shim injects its hook roster via environment variables or the ``run()``
Python API.  In a non-gaia checkout (no roster injected) only the generic guard
fires — ``pytest tests/`` is allowed and no gaia policy scripts are invoked.

Environment-variable API (for standalone hook use):
  ``AGENTIC_PR_DASH_BASH_POLICY_HOOK_BASE_DIR``
      Root directory from which hook paths are resolved.
      Alias: ``CODEX_BASH_POLICY_HOOK_BASE_DIR`` (back-compat).
  ``AGENTIC_PR_DASH_BASH_POLICY_SHARED_HOOKS``
      JSON array of ``[name, relpath]`` pairs for shared (every-command) hooks.
  ``AGENTIC_PR_DASH_BASH_POLICY_COMMIT_HOOKS``
      JSON array of ``[name, relpath]`` pairs for commit-only hooks.

Python API (for gaia shim / programmatic use):
  ``run(shared_hooks, commit_hooks, *, base_dir, behavior_enabled=...)``
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from agentic_pr_dash.codex_hooks._payload import (
    behavior_enabled as _default_behavior_enabled,
    load_payload,
    normalized_payload,
)


_PARENT_TREE_AGENTS_SEARCH_RE = re.compile(
    r"""
    ^\s*
    find\s+(?:\.\./?|['"]\.\./?['"])
    (?:\s+(?![;&|])\S+)*
    \s+-name\s+['"]?AGENTS\.md['"]?
    (?:\s+(?![;&|])\S+)*
    \s*$
    """,
    re.VERBOSE,
)


def hook_base_dir() -> Path:
    """Return the base directory for resolving hook script paths.

    Reads ``AGENTIC_PR_DASH_BASH_POLICY_HOOK_BASE_DIR`` (preferred) or the
    legacy alias ``CODEX_BASH_POLICY_HOOK_BASE_DIR``, falling back to cwd.
    """
    env_val = (
        os.environ.get("AGENTIC_PR_DASH_BASH_POLICY_HOOK_BASE_DIR", "").strip()
        or os.environ.get("CODEX_BASH_POLICY_HOOK_BASE_DIR", "").strip()
    )
    if env_val:
        return Path(env_val)
    return Path(os.getcwd())


def _emit_codex_block(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def _block_reason_for_command(command: object) -> str | None:
    """Return a block reason for generic (repo-agnostic) policy violations.

    Currently checks:
    - ``find .. -name AGENTS.md`` sibling-repo searches.

    Returns ``None`` to allow.
    """
    if not isinstance(command, str):
        return None

    if _PARENT_TREE_AGENTS_SEARCH_RE.match(command):
        return (
            "`find .. -name AGENTS.md` searches sibling repositories from shared code roots. "
            "Use `test -f AGENTS.md` in the current checkout, one bounded parent check when "
            "needed, or a path-scoped search under the current repository instead."
        )

    return None


def run_script(path: str, payload_text: str, *, base: Path | None = None) -> int:
    """Run a hook script ``path`` (relative to *base*) with *payload_text* on stdin.

    Prints the script's stdout/stderr to the current process's stdout/stderr so
    any ``{"decision":"block",...}`` emitted by the script reaches the hook harness.
    """
    resolved_base = base if base is not None else hook_base_dir()
    result = subprocess.run(
        [sys.executable, str(resolved_base / path)],
        input=payload_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def is_git_commit_command(command: str) -> bool:
    """Return True iff *command* is a ``git commit`` invocation.

    Handles leading ``env``/``KEY=VAL`` prefixes and ``git`` global option flags
    (``-C``, ``-c``, ``--git-dir``, ``--work-tree``, ``--namespace``) before the
    subcommand position.  Returns False for unparseable commands.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False

    index = 0
    while index < len(tokens) and (
        tokens[index] == "env" or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[index])
    ):
        index += 1
        if tokens[index - 1] == "env":
            while index < len(tokens) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[index]):
                index += 1

    if index >= len(tokens):
        return False

    token = tokens[index]
    if token != "git" and not token.endswith("/git"):
        return False

    index += 1
    option_value_flags = {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
    }
    while index < len(tokens):
        token = tokens[index]
        if token == "commit":
            return True
        if token.startswith("-"):
            if token.split("=", 1)[0] in option_value_flags:
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


def _load_hooks_from_env(env_key: str) -> list[tuple[str, str]]:
    """Parse a JSON ``[[name, relpath], ...]`` roster from an env var."""
    raw = os.environ.get(env_key, "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    result = []
    for item in parsed:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            name, path = str(item[0]), str(item[1])
            result.append((name, path))
    return result


def run(
    shared_hooks: list[tuple[str, str]],
    commit_hooks: list[tuple[str, str]],
    *,
    base_dir: Path,
    behavior_enabled: Callable[[str], bool] = _default_behavior_enabled,
    payload_text: str,
    command: str,
) -> int:
    """Apply policy hooks and return 0 (allow) or the first non-zero exit code (block).

    Args:
        shared_hooks:     ``[(name, relpath), ...]`` run for every Bash command.
        commit_hooks:     ``[(name, relpath), ...]`` run only for ``git commit``.
        base_dir:         Directory from which *relpath* entries are resolved.
        behavior_enabled: Predicate ``(name) -> bool``; hooks whose name returns
                          False are skipped.  Defaults to env-var-based predicate.
        payload_text:     Serialised hook payload passed to each script on stdin.
        command:          Extracted Bash command string (for commit detection).
    """
    for hook_name, hook_path in shared_hooks:
        if not behavior_enabled(hook_name):
            continue
        rc = run_script(hook_path, payload_text, base=base_dir)
        if rc != 0:
            return rc

    if is_git_commit_command(command):
        for hook_name, hook_path in commit_hooks:
            if not behavior_enabled(hook_name):
                continue
            rc = run_script(hook_path, payload_text, base=base_dir)
            if rc != 0:
                return rc

    return 0


def main() -> int:
    """Standalone entry-point: reads roster from env vars, payload from stdin."""
    payload = load_payload()
    normalized = normalized_payload(payload)
    # apply_shared_env omitted upstream (gaia-specific env vars).

    if normalized["tool_name"] != "Bash":
        return 0

    payload_text = json.dumps(normalized)
    command = normalized["tool_input"].get("command", "")

    # Generic repo-agnostic guard (always active)
    block_reason = _block_reason_for_command(command)
    if block_reason is not None:
        return _emit_codex_block(block_reason)

    # Roster injection via env vars (BOU-1575: empty by default → only generic
    # guard fires in non-gaia checkouts)
    shared_hooks = _load_hooks_from_env("AGENTIC_PR_DASH_BASH_POLICY_SHARED_HOOKS")
    commit_hooks = _load_hooks_from_env("AGENTIC_PR_DASH_BASH_POLICY_COMMIT_HOOKS")
    base = hook_base_dir()

    return run(
        shared_hooks,
        commit_hooks,
        base_dir=base,
        payload_text=payload_text,
        command=command,
    )


if __name__ == "__main__":
    raise SystemExit(main())
