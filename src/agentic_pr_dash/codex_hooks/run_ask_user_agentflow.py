"""AskUserQuestion → AgentFlow routing hook (runtime-agnostic).

PreToolUse hook for ``AskUserQuestion`` (Claude) / ``request_user_input``
(Codex). When an AgentFlow hub is running, the question(s) are routed there and
we long-poll for the human reply. When no hub is reachable — or the human never
replies — the hook is a clean no-op and the normal CLI dialog proceeds.

ADVISORY: this hook must never block the tool. PreToolUse has no documented way
to *inject* an answer back into ``AskUserQuestion`` (``updatedInput`` only edits
the tool's own arguments, and the deprecated top-level ``decision:"block"`` maps
to DENY, not "answer"). So on a hub reply we surface the human's answer to the
model via the documented, non-blocking ``additionalContext`` field and let the
tool proceed — we never emit a block/deny decision.

``AskUserQuestion`` can carry one to four questions; all of them are forwarded.
The payload arrives on stdin in the Claude hook shape
``{"tool_name", "tool_input": {"questions": [...]}}``; the repo-local shim
normalizes Codex payloads to that shape before delegating.
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


def _format_all_questions(questions: list) -> str:
    """Render every question (AskUserQuestion carries 1–4) into one prompt."""
    if len(questions) == 1:
        q = questions[0]
        return _format_question(q.get("question", ""), q.get("options", []))
    blocks = []
    for i, q in enumerate(questions, 1):
        body = _format_question(q.get("question", ""), q.get("options", []))
        header = q.get("header", f"Question {i}")
        blocks.append(f"### {i}. {header}\n{body}")
    return "\n\n".join(blocks)


def main() -> int:
    payload = _load_payload()
    if payload.get("tool_name") not in _ASK_TOOLS:
        return 0

    base_url = agentflow.get_healthy_hub_url()
    if not base_url:
        return 0  # no reachable hub → normal AskUserQuestion proceeds

    questions = (payload.get("tool_input") or {}).get("questions") or []
    if not questions:
        return 0

    headers = [q.get("header", "Question") for q in questions]
    title = headers[0] if len(headers) == 1 else f"{len(questions)} questions"

    session_id = agentflow.register_session(
        base_url, f"Question - {title}", source="agent-hook"
    )
    if not session_id:
        return 0

    request_id = agentflow.create_request(
        base_url,
        session_id,
        title="Agent Question",
        question=_format_all_questions(questions),
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

    # PreToolUse cannot inject an answer into AskUserQuestion, and a blocking
    # decision would DENY the tool (violating the advisory contract). Surface the
    # human's reply as non-blocking context and let the tool proceed.
    print("Response received via AgentFlow; surfacing to the agent", file=sys.stderr)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": (
                "A human answered this question via the AgentFlow dashboard. "
                f"Use this answer:\n{response_text}"
            ),
        }
    }
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
