"""Model-dispatch logger (runtime-agnostic).

PostToolUse hook: log every Agent/subagent dispatch so model-stats and the
session-start stats hook can report which model handled which task type and
whether a Claude fallback was used in place of the cheaper primary.

Generic behavior (here):
  * classify the dispatch from its description / prompt / subagent type;
  * resolve the model display name and the fallback flag;
  * append a ``model_dispatch`` JSON line to the interactions log.

Repo config:
  * the interactions-log path — defaults to ``<project>/.beads/interactions.jsonl``,
    overridable via ``MODEL_DISPATCH_LOG``;
  * an optional secondary ``bd audit record`` write, enabled via
    ``MODEL_DISPATCH_BD_AUDIT=1`` (beads is a repo-specific tool, off by default
    so the upstream surface has no beads dependency).

Row provenance (BOU-2159):
  * every ledger row is stamped with ``source: "session"`` (this logger only
    runs as a PostToolUse hook inside a live session) and ``cwd`` (the
    session's project dir, ``CLAUDE_PROJECT_DIR``-first), so scoped replay
    consumers — e.g. a review-budget breaker recomputing rounds from the
    ledger — can attribute Agent-dispatch rows instead of skipping them;
  * the invoking hook may extend the row via ``MODEL_DISPATCH_EXTRA``: a JSON
    object (e.g. ``{"verdict": "no_findings", "task_type": "code_review"}``)
    merged into the row before it is written. Reserved base keys are never
    overwritten and null values are dropped. This lets a wrapper hook that
    pre-reads the payload inject its parsed verdict without double-writing.
    All fields are optional — readers must tolerate their absence, and legacy
    rows (without ``source``/``cwd``) keep parsing unchanged.

Runs after the Agent call completes, so it never blocks. ``SKIP_MODEL_DISPATCH=1``
disables it entirely.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

# Classification keyword map (description + prompt prefix). Order matters: the
# first matching task type wins.
CLASSIFY_MAP = [
    ("code_review", [
        r"\breview\b", r"\bcode review\b", r"\baudit (?:the )?code\b",
        r"\bcheck (?:the )?implementation\b", r"\bverify (?:the )?changes\b",
    ]),
    ("debugging", [
        r"\bdebug\b", r"\bdiagnose\b", r"\broot cause\b", r"\bfix (?:the )?bug\b",
        r"\bstack trace\b", r"\bfailing test\b", r"\btest failure\b",
    ]),
    ("exploration", [
        r"\bexplore\b", r"\bhow does .+ work\b", r"\barchitecture\b",
        r"\btrace (?:the )?(?:data )?flow\b",
        r"\bfind all (?:usages|references|callers)\b",
    ]),
    ("trivial", [
        r"\blookup\b", r"\bsimple (?:fix|change|update)\b",
        r"\bquick (?:fix|check|change)\b", r"\bone.line(?:r)?\b",
    ]),
    ("large_impl", [
        r"\b(?:multiple|several|many) files?\b", r"\bcross[- ]module\b",
        r"\bacross (?:multiple |several )?\w*(?:files?|modules?|services?|packages?)\b",
        r"\b(?:3|4|5|6|7|8|9|\d{2,})\+?\s*files?\b",
        r"\bmulti[- ]file\b", r"\blarge (?:impl|implementation|change|refactor)\b",
        r"\bend[- ]to[- ]end\b", r"\bfull[- ]stack\b",
        r"\btyped.*cross[- ]module\b",
    ]),
    ("small_impl", [
        r"\bimplement\b", r"\badd feature\b",
        r"\bcreate (?:the |a )?(?:new )?\w+ (?:file|class|function|component|module)\b",
        r"\bbuild\b", r"\bwrite (?:the )?code\b",
        r"\bmodify (?:the )?code\b", r"\bscaffold\b", r"\brefactor\b",
    ]),
]

DEFAULT_MODEL_NAMES = {
    "sonnet": "sonnet-4.6",
    "opus": "opus-4.6",
    "haiku": "haiku-4.5",
    "": "opus-4.6",
}
_DEFAULT_MODEL = "opus-4.6"

# Subagent-dispatch tool names across runtimes. Claude fires ``Agent``; Codex
# fires ``spawn_agent`` (and the namespaced ``functions.spawn_agent`` shape seen
# elsewhere in this package, e.g. ``run_arm_pr_watch``).
_DISPATCH_TOOLS = {"Agent", "spawn_agent", "functions.spawn_agent"}

# Base row keys the MODEL_DISPATCH_EXTRA extension may never overwrite
# (BOU-2159): the core legacy shape plus the provenance stamps.
_RESERVED_KEYS = frozenset(
    {"kind", "timestamp", "model", "prompt", "response", "source", "cwd"}
)


def extra_fields() -> dict:
    """Optional row extensions injected by the invoking hook (BOU-2159).

    ``MODEL_DISPATCH_EXTRA`` holds a JSON object merged into the ledger row so
    a wrapper hook (which pre-reads the PostToolUse payload and, e.g., parses
    a review verdict from the subagent's report) can attribute the row without
    writing a second one. Reserved base keys and null values are dropped;
    malformed payloads are ignored — logging never blocks.
    """
    raw = os.environ.get("MODEL_DISPATCH_EXTRA")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: value
        for key, value in data.items()
        if key not in _RESERVED_KEYS and value is not None
    }


def classify(description: str, prompt: str, subagent_type: str) -> str:
    if subagent_type == "Explore":
        return "exploration"
    text = f"{description} {prompt[:500]}".lower()
    for task_type, keywords in CLASSIFY_MAP:
        for kw in keywords:
            if re.search(kw, text, re.IGNORECASE):
                return task_type
    return "general"


def resolve_model_name(model_raw: str) -> str:
    return DEFAULT_MODEL_NAMES.get(model_raw, model_raw or _DEFAULT_MODEL)


def is_fallback(task_type: str, model_raw: str) -> bool:
    """A Claude fallback: a task whose primary should be a cheaper model
    (codex/sonnet) but which was dispatched to sonnet/default instead."""
    return task_type in ("small_impl", "code_review", "debugging") and model_raw in ("sonnet", "")


def log_path(project_dir: str) -> str:
    override = os.environ.get("MODEL_DISPATCH_LOG")
    if override:
        return override
    return os.path.join(project_dir, ".beads", "interactions.jsonl")


def _write_jsonl(path: str, entry: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # never block on logging failure


def _bd_audit(project_dir: str, model_name: str, prompt_str: str) -> None:
    if os.environ.get("MODEL_DISPATCH_BD_AUDIT") != "1":
        return
    try:
        subprocess.run(
            [
                "bd", "audit", "record", "--no-daemon",
                "--kind", "model_dispatch",
                "--model", model_name,
                "--prompt", prompt_str,
                "--response", "dispatched",
            ],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass


def main() -> int:
    if os.environ.get("SKIP_MODEL_DISPATCH") == "1":
        return 0

    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(input_data, dict):
        return 0

    if input_data.get("tool_name") not in _DISPATCH_TOOLS:
        return 0

    tool_input = input_data.get("tool_input", {}) or {}
    description = tool_input.get("description", "")
    prompt = tool_input.get("prompt", "")
    model_raw = tool_input.get("model", "")
    subagent_type = tool_input.get("subagent_type", "")

    task_type = classify(description, prompt, subagent_type)
    model_name = resolve_model_name(model_raw)

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    prompt_str = (
        f"task_type={task_type}, "
        f"fallback={str(is_fallback(task_type, model_raw)).lower()}, "
        f"subagent={subagent_type or 'default'}, "
        f"desc={description[:80]}"
    )

    entry = {
        "kind": "model_dispatch",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model_name,
        "prompt": prompt_str,
        "response": "dispatched",
        # Provenance stamps (BOU-2159): this hook only ever runs inside a live
        # session, and cwd is the CLAUDE_PROJECT_DIR-first project identity —
        # the same resolution the codex-dispatch logger uses — so scoped
        # ledger replays can attribute Agent rows.
        "source": "session",
        "cwd": project_dir,
        **extra_fields(),
    }
    _write_jsonl(log_path(project_dir), entry)
    _bd_audit(project_dir, model_name, prompt_str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
