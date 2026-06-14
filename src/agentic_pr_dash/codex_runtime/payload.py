"""Codex→Claude hook payload normalization + upstream-hook loader (BOU-1672).

Repo-agnostic. A host repo supplies its own ``repo_root`` (a zero-arg callable
returning the checkout root) so these helpers stay free of any path layout
assumptions.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

# Codex names its shell tool ``exec_command`` (sometimes namespaced
# ``functions.exec_command``); Claude calls the same thing ``Bash``. Map so
# shared hooks see the Claude tool name they already branch on.
CODEX_TO_CLAUDE_TOOL = {
    "exec_command": "Bash",
    "functions.exec_command": "Bash",
    "request_user_input": "request_user_input",
    "functions.request_user_input": "request_user_input",
}


def load_payload() -> dict:
    """Read the hook payload from stdin. A non-dict payload is a clean no-op."""
    raw = json.load(sys.stdin)
    if not isinstance(raw, dict):
        raise SystemExit(0)
    return raw


def normalized_tool_name(payload: dict) -> str:
    original = payload.get("tool_name", "")
    return CODEX_TO_CLAUDE_TOOL.get(original, original)


def normalized_tool_input(payload: dict) -> dict:
    original = payload.get("tool_input", {}) or {}
    # `or {}` only collapses falsy values; a truthy non-dict (e.g. a bare string from
    # an upstream adapter bug) would otherwise pass straight through and violate the
    # `-> dict` contract, crashing every consumer that does `tool_input.get("command")`.
    if not isinstance(original, dict):
        return {}
    if payload.get("tool_name") in {"exec_command", "functions.exec_command"}:
        return {"command": original.get("cmd", "")}
    return original


def normalized_cwd(payload: dict, *, repo_root: Callable[[], Path]) -> str:
    original = payload.get("tool_input", {}) or {}
    if isinstance(original, dict):
        workdir = original.get("workdir")
        if isinstance(workdir, str) and workdir:
            return workdir

    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd

    return str(repo_root())


def normalized_payload(payload: dict, *, repo_root: Callable[[], Path]) -> dict:
    return {
        "tool_name": normalized_tool_name(payload),
        "tool_input": normalized_tool_input(payload),
        "cwd": normalized_cwd(payload, repo_root=repo_root),
    }


def apply_shared_env(payload: dict, *, repo_root: Callable[[], Path]) -> None:
    """Seed the project-dir env vars shared hooks rely on.

    ``payload`` is accepted (and ignored) so callers can pass the raw hook
    payload uniformly; the project dir is always the host repo root.
    """
    root = str(repo_root())
    os.environ["CLAUDE_PROJECT_DIR"] = root
    os.environ["GAIA_PROJECT_DIR"] = root


def load_upstream_hook_main(hook_name: str) -> Callable[[], int] | None:
    """Resolve ``agentic_pr_dash.codex_hooks.<hook_name>.main`` (or ``None``).

    Returns ``None`` ONLY when the hook pack is GENUINELY absent (the package or
    the named hook module isn't installed). A PRESENT-but-broken hook pack — a
    module that imports but has a missing/renamed sibling import, or has no
    callable ``main`` — is reported loudly rather than silently disabled, so a
    broken cutover never masquerades as a successful no-op.
    """
    module_name = f"agentic_pr_dash.codex_hooks.{hook_name}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing in {"agentic_pr_dash", "agentic_pr_dash.codex_hooks", module_name}:
            return None
        raise

    main = getattr(module, "main", None)
    if main is None:
        raise AttributeError(
            f"{module_name} is installed but missing a `main` entrypoint; "
            "this is an agentic-pr-dash packaging error, not an absent hook."
        )
    if not callable(main):
        raise TypeError(f"{module_name}.main is not callable")
    return main
