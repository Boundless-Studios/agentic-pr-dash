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
    ClassificationAuthority,
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

AGENT_CLASSIFY_MAP = [
    (
        "code_review",
        [
            r"\breview\b",
            r"\bcode review\b",
            r"\baudit (?:the )?code\b",
            r"\bcheck (?:the )?implementation\b",
            r"\bverify (?:the )?changes\b",
        ],
    ),
    (
        "debugging",
        [
            r"\bdebug\b",
            r"\bdiagnose\b",
            r"\broot cause\b",
            r"\bfix (?:the )?bug\b",
            r"\bstack trace\b",
            r"\bfailing test\b",
            r"\btest failure\b",
        ],
    ),
    (
        "exploration",
        [
            r"\bexplore\b",
            r"\bhow does .+ work\b",
            r"\barchitecture\b",
            r"\btrace (?:the )?(?:data )?flow\b",
            r"\bfind all (?:usages|references|callers)\b",
        ],
    ),
    (
        "trivial",
        [
            r"\blookup\b",
            r"\bsimple (?:fix|change|update)\b",
            r"\bquick (?:fix|check|change)\b",
            r"\bone.line(?:r)?\b",
        ],
    ),
    (
        "large_impl",
        [
            r"\b(?:multiple|several|many) files?\b",
            r"\bcross[- ]module\b",
            r"\bacross (?:multiple |several )?\w*(?:files?|modules?|services?|packages?)\b",
            r"\b(?:3|4|5|6|7|8|9|\d{2,})\+?\s*files?\b",
            r"\bmulti[- ]file\b",
            r"\blarge (?:impl|implementation|change|refactor)\b",
            r"\bend[- ]to[- ]end\b",
            r"\bfull[- ]stack\b",
            r"\btyped.*cross[- ]module\b",
        ],
    ),
    (
        "small_impl",
        [
            r"\bimplement\b",
            r"\badd feature\b",
            r"\bcreate (?:the |a )?(?:new )?\w+ (?:file|class|function|component|module)\b",
            r"\bbuild\b",
            r"\bwrite (?:the )?code\b",
            r"\bmodify (?:the )?code\b",
            r"\bscaffold\b",
            r"\brefactor\b",
        ],
    ),
]

AGENT_DEFAULT_MODEL_NAMES = {
    "sonnet": "sonnet-4.6",
    "opus": "opus-4.6",
    "haiku": "haiku-4.5",
    "": "opus-4.6",
}
_AGENT_DEFAULT_MODEL = "opus-4.6"


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
        and observation.classification_authority is ClassificationAuthority.DECLARED
    ):
        try:
            additional_context = callback(observation)
        except Exception:  # noqa: BLE001 - repository callbacks are advisory
            additional_context = None
    return DispatchHookResult(observation, additional_context)


def classify_agent_dispatch(description: str, prompt: str, subagent_type: str) -> str:
    """Classify an Agent/spawn_agent dispatch using the shared task taxonomy."""

    if subagent_type == "Explore":
        return "exploration"
    text = f"{description} {prompt[:500]}".lower()
    for task_type, patterns in AGENT_CLASSIFY_MAP:
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
            return task_type
    return "general"


def resolve_agent_model(model_raw: str) -> str:
    """Resolve a runtime model alias to the stable ledger display name."""

    return AGENT_DEFAULT_MODEL_NAMES.get(model_raw, model_raw or _AGENT_DEFAULT_MODEL)


def observation_from_agent_payload(
    payload: dict[str, object], worktree_root: str
) -> DispatchObservation | None:
    """Normalize an Agent/spawn_agent PostToolUse payload."""

    if payload.get("tool_name") not in {
        "Agent",
        "spawn_agent",
        "functions.spawn_agent",
    }:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    description = str(tool_input.get("description") or "")
    prompt = str(tool_input.get("prompt") or "")
    subagent_type = str(tool_input.get("subagent_type") or "")
    model_raw = str(tool_input.get("model") or "")
    return DispatchObservation(
        provider=DispatchProvider.CLAUDE,
        source=DispatchSource.INTERACTIVE_HOOK,
        session_id=str(payload.get("session_id") or ""),
        worktree_root=worktree_root,
        command=f"{description} {prompt}".strip(),
        task_type=classify_agent_dispatch(description, prompt, subagent_type),
        requested_model=model_raw or None,
        resolved_model=resolve_agent_model(model_raw),
        outcome=DispatchOutcome.SUCCESS,
    )


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
    declared = _declared_classification(request.payload)
    if declared is not None and not _has_provider_invocation(command, request.provider):
        declared = None
    task_type = (
        declared[0]
        if declared is not None
        else ("review" if _REVIEW_PATTERN.search(command) else "exec")
    )
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
        classification_authority=(
            ClassificationAuthority.DECLARED
            if declared is not None
            else ClassificationAuthority.LEGACY_INFERRED
        ),
        classification_framework=declared[1] if declared is not None else None,
    )


def _declared_classification(payload: dict[str, object]) -> tuple[str, str] | None:
    classification = payload.get("dispatch_classification")
    if not isinstance(classification, dict):
        return None
    task_type = classification.get("task_type")
    framework = classification.get("framework")
    if not isinstance(task_type, str) or not task_type.strip():
        return None
    if framework != "coding-agent/v1":
        return None
    return task_type.strip(), framework.strip()


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


def _has_provider_invocation(command: str, provider: DispatchProvider) -> bool:
    """Return whether a shell command segment executes the requested provider."""
    executable = "codex" if provider is DispatchProvider.CODEX else "opencode"
    subcommand = "exec" if provider is DispatchProvider.CODEX else "run"
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False

    if any(token in {"&&", "||", "&"} for token in tokens):
        return False

    at_command_start = True
    for index, token in enumerate(tokens):
        if token and all(character in ";&|\n" for character in token):
            at_command_start = True
            continue
        if not at_command_start:
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            continue
        at_command_start = False
        if (
            Path(token).name == executable
            and index + 1 < len(tokens)
            and tokens[index + 1] == subcommand
        ):
            return True
    return False


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
