"""Shared AgentFlow-hub client used by the ask-user and permission hooks.

The hooks route a human-in-the-loop prompt (an AskUserQuestion, or a permission
dialog) to a running AgentFlow / sessionbus hub and long-poll for the human's
reply. This module owns the generic transport: hub discovery, session
registration, request creation, inbox polling, and acknowledgement. It is
runtime-agnostic — Claude Code and Codex share the same wire protocol — so the
behavior lives here and the repo-local shims only carry config (which hub
runtime files to look at) and the registration ``source`` tag.

Every hook that uses this is ADVISORY: when the hub is absent/unhealthy or the
human never replies, the caller falls through to the normal CLI flow. Nothing
here raises into the hook path.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Default hub runtime-file locations (most-preferred first). A repo may override
# via ``AGENTFLOW_RUNTIME_FILES`` (os.pathsep-separated) without changing code.
_DEFAULT_RUNTIME_FILES = (
    Path.home() / ".agentflow" / "runtime.json",
    Path.home() / ".sessionbus" / "runtime.json",
)


def runtime_files() -> list[Path]:
    override = os.environ.get("AGENTFLOW_RUNTIME_FILES")
    if override:
        return [Path(p) for p in override.split(os.pathsep) if p]
    return list(_DEFAULT_RUNTIME_FILES)


def get_hub_url() -> str | None:
    """Read the AgentFlow hub base URL from the first runtime file present."""
    for path in runtime_files():
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        base = data.get("base_url")
        if isinstance(base, str) and base:
            return base
    return None


def hub_is_healthy(base_url: str) -> bool:
    try:
        req = urllib.request.Request(f"{base_url}/api/sessions", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - advisory; any failure means "no hub"
        return False


def register_session(base_url: str, display_name: str, *, source: str) -> str | None:
    try:
        data = json.dumps(
            {
                "display_name": display_name,
                "metadata": {
                    "source": source,
                    "cwd": os.getcwd(),
                    "branch": os.environ.get("CLAUDE_GIT_BRANCH", "unknown"),
                },
            }
        ).encode()
        req = urllib.request.Request(
            f"{base_url}/api/sessions/register",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read()).get("session_id")
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to register session: {exc}", file=sys.stderr)
        return None


def create_request(
    base_url: str,
    session_id: str,
    *,
    title: str,
    question: str,
    tags: list[str],
    priority: str = "HIGH",
) -> str | None:
    try:
        data = json.dumps(
            {"title": title, "question": question, "priority": priority, "tags": tags}
        ).encode()
        req = urllib.request.Request(
            f"{base_url}/api/sessions/{session_id}/requests",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read()).get("request_id")
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to create request: {exc}", file=sys.stderr)
        return None


def poll_inbox(base_url: str, session_id: str, timeout: int = 60) -> dict | None:
    try:
        req = urllib.request.Request(
            f"{base_url}/api/sessions/{session_id}/inbox?timeout={timeout}",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
            result = json.loads(resp.read())
            for msg in result.get("messages", []):
                if msg.get("type") == "INPUT_RESPONSE":
                    return msg
    except urllib.error.URLError as exc:
        if "timed out" not in str(exc):
            print(f"Poll failed: {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"Poll failed: {exc}", file=sys.stderr)
    return None


def ack_message(base_url: str, session_id: str, message_id: str) -> bool:
    try:
        req = urllib.request.Request(
            f"{base_url}/api/sessions/{session_id}/inbox/{message_id}/ack",
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def await_response(
    base_url: str,
    session_id: str,
    *,
    max_wait: int = 600,
    poll_interval: int = 10,
) -> str | None:
    """Long-poll the inbox until an INPUT_RESPONSE arrives or we time out.

    Returns the response text (acking the message), or ``None`` on timeout so the
    caller can fall through to its normal CLI flow.
    """
    start = time.time()
    while time.time() - start < max_wait:
        msg = poll_inbox(base_url, session_id, timeout=poll_interval)
        if msg:
            response_text = msg.get("payload", {}).get("response_text", "")
            message_id = msg.get("message_id")
            if message_id:
                ack_message(base_url, session_id, message_id)
            return response_text
    return None
