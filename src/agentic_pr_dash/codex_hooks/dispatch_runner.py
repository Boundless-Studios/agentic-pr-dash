"""Provider-neutral dispatch parsing and persistence."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from agentic_pr_dash.dispatch_observation import (
    ClassificationAuthority,
    DispatchObservation,
    DispatchOutcome,
    DispatchProvider,
    DispatchSource,
)
from agentic_pr_dash.dispatch_telemetry import (
    DispatchParseStatus,
    DispatchResolutionSource,
    DispatchTelemetry,
    emit_dispatch_span,
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
_ERROR_SCAN_CHUNK_SIZE = 64 * 1024
_ERROR_SCAN_OVERLAP = 64
_STRUCTURED_STDIN_LIMIT = 1024 * 1024
_MIN_PLAUSIBLE_UNIX_NANO = 946_684_800_000_000_000
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
    _append_jsonl(request.ledger_path, observation.to_persisted_dict())

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
    if "--structured-stdin" in argv:
        source = DispatchSource.DETACHED_RUNNER
        payload = _structured_stdin_payload(argv)
        if payload is None:
            return 0
    elif "--command" in argv:
        source = DispatchSource.DETACHED_RUNNER
        command = _option_value(argv, "--command") or ""
        exit_code = _option_value(argv, "--exit-code")
        error_file = _option_value(argv, "--error-file")
        stderr = ""
        if error_file:
            try:
                if _error_file_reports_unavailable(Path(error_file)):
                    stderr = "quota"
            except OSError:
                pass
        payload: dict[str, object] = {
            "session_id": os.environ.get("CLAUDE_SESSION_ID", ""),
            "cwd": os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()),
            "tool_input": {"command": command},
            "tool_response": {"exit_code": exit_code, "stderr": stderr},
        }
        configured_model = _option_value(argv, "--configured-model")
        default_model = _option_value(argv, "--default-model")
        if configured_model is not None or default_model is not None:
            payload["dispatch_model_resolution"] = {
                "configured_model": configured_model,
                "default_model": default_model,
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
    structured = _structured_telemetry(request)
    if structured is not None:
        effective_root = Path(structured.cwd)
        request = replace(
            request,
            ledger_path=(
                request.ledger_path
                if "MODEL_DISPATCH_LOG" in os.environ
                else effective_root / ".beads" / "interactions.jsonl"
            ),
            availability_path=(
                request.availability_path
                if "DISPATCH_AVAILABILITY_PATH" in os.environ
                else effective_root
                / ".agentic-pr-dash"
                / f"{provider.value}-availability.json"
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


def _structured_stdin_payload(argv: list[str]) -> dict[str, object] | None:
    task_type = _option_value(argv, "--task-type")
    if not task_type:
        return None
    try:
        raw = sys.stdin.buffer.read(_STRUCTURED_STDIN_LIMIT + 1)
        if len(raw) > _STRUCTURED_STDIN_LIMIT or not raw.endswith(b"\0"):
            return None
        command_argv = [part.decode("utf-8") for part in raw[:-1].split(b"\0")]
    except (AttributeError, UnicodeDecodeError, OSError):
        return None
    if not command_argv or any("\0" in value for value in command_argv):
        return None

    exit_code_raw = _option_value(argv, "--exit-code")
    try:
        exit_code = int(exit_code_raw) if exit_code_raw is not None else None
    except ValueError:
        return None
    if exit_code is None:
        return None
    stderr = ""
    error_file = _option_value(argv, "--error-file")
    if error_file:
        try:
            if _error_file_reports_unavailable(Path(error_file)):
                stderr = "quota"
        except OSError:
            pass
    started_at = _option_value(argv, "--started-at-unix-nano")
    try:
        start_time_unix_nano = int(started_at) if started_at is not None else None
    except ValueError:
        return None
    payload: dict[str, object] = {
        "session_id": os.environ.get("CLAUDE_SESSION_ID", ""),
        "cwd": os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()),
        "tool_input": {"command": "<redacted>"},
        "tool_response": {"exit_code": exit_code, "stderr": stderr},
        "dispatch_telemetry": {
            "argv": command_argv,
            "task_type": task_type,
            "start_time_unix_nano": start_time_unix_nano,
        },
    }
    configured_model = _option_value(argv, "--configured-model")
    default_model = _option_value(argv, "--default-model")
    if configured_model is not None or default_model is not None:
        payload["dispatch_model_resolution"] = {
            "configured_model": configured_model,
            "default_model": default_model,
        }
    return payload


def _observation_from_request(
    request: DispatchHookRequest,
) -> DispatchObservation | None:
    structured = _structured_telemetry(request)
    if "dispatch_telemetry" in request.payload:
        if structured is None:
            return None
        response = request.payload.get("tool_response")
        if not isinstance(response, dict) or _exit_code(response) is None:
            return None
        outcome = _outcome(response)
        resolution = request.payload.get("dispatch_model_resolution")
        effective_model = resolve_provider_model(
            structured.requested_model,
            resolution,
            ignore_user_config=structured.ignore_user_config,
        )
        if structured.resolution_source is DispatchResolutionSource.UNAVAILABLE:
            structured = replace(
                structured,
                resolution_source=_adapter_resolution_source(
                    resolution, ignore_user_config=structured.ignore_user_config
                ),
            )
        verdict = response.get("review_verdict")
        if (
            not isinstance(verdict, dict)
            or outcome is not DispatchOutcome.SUCCESS
            or structured.task_type != "review"
        ):
            verdict = None
        emit_dispatch_span(
            structured,
            outcome=outcome,
            effective_model=effective_model,
            error_type=(
                f"process.exit_code.{_exit_code(response)}"
                if outcome is DispatchOutcome.FAILURE
                else (
                    "provider.unavailable"
                    if outcome is DispatchOutcome.UNAVAILABLE
                    else None
                )
            ),
        )
        return structured.to_dispatch_observation(
            outcome=outcome,
            effective_model=effective_model,
            review_verdict=dict(verdict) if verdict is not None else None,
        )

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
    direct_invocation = has_provider_invocation(command, request.provider)
    if declared is not None and not direct_invocation:
        declared = None
    task_type = (
        declared[0]
        if declared is not None
        else ("review" if _REVIEW_PATTERN.search(command) else "exec")
    )
    verdict = response.get("review_verdict")
    if (
        not isinstance(verdict, dict)
        or outcome is not DispatchOutcome.SUCCESS
        or not direct_invocation
    ):
        verdict = None
    requested_model = _requested_model(command)
    resolved_model = resolve_provider_model(
        requested_model,
        request.payload.get("dispatch_model_resolution"),
    )

    observation = DispatchObservation(
        provider=request.provider,
        source=request.source,
        session_id=str(request.payload.get("session_id") or ""),
        worktree_root=str(request.payload.get("cwd") or os.getcwd()),
        command=command,
        task_type=task_type,
        requested_model=requested_model,
        resolved_model=resolved_model,
        outcome=outcome,
        review_verdict=dict(verdict) if verdict is not None else None,
        classification_authority=(
            ClassificationAuthority.DECLARED
            if declared is not None
            else ClassificationAuthority.LEGACY_INFERRED
        ),
        classification_framework=declared[1] if declared is not None else None,
    )
    legacy_telemetry = DispatchTelemetry.from_legacy_observation(
        observation,
        parse_status=(
            DispatchParseStatus.LEGACY_PARSED
            if direct_invocation
            else DispatchParseStatus.AMBIGUOUS
        ),
    )
    emit_dispatch_span(
        legacy_telemetry,
        outcome=outcome,
        effective_model=resolved_model,
        error_type=(
            f"process.exit_code.{_exit_code(response)}"
            if outcome is DispatchOutcome.FAILURE
            else (
                "provider.unavailable"
                if outcome is DispatchOutcome.UNAVAILABLE
                else None
            )
        ),
    )
    return observation


def _structured_telemetry(
    request: DispatchHookRequest,
) -> DispatchTelemetry | None:
    metadata = request.payload.get("dispatch_telemetry")
    if not isinstance(metadata, dict):
        return None
    argv = metadata.get("argv")
    task_type = metadata.get("task_type")
    start_time_unix_nano = metadata.get("start_time_unix_nano")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(value, str) for value in argv)
        or not isinstance(task_type, str)
        or not task_type.strip()
        or not _valid_start_time(start_time_unix_nano)
    ):
        return None
    executable = argv[0].replace("\\", "/").rsplit("/", 1)[-1]
    if executable not in {
        request.provider.value,
        f"{request.provider.value}.exe",
    } or not _has_supported_subcommand(argv, request.provider):
        return None
    try:
        return DispatchTelemetry.from_argv(
            provider=request.provider,
            source=request.source,
            argv=argv,
            cwd=str(request.payload.get("cwd") or os.getcwd()),
            task_type=task_type.strip(),
            session_id=str(request.payload.get("session_id") or ""),
            environment={"CODEX_HOME": os.environ["CODEX_HOME"]}
            if "CODEX_HOME" in os.environ
            else None,
            start_time_unix_nano=start_time_unix_nano,
        )
    except (OSError, ValueError):
        return None


def _has_supported_subcommand(argv: list[str], provider: DispatchProvider) -> bool:
    if provider is DispatchProvider.OPENCODE:
        return len(argv) >= 2 and argv[1] == "run"
    if provider is not DispatchProvider.CODEX:
        return False
    option_tokens = argv[1 : argv.index("--")] if "--" in argv else argv[1:]
    if any(token in {"-h", "--help", "-V", "--version"} for token in option_tokens):
        return False
    value_options = {
        "--add-dir",
        "--ask-for-approval",
        "--cd",
        "--config",
        "--disable",
        "--enable",
        "--image",
        "--local-provider",
        "--model",
        "--profile",
        "--remote",
        "--remote-auth-token-env",
        "--sandbox",
        "-a",
        "-C",
        "-c",
        "-i",
        "-m",
        "-p",
        "-s",
    }
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in {"--image", "-i"}:
            return False
        if token in value_options:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token in {"exec", "e"}
    return False


def _adapter_resolution_source(
    resolution: object, *, ignore_user_config: bool = False
) -> DispatchResolutionSource:
    if not isinstance(resolution, dict):
        return DispatchResolutionSource.UNAVAILABLE
    configured = resolution.get("configured_model")
    if not ignore_user_config and isinstance(configured, str) and configured.strip():
        return DispatchResolutionSource.BASE_CONFIG
    default = resolution.get("default_model")
    if isinstance(default, str) and default.strip():
        return DispatchResolutionSource.TASK_ROUTING
    return DispatchResolutionSource.UNAVAILABLE


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


def has_provider_invocation(command: str, provider: DispatchProvider) -> bool:
    """Return whether a shell command executes the requested provider.

    Provider words passed to another executable are not invocations. Leading
    environment assignments, ``env`` and ``timeout`` prefixes, and the canonical
    ``cd <directory> && <provider>`` boundary are recognized.
    """
    executable = "codex" if provider is DispatchProvider.CODEX else "opencode"
    subcommand = "exec" if provider is DispatchProvider.CODEX else "run"
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    if "&&" in tokens:
        boundary = tokens.index("&&")
        prefix, tokens = tokens[:boundary], tokens[boundary + 1 :]
        if (
            not prefix
            or Path(prefix[0]).name != "cd"
            or any(re.fullmatch(r"[;&|<>\n]+", token) for token in prefix)
        ):
            return False
    tokens = _without_dev_null_stdin_redirect(tokens)
    if any(re.fullmatch(r"[;&|<>\n]+", token) for token in tokens):
        return False
    return _segment_invokes_provider(tokens, executable, subcommand)


def _without_dev_null_stdin_redirect(tokens: list[str]) -> list[str]:
    if len(tokens) >= 2 and tokens[-2:] == ["<", "/dev/null"]:
        return tokens[:-2]
    return tokens


def _segment_invokes_provider(
    tokens: list[str], executable: str, subcommand: str
) -> bool:
    index = 0
    while index < len(tokens) and _is_environment_assignment(tokens[index]):
        index += 1
    if index < len(tokens) and Path(tokens[index]).name == "env":
        index = _skip_env_prefix(tokens, index + 1)
    if index < len(tokens) and Path(tokens[index]).name == "timeout":
        index = _skip_timeout_prefix(tokens, index + 1)
    return (
        index + 1 < len(tokens)
        and Path(tokens[index]).name == executable
        and tokens[index + 1] == subcommand
    )


def _is_environment_assignment(token: str) -> bool:
    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token) is not None


def _skip_env_prefix(tokens: list[str], index: int) -> int:
    options_with_value = {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}
    while index < len(tokens):
        token = tokens[index]
        if _is_environment_assignment(token):
            index += 1
        elif token in options_with_value:
            index += 2
        elif token.startswith("-"):
            index += 1
        else:
            break
    return index


def _skip_timeout_prefix(tokens: list[str], index: int) -> int:
    options_with_value = {"-k", "--kill-after", "-s", "--signal"}
    while index < len(tokens) and tokens[index].startswith("-"):
        token = tokens[index]
        index += 2 if token in options_with_value else 1
    return min(index + 1, len(tokens))


def resolve_provider_model(
    requested_model: str | None,
    resolution: object,
    *,
    ignore_user_config: bool = False,
) -> str | None:
    """Resolve model attribution supplied by a repository adapter.

    ``dispatch_model_resolution`` is an optional payload mapping with
    ``configured_model`` and ``default_model`` string candidates. An explicit
    provider CLI model always wins.
    """
    if requested_model:
        return requested_model
    if not isinstance(resolution, dict):
        return None
    keys = (
        ("default_model",)
        if ignore_user_config
        else ("configured_model", "default_model")
    )
    for key in keys:
        candidate = resolution.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


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


def _exit_code(response: dict[object, object]) -> int | None:
    exit_code = response.get("exit_code", response.get("exitCode"))
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return None
    return exit_code


def _valid_start_time(value: object) -> bool:
    return value is None or (
        isinstance(value, int)
        and not isinstance(value, bool)
        and _MIN_PLAUSIBLE_UNIX_NANO <= value <= time.time_ns()
    )


def _error_file_reports_unavailable(path: Path) -> bool:
    carry = ""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        while chunk := handle.read(_ERROR_SCAN_CHUNK_SIZE):
            window = carry + chunk
            if _UNAVAILABLE_PATTERN.search(window):
                return True
            carry = window[-_ERROR_SCAN_OVERLAP:]
    return False


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
