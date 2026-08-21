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
  * ``MODEL_DISPATCH_SHARED_GIT_COMMON=1`` routes the default log to the main
    checkout beside Git's common directory, so linked worktrees share one ledger;
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
disables it entirely. Embedders may pass a fail-open ``payload_observer`` to
``main`` for repository policy that must inspect the parsed payload before the
row is assembled.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime

from agentic_pr_dash.codex_hooks.dispatch_runner import (
    AGENT_CLASSIFY_MAP,
    AGENT_DEFAULT_MODEL_NAMES,
    classify_agent_dispatch,
    observation_from_agent_payload,
    resolve_agent_model,
)

# Classification keyword map (description + prompt prefix). Order matters: the
# first matching task type wins.
CLASSIFY_MAP = AGENT_CLASSIFY_MAP
DEFAULT_MODEL_NAMES = AGENT_DEFAULT_MODEL_NAMES

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
    return classify_agent_dispatch(description, prompt, subagent_type)


def resolve_model_name(model_raw: str) -> str:
    return resolve_agent_model(model_raw)


def is_fallback(task_type: str, model_raw: str) -> bool:
    """A Claude fallback: a task whose primary should be a cheaper model
    (codex/sonnet) but which was dispatched to sonnet/default instead."""
    return task_type in ("small_impl", "code_review", "debugging") and model_raw in (
        "sonnet",
        "",
    )


def log_path(project_dir: str) -> str:
    override = os.environ.get("MODEL_DISPATCH_LOG")
    if override:
        return override
    if os.environ.get("MODEL_DISPATCH_SHARED_GIT_COMMON") == "1":
        try:
            common_dir = subprocess.run(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            common_dir = ""
        if common_dir:
            ledger_root = (
                os.path.dirname(common_dir)
                if os.path.basename(common_dir) == ".git"
                else common_dir
            )
            return os.path.join(
                ledger_root, ".beads", "interactions.jsonl"
            )
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
                "bd",
                "audit",
                "record",
                "--no-daemon",
                "--kind",
                "model_dispatch",
                "--model",
                model_name,
                "--prompt",
                prompt_str,
                "--response",
                "dispatched",
            ],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass


def main(*, payload_observer: Callable[[dict], None] | None = None) -> int:
    if os.environ.get("SKIP_MODEL_DISPATCH") == "1":
        return 0

    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(input_data, dict):
        return 0

    invocation_extra = extra_fields()
    if payload_observer is not None:
        previous_extra = os.environ.get("MODEL_DISPATCH_EXTRA")
        try:
            payload_observer(deepcopy(input_data))
            invocation_extra = extra_fields()
        except Exception:  # noqa: BLE001 - observers are advisory by contract
            pass
        finally:
            if previous_extra is None:
                os.environ.pop("MODEL_DISPATCH_EXTRA", None)
            else:
                os.environ["MODEL_DISPATCH_EXTRA"] = previous_extra

    if input_data.get("tool_name") not in _DISPATCH_TOOLS:
        return 0

    tool_input = input_data.get("tool_input", {}) or {}
    description = tool_input.get("description", "")
    model_raw = tool_input.get("model", "")
    subagent_type = tool_input.get("subagent_type", "")

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    observation = observation_from_agent_payload(input_data, project_dir)
    if observation is None:
        return 0
    task_type = observation.task_type
    model_name = observation.resolved_model or resolve_model_name(model_raw)
    prompt_str = (
        f"task_type={task_type}, "
        f"fallback={str(is_fallback(task_type, model_raw)).lower()}, "
        f"subagent={subagent_type or 'default'}, "
        f"desc={description[:80]}"
    )

    entry = {
        "kind": "model_dispatch",
        "timestamp": datetime.now(UTC).isoformat(),
        "model": model_name,
        "prompt": prompt_str,
        "response": "dispatched",
        # Provenance stamps (BOU-2159): this hook only ever runs inside a live
        # session, and cwd is the CLAUDE_PROJECT_DIR-first project identity —
        # the same resolution the codex-dispatch logger uses — so scoped
        # ledger replays can attribute Agent rows.
        "source": "session",
        "cwd": project_dir,
        **invocation_extra,
    }
    _write_jsonl(log_path(project_dir), entry)
    _bd_audit(project_dir, model_name, prompt_str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
