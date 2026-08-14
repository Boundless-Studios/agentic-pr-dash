"""Provider-neutral dispatch parsing and persistence."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agentic_pr_dash.dispatch_observation import (
    DispatchObservation,
    DispatchOutcome,
    DispatchProvider,
    DispatchSource,
)


@dataclass(frozen=True, slots=True)
class DispatchHookRequest:
    """Normalized input shared by interactive and detached hook entrypoints."""

    provider: DispatchProvider
    source: DispatchSource
    payload: dict[str, object]
    ledger_path: Path
    availability_path: Path


@dataclass(frozen=True, slots=True)
class DispatchHookResult:
    """Observation and optional repository-owned hook context."""

    observation: DispatchObservation | None
    additional_context: str | None


RepositoryCallback = Callable[[DispatchObservation], str | None]

_UNAVAILABLE_PATTERN = re.compile(
    r"\b(?:quota|rate[ -]?limit|authorization|unauthorized|forbidden|credits?)\b",
    re.IGNORECASE,
)
_REVIEW_PATTERN = re.compile(r"\b(?:review|audit|fix|debug)\b", re.IGNORECASE)


def run_dispatch_hook(
    request: DispatchHookRequest,
    callback: RepositoryCallback | None = None,
) -> DispatchHookResult:
    """Parse and persist one dispatch, then invoke repository policy if eligible."""

    observation = _observation_from_request(request)
    if observation is None:
        return DispatchHookResult(observation=None, additional_context=None)

    if observation.outcome is DispatchOutcome.UNAVAILABLE:
        _write_json_atomic(
            request.availability_path,
            {
                "provider": observation.provider.value,
                "available": False,
                "session_id": observation.session_id,
                "worktree_root": observation.worktree_root,
            },
        )
    _append_jsonl(request.ledger_path, observation.to_dict())

    additional_context = None
    if (
        callback is not None
        and observation.source is DispatchSource.INTERACTIVE_HOOK
        and observation.outcome is DispatchOutcome.SUCCESS
        and observation.task_type == "review"
    ):
        try:
            additional_context = callback(observation)
        except Exception:  # noqa: BLE001 - repository callbacks are advisory
            additional_context = None
    return DispatchHookResult(observation, additional_context)


def run_provider_entrypoint(
    provider: DispatchProvider,
    argv: list[str],
    callback: RepositoryCallback | None = None,
) -> int:
    """Translate hook stdin or detached CLI arguments into a dispatch request."""

    source = DispatchSource.INTERACTIVE_HOOK
    if "--command" in argv:
        source = DispatchSource.DETACHED_RUNNER
        command = _option_value(argv, "--command") or ""
        exit_code = _option_value(argv, "--exit-code")
        payload: dict[str, object] = {
            "session_id": os.environ.get("CLAUDE_SESSION_ID", ""),
            "cwd": os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()),
            "tool_input": {"command": command},
            "tool_response": {"exit_code": exit_code},
        }
    else:
        try:
            raw = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError):
            return 0
        if not isinstance(raw, dict):
            return 0
        payload = _normalize_hook_payload(raw)

    root = Path(str(payload.get("cwd") or os.getcwd()))
    request = DispatchHookRequest(
        provider=provider,
        source=source,
        payload=payload,
        ledger_path=Path(
            os.environ.get("MODEL_DISPATCH_LOG", root / ".beads" / "interactions.jsonl")
        ),
        availability_path=Path(
            os.environ.get(
                "DISPATCH_AVAILABILITY_PATH",
                root / ".agentic-pr-dash" / f"{provider.value}-availability.json",
            )
        ),
    )
    result = run_dispatch_hook(request, callback)
    if result.additional_context:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": result.additional_context,
                    }
                }
            )
        )
    return 0


def _observation_from_request(
    request: DispatchHookRequest,
) -> DispatchObservation | None:
    tool_input = request.payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not _matches_provider(command, request.provider):
        return None

    response = request.payload.get("tool_response")
    response = response if isinstance(response, dict) else {}
    outcome = _outcome(response)
    task_type = "review" if _REVIEW_PATTERN.search(command) else "exec"
    verdict = response.get("review_verdict")
    if not isinstance(verdict, dict) or outcome is not DispatchOutcome.SUCCESS:
        verdict = None
    requested_model = _requested_model(command)

    return DispatchObservation(
        provider=request.provider,
        source=request.source,
        session_id=str(request.payload.get("session_id") or ""),
        worktree_root=str(request.payload.get("cwd") or os.getcwd()),
        command=command,
        task_type=task_type,
        requested_model=requested_model,
        resolved_model=requested_model,
        outcome=outcome,
        review_verdict=dict(verdict) if verdict is not None else None,
    )


def _normalize_hook_payload(payload: dict[str, object]) -> dict[str, object]:
    tool_input = payload.get("tool_input")
    normalized_input = tool_input if isinstance(tool_input, dict) else {}
    if payload.get("tool_name") in {"exec_command", "functions.exec_command"}:
        normalized_input = {"command": normalized_input.get("cmd", "")}
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        cwd = os.getcwd()
    return {
        **payload,
        "cwd": cwd,
        "tool_input": normalized_input,
    }


def _option_value(argv: list[str], option: str) -> str | None:
    try:
        index = argv.index(option)
    except ValueError:
        return None
    return argv[index + 1] if index + 1 < len(argv) else None


def _matches_provider(command: str, provider: DispatchProvider) -> bool:
    executable = "codex" if provider is DispatchProvider.CODEX else "opencode"
    subcommand = "exec" if provider is DispatchProvider.CODEX else "run"
    return bool(re.search(rf"\b{executable}\s+{subcommand}\b", command))


def _requested_model(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    for index, token in enumerate(tokens):
        if token in {"--model", "-m"} and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith("--model="):
            return token.partition("=")[2]
        if token == "-c" and index + 1 < len(tokens):
            match = re.fullmatch(r"model=(.+)", tokens[index + 1])
            if match:
                return match.group(1).strip("\"'")
    return None


def _outcome(response: dict[object, object]) -> DispatchOutcome:
    exit_code = response.get("exit_code", response.get("exitCode", 0))
    try:
        failed = int(exit_code) != 0
    except (TypeError, ValueError):
        failed = False
    if not failed:
        return DispatchOutcome.SUCCESS
    stderr = response.get("stderr")
    if isinstance(stderr, str) and _UNAVAILABLE_PATTERN.search(stderr):
        return DispatchOutcome.UNAVAILABLE
    return DispatchOutcome.FAILURE


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
