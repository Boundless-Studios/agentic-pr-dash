"""PermissionRequest → AgentFlow routing hook (runtime-agnostic).

When an AgentFlow hub is running, a permission prompt (tool + action) is routed
there and we long-poll for an allow/deny reply, returning the decision so the
local permission dialog is bypassed. When no hub is reachable — or the human
never replies — the hook is a clean no-op and the normal permission dialog
proceeds (project allow-list etc.).

Routing is gated on a reachable hub, not on stdin: a Claude command hook always
receives its JSON payload piped on stdin and runs with no controlling terminal,
so ``os.isatty(stdin)`` is *always* false and is useless as an "is a human
watching" signal. Instead the hook routes whenever a healthy hub is discovered
(the hub is the human's UI); set ``DISABLE_AGENTFLOW`` to force the local
permission dialog even when a hub is up. ADVISORY: this hook never blocks — on a
hub timeout or any error it falls through to the normal permission dialog.
"""

from __future__ import annotations

import json
import os
import sys

from agentic_pr_dash.codex_hooks import agentflow

_ALLOW_RESPONSES = {
    "yes", "y", "allow", "approve", "ok", "okay", "sure",
    "go ahead", "proceed", "1", "true",
}


def format_tool_description(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        description = tool_input.get("description", "")
        if description:
            return f"Run command: `{command}`\n\nDescription: {description}"
        return f"Run command: `{command}`"
    if tool_name == "Write":
        file_path = tool_input.get("file_path", "")
        content = tool_input.get("content", "")
        preview = content[:500] + "..." if len(content) > 500 else content
        return f"Write file: `{file_path}`\n\nContent preview:\n```\n{preview}\n```"
    if tool_name == "Edit":
        file_path = tool_input.get("file_path", "")
        old_string = tool_input.get("old_string", "")
        new_string = tool_input.get("new_string", "")
        return (
            f"Edit file: `{file_path}`\n\nReplace:\n```\n{old_string}\n```\n\n"
            f"With:\n```\n{new_string}\n```"
        )
    if tool_name == "Read":
        return f"Read file: `{tool_input.get('file_path', '')}`"
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        server = parts[1] if len(parts) > 1 else "unknown"
        tool = parts[2] if len(parts) > 2 else tool_name
        return f"MCP Tool: {server}/{tool}\n\nInput:\n```json\n{json.dumps(tool_input, indent=2)}\n```"
    return f"Tool: {tool_name}\n\nInput:\n```json\n{json.dumps(tool_input, indent=2)}\n```"


def parse_permission_response(response_text: str) -> bool:
    response_lower = response_text.strip().lower()
    return (
        response_lower in _ALLOW_RESPONSES
        or response_lower.startswith("yes")
        or response_lower.startswith("allow")
    )


def _load_payload() -> dict:
    try:
        raw = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _routing_enabled() -> bool:
    """Whether to route permission prompts to AgentFlow at all.

    A Claude command hook's stdin is the JSON payload (a pipe) and the process
    has no controlling terminal, so ``os.isatty`` can't tell an interactive
    session from a subagent — the *hub's* reachability is the real "a human can
    answer" signal. Routing is therefore on by default and opt-out via
    ``DISABLE_AGENTFLOW`` (or the legacy negative of ``FORCE_AGENTFLOW=0``)."""
    if os.environ.get("DISABLE_AGENTFLOW"):
        return False
    force = os.environ.get("FORCE_AGENTFLOW")
    if force is not None and force.strip().lower() in {"0", "false", "no", "off", ""}:
        return False
    return True


def main() -> int:
    payload = _load_payload()
    if payload.get("hook_event_name") != "PermissionRequest":
        return 0

    if not _routing_enabled():
        return 0  # explicitly disabled → normal permission handling

    base_url = agentflow.get_healthy_hub_url()
    if not base_url:
        return 0  # no reachable hub → normal permission dialog

    tool_name = payload.get("tool_name", "Unknown")
    tool_input = payload.get("tool_input", {}) or {}

    session_id = agentflow.register_session(
        base_url, f"Permission - {tool_name}", source="agent-permission-hook"
    )
    if not session_id:
        return 0

    description = format_tool_description(tool_name, tool_input)
    question = (
        f"Agent wants to use the **{tool_name}** tool.\n\n{description}\n\n"
        "**Allow this action?** (Reply 'yes'/'allow' to approve, anything else to deny)"
    )
    request_id = agentflow.create_request(
        base_url,
        session_id,
        title=f"Permission: {tool_name}",
        question=question,
        tags=["agent", "permission", tool_name.lower()],
    )
    if not request_id:
        return 0

    print(f"Permission request sent to AgentFlow: {base_url}", file=sys.stderr)
    print(f"   Tool: {tool_name}", file=sys.stderr)
    print("   Waiting for response...", file=sys.stderr)

    response_text = agentflow.await_response(base_url, session_id)
    if response_text is None:
        print("No response received, falling back to CLI permission dialog", file=sys.stderr)
        return 0

    if parse_permission_response(response_text):
        print("Permission GRANTED via AgentFlow", file=sys.stderr)
        decision = {"behavior": "allow"}
    else:
        print(f"Permission DENIED via AgentFlow: {response_text}", file=sys.stderr)
        decision = {
            "behavior": "deny",
            "message": f"Permission denied by user via AgentFlow: {response_text}",
        }

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
