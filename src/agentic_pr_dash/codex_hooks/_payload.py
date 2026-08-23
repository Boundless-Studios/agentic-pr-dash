"""Shared payload helpers for upstream codex hook modules.

Provides ``load_payload``, ``normalized_payload``, and ``repo_root``
equivalents that work without any gaia-specific imports.  The gaia shim's
``common.py`` is the authoritative upstream reference; this module mirrors
its semantics exactly.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_CODEX_TO_CLAUDE_TOOL: dict[str, str] = {
    "bash": "Bash",
    "exec_command": "Bash",
    "functions.exec_command": "Bash",
    "request_user_input": "request_user_input",
    "functions.request_user_input": "request_user_input",
}


def load_payload() -> dict:
    """Read a JSON hook payload from stdin.

    Returns ``{}`` and exits 0 on malformed / non-dict input — hooks are
    advisory and must not crash the agent session on bad input.
    """
    try:
        raw = json.load(sys.stdin)
    except (json.JSONDecodeError, Exception):  # noqa: BLE001
        raise SystemExit(0)
    if not isinstance(raw, dict):
        raise SystemExit(0)
    return raw


def _normalized_tool_name(payload: dict) -> str:
    original = payload.get("tool_name", "")
    return _CODEX_TO_CLAUDE_TOOL.get(original, original)


def _normalized_tool_input(payload: dict) -> dict:
    original = payload.get("tool_input", {}) or {}
    if not isinstance(original, dict):
        return {}
    if payload.get("tool_name") in {"exec_command", "functions.exec_command"}:
        return {"command": original.get("cmd", "")}
    return original


def _normalized_cwd(payload: dict) -> str:
    original = payload.get("tool_input", {}) or {}
    if isinstance(original, dict):
        workdir = original.get("workdir")
        if isinstance(workdir, str) and workdir:
            return workdir

    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return cwd

    return os.getcwd()


# Metadata keys that warden's session-scoped behaviour (per-session approval /
# YOLO state) depends on. We rename only the tool fields that need normalising
# and pass these through verbatim when present, so the downstream warden-hook
# can still distinguish the active session.
_PASSTHROUGH_METADATA_KEYS = ("session_id", "transcript_path")


def normalized_payload(payload: dict) -> dict:
    """Return the normalised hook payload for both Claude and Codex hooks.

    Always contains ``{tool_name, tool_input, cwd}``; passthrough metadata such
    as ``session_id`` is preserved when present so warden's session-scoped
    behaviour keeps working.
    """
    normalized = {
        "tool_name": _normalized_tool_name(payload),
        "tool_input": _normalized_tool_input(payload),
        "cwd": _normalized_cwd(payload),
    }
    for key in _PASSTHROUGH_METADATA_KEYS:
        value = payload.get(key)
        if value is not None:
            normalized[key] = value
    return normalized


def repo_root(payload: dict | None = None) -> Path:
    """Return the best-effort repository root.

    Unlike the gaia version (which walks ``__file__`` parents), upstream
    cannot assume a fixed directory layout.  We use the payload cwd when
    available, falling back to the process cwd.
    """
    if payload is not None:
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            return Path(cwd)
    return Path(os.getcwd())


def behavior_enabled(name: str, *, default: bool = True) -> bool:
    """Return whether a named hook behaviour is enabled.

    Upstream carries no gaia settings module, so behaviour defaults to
    ``True`` and can be overridden by environment variable
    ``AGENTIC_PR_DASH_HOOK_<NAME>`` (upper-cased, hyphens → underscores).
    The gaia shim owns real settings logic; upstream just needs a sensible
    default that the shim can override via env.
    """
    env_key = "AGENTIC_PR_DASH_HOOK_" + name.upper().replace("-", "_")
    env_val = os.environ.get(env_key, "").strip().lower()
    if env_val in {"0", "false", "no", "off"}:
        return False
    if env_val in {"1", "true", "yes", "on"}:
        return True
    return default
