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


# The leading path may be ``..``, ``../``, ``../..``, ``../../foo`` etc. —
# any path that starts by walking into the parent tree. We match a leading
# ``..`` segment optionally followed by ``/``-separated continuation so deeper
# parent walks (``find ../.. -name AGENTS.md``) are caught too, not just ``..``.
#
# ``find`` accepts global options (``-H``/``-L``/``-P``/``-Olevel``/``-D...``)
# *before* the path operands (``find -L ../.. -name AGENTS.md``), so we allow a
# run of those options between ``find`` and the parent path — otherwise an
# option prefix would slip the parent-tree search past the guard.
_PARENT_TREE_AGENTS_SEARCH_RE = re.compile(
    r"""
    ^\s*
    find\s+
    (?:                              # optional pre-path find global options
        (?:-[HLP]+|-O\S*|-D\S+)\s+
    )*
    (?:
        \.\.(?:/[^\s;&|]*)*          # ..  ../  ../..  ../../foo
        |
        ['"]\.\.(?:/[^\s;&|]*)*['"]  # quoted forms of the above
    )
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

    # Scan each top-level shell segment: a parent-tree AGENTS.md search chained
    # after a benign prefix (``cd . && find ../.. -name AGENTS.md`` or
    # ``echo ok; find .. -name AGENTS.md``) still executes, so the anchored guard
    # must run against each segment, not just the whole raw command.
    for segment in _split_top_level_segments(command):
        if _PARENT_TREE_AGENTS_SEARCH_RE.match(segment):
            return (
                "`find .. -name AGENTS.md` searches sibling repositories from shared code roots. "
                "Use `test -f AGENTS.md` in the current checkout, one bounded parent check when "
                "needed, or a path-scoped search under the current repository instead."
            )

    return None


# Sentinel exit code used when a hook signals a block via its stdout JSON
# (``{"decision":"block",...}`` or ``hookSpecificOutput.permissionDecision:"deny"``)
# while still exiting 0 — see ``_stdout_is_block``. The Codex contract treats a
# *stdout-JSON* block and an *exit-2/stderr* block as two distinct paths: a JSON
# denial on stdout is parsed for its reason, while exit 2 is handled as a failed/
# stderr-style hook. We must therefore keep the stdout-JSON block on the exit-0
# path (the child already printed valid JSON to stdout) and only stop invoking
# later hooks, rather than rewriting it to exit 2 (which would print JSON to
# stdout *and* exit non-zero and corrupt the contract). This negative sentinel
# never collides with a real child exit code; ``run()`` maps it back to 0.
_STDOUT_BLOCK_RC = -1000


def _stdout_is_block(stdout: str) -> bool:
    """Return True iff *stdout* is a Codex block payload.

    Recognises both the legacy top-level ``{"decision":"block",...}`` shape and
    the current ``hookSpecificOutput.permissionDecision:"deny"`` shape — either
    one is a final deny that must short-circuit the remaining hooks.
    """
    trimmed = stdout.strip()
    if not trimmed:
        return False
    try:
        payload = json.loads(trimmed)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("decision") == "block":
        return True
    hook_specific = payload.get("hookSpecificOutput")
    return (
        isinstance(hook_specific, dict)
        and hook_specific.get("permissionDecision") == "deny"
    )


def run_script(path: str, payload_text: str, *, base: Path | None = None) -> int:
    """Run a hook script ``path`` (relative to *base*) with *payload_text* on stdin.

    Prints the script's stdout/stderr to the current process's stdout/stderr so
    any block decision emitted by the script reaches the hook harness.

    Returns the child's exit code, or ``_STDOUT_BLOCK_RC`` when the child exits 0
    but emits a block decision on stdout — so a stdout-only block is treated as
    final (later hooks are not invoked) while ``run()`` keeps the response on the
    exit-0 / stdout-JSON path rather than rewriting it to a non-zero exit.
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
    if result.returncode == 0 and _stdout_is_block(result.stdout):
        return _STDOUT_BLOCK_RC
    return result.returncode


# Top-level shell operators that separate one command from the next. We split
# the raw command on these (respecting quotes) so a commit chained after setup
# (``cd subdir && git commit -m x``, even space-free ``cd x&&git commit``) is
# still recognised — the shell will run the ``git commit`` segment, so
# commit-only hooks must fire for it. Longest operators first so ``&&``/``||``
# are matched before a bare ``&``/``|``. A literal newline is also a top-level
# shell command separator (``make\ngit commit -m x`` runs the commit), so it is
# included — otherwise commit-only hooks would be skipped for newline-chained
# commits.
_SHELL_SEPARATORS = ("&&", "||", ";", "|", "&", "\n")


def _split_top_level_segments(command: str) -> list[str]:
    """Split a raw shell command into top-level segments on shell operators.

    Quote- and escape-aware so separators inside single/double quotes (e.g. a
    commit message ``-m 'a && b'``) do not split the command. Backgrounding and
    pipe operators are treated as separators alongside ``&&``/``||``/``;``.
    """
    segments: list[str] = []
    current: list[str] = []
    i = 0
    n = len(command)
    quote: str | None = None
    while i < n:
        ch = command[i]
        if quote is not None:
            current.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                current.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            current.append(ch)
            current.append(command[i + 1])
            i += 2
            continue
        matched = next(
            (sep for sep in _SHELL_SEPARATORS if command.startswith(sep, i)),
            None,
        )
        if matched is not None:
            segments.append("".join(current))
            current = []
            i += len(matched)
            continue
        current.append(ch)
        i += 1
    segments.append("".join(current))
    return [seg for seg in (s.strip() for s in segments) if seg]


def is_git_commit_command(command: str) -> bool:
    """Return True iff *command* contains a top-level ``git commit`` invocation.

    Handles leading ``env``/``KEY=VAL`` prefixes and ``git`` global option flags
    (``-C``, ``-c``, ``--git-dir``, ``--work-tree``, ``--namespace``) before the
    subcommand position.  A commit chained after setup
    (``cd subdir && git commit -m x``, ``make && git commit ...``) is detected
    by scanning each top-level shell segment.  Returns False for unparseable
    commands.
    """
    return any(
        _segment_is_git_commit(segment)
        for segment in _split_top_level_segments(command)
    )


def _segment_is_git_commit(segment: str) -> bool:
    """Return True iff a single command segment is a ``git commit`` invocation."""
    try:
        tokens = shlex.split(segment)
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
    # Valid no-argument git global options (``git -h`` usage:
    # ``[-p | --paginate | -P | --no-pager] ... <command>``). They take no value
    # and may legitimately precede the subcommand, so ``git --no-pager commit``
    # and ``git -P commit`` must still be detected as commits.
    no_value_flags = {
        "-p",
        "-P",
        "--paginate",
        "--no-pager",
        "--no-replace-objects",
        "--bare",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
        "--no-optional-locks",
    }
    while index < len(tokens):
        token = tokens[index]
        if token == "commit":
            return True
        if token.startswith("-"):
            if token in no_value_flags:
                index += 1
                continue
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
        if rc == _STDOUT_BLOCK_RC:
            # Stdout-JSON block: the child already printed the block JSON on
            # stdout. Stop invoking later hooks but keep the exit-0/stdout-JSON
            # path intact (do not rewrite to a non-zero exit).
            return 0
        if rc != 0:
            return rc

    if is_git_commit_command(command):
        for hook_name, hook_path in commit_hooks:
            if not behavior_enabled(hook_name):
                continue
            rc = run_script(hook_path, payload_text, base=base_dir)
            if rc == _STDOUT_BLOCK_RC:
                return 0
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
