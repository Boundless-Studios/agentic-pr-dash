"""AskUserQuestion → AgentFlow routing hook (runtime-agnostic).

PreToolUse hook for ``AskUserQuestion`` (Claude) / ``request_user_input``
(Codex). When an AgentFlow hub is running, the question is routed there and we
long-poll for the human reply, returning it as the tool result so the model
never sees the local CLI dialog. When no hub is reachable — or the human never
replies — the hook is a clean no-op and the normal flow proceeds.

ADVISORY: this hook must never block the tool. The payload arrives on stdin in
the Claude hook shape ``{"tool_name", "tool_input": {"questions": [...]}}``;
the repo-local shim normalizes Codex payloads to that shape before delegating.
"""

from __future__ import annotations

import json
import sys

from agentic_pr_dash.codex_hooks import agentflow

_ASK_TOOLS = {"AskUserQuestion", "request_user_input"}


def _format_question(question: str, options: list) -> str:
    if not options:
        return question
    formatted = question + "\n\nOptions:\n"
    for i, opt in enumerate(options, 1):
        label = opt.get("label", f"Option {i}")
        desc = opt.get("description", "")
        formatted += f"  {i}. {label}"
        if desc:
            formatted += f" - {desc}"
        formatted += "\n"
    return formatted


def _load_payload() -> dict:
    try:
        raw = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def main() -> int:
    payload = _load_payload()
    if payload.get("tool_name") not in _ASK_TOOLS:
        return 0

    base_url = agentflow.get_hub_url()
    if not base_url or not agentflow.hub_is_healthy(base_url):
        return 0  # no hub → normal AskUserQuestion proceeds

    questions = (payload.get("tool_input") or {}).get("questions") or []
    if not questions:
        return 0

    q = questions[0]  # handle the first question only
    question_text = q.get("question", "")
    options = q.get("options", [])
    header = q.get("header", "Question")

    session_id = agentflow.register_session(
        base_url, f"Question - {header}", source="agent-hook"
    )
    if not session_id:
        return 0

    request_id = agentflow.create_request(
        base_url,
        session_id,
        title="Agent Question",
        question=_format_question(question_text, options),
        tags=["agent", "ask-user"],
    )
    if not request_id:
        return 0

    print(f"Question sent to AgentFlow dashboard: {base_url}", file=sys.stderr)
    print("   Waiting for response...", file=sys.stderr)

    response_text = agentflow.await_response(base_url, session_id)
    if response_text is None:
        print("No response received, falling back to CLI", file=sys.stderr)
        return 0

    output = {
        "decision": "block",
        "reason": f"Response received via AgentFlow: {response_text}",
        "tool_result": {"answers": {"0": response_text}},
    }
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
