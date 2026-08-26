"""OpenTelemetry semantic profile for structured coding-agent dispatches."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode, Tracer

from agentic_pr_dash.dispatch_observation import (
    ClassificationAuthority,
    DispatchObservation,
    DispatchOutcome,
    DispatchProvider,
    DispatchSource,
)


class DispatchParseStatus(str, Enum):
    """How dispatch metadata reached the observer."""

    STRUCTURED = "structured"
    LEGACY_PARSED = "legacy_parsed"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


OtelAttribute = str | int | bool | tuple[str, ...]

MAX_ARG_COUNT = 128
MAX_STRING_LENGTH = 1024

_PROVIDER_NAMES = {
    DispatchProvider.CODEX: "openai",
    DispatchProvider.OPENCODE: "opencode",
    DispatchProvider.CLAUDE: "anthropic",
}
_VALUE_OPTIONS = {
    "--add-dir",
    "--ask-for-approval",
    "--config",
    "--model",
    "--profile",
    "--sandbox",
    "-c",
    "-m",
    "-p",
}
_SAFE_CONFIG_KEYS = {"model", "model_reasoning_effort"}


@dataclass(frozen=True, slots=True)
class DispatchTelemetry:
    """One structured dispatch expressed as OpenTelemetry attributes."""

    provider: DispatchProvider
    source: DispatchSource
    argv: tuple[str, ...]
    sanitized_argv: tuple[str, ...]
    cwd: str
    task_type: str
    session_id: str
    requested_model: str | None
    requested_profile: str | None
    reasoning_effort: str | None
    codex_home: str | None
    sandbox_mode: str | None
    approval_mode: str | None
    ignore_user_config: bool
    start_time_unix_nano: int | None
    parse_status: DispatchParseStatus = DispatchParseStatus.STRUCTURED
    dispatch_id: str = field(default_factory=lambda: str(uuid4()))
    observed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @classmethod
    def from_argv(
        cls,
        *,
        provider: DispatchProvider,
        source: DispatchSource,
        argv: Sequence[str],
        cwd: str,
        task_type: str,
        session_id: str,
        environment: Mapping[str, str] | None = None,
        start_time_unix_nano: int | None = None,
    ) -> DispatchTelemetry:
        """Build telemetry from an argv sequence without interpreting shell text."""

        exact_argv = tuple(argv)
        parsed = _sanitize_and_resolve(exact_argv)
        return cls(
            provider=provider,
            source=source,
            argv=exact_argv,
            sanitized_argv=parsed.sanitized_argv,
            cwd=cwd,
            task_type=task_type,
            session_id=session_id,
            requested_model=parsed.model,
            requested_profile=parsed.profile,
            reasoning_effort=parsed.reasoning_effort,
            codex_home=(
                _truncate((environment or {})["CODEX_HOME"])
                if "CODEX_HOME" in (environment or {})
                else None
            ),
            sandbox_mode=parsed.sandbox_mode,
            approval_mode=parsed.approval_mode,
            ignore_user_config=parsed.ignore_user_config,
            start_time_unix_nano=start_time_unix_nano,
        )

    def otel_attributes(self) -> dict[str, OtelAttribute]:
        """Return sanitized standard and Gaia-extension OTel attributes."""

        attributes: dict[str, OtelAttribute] = {
            "process.command": Path(self.argv[0]).name,
            "process.executable.path": self.argv[0],
            "process.command_args": self.sanitized_argv,
            "process.args_count": len(self.argv),
            "gen_ai.provider.name": _PROVIDER_NAMES[self.provider],
            "gaia.dispatch.source": self.source.value,
            "gaia.dispatch.id": self.dispatch_id,
            "gaia.dispatch.task_type": self.task_type,
            "gaia.dispatch.worktree": self.cwd,
            "gaia.dispatch.parse_status": self.parse_status.value,
            "gaia.dispatch.ignore_user_config": self.ignore_user_config,
        }
        optional = {
            "gen_ai.request.model": self.requested_model,
            "process.environment_variable.CODEX_HOME": self.codex_home,
            "gaia.dispatch.profile": self.requested_profile,
            "gaia.dispatch.reasoning_effort": self.reasoning_effort,
            "gaia.dispatch.sandbox_mode": self.sandbox_mode,
            "gaia.dispatch.approval_mode": self.approval_mode,
        }
        attributes.update({key: value for key, value in optional.items() if value})
        return attributes

    def to_dispatch_observation(
        self,
        *,
        outcome: DispatchOutcome,
        effective_model: str | None,
        review_verdict: dict[str, object] | None = None,
    ) -> DispatchObservation:
        """Project canonical telemetry into the migration JSONL contract."""

        return DispatchObservation(
            provider=self.provider,
            source=self.source,
            session_id=self.session_id,
            worktree_root=self.cwd,
            command="<redacted>",
            task_type=self.task_type,
            requested_model=self.requested_model,
            resolved_model=effective_model or self.requested_model,
            outcome=outcome,
            review_verdict=review_verdict,
            classification_authority=ClassificationAuthority.DECLARED,
            classification_framework="coding-agent/v1",
            observed_at=self.observed_at,
        )


@dataclass(frozen=True, slots=True)
class _ParsedArgv:
    sanitized_argv: tuple[str, ...]
    model: str | None
    profile: str | None
    reasoning_effort: str | None
    sandbox_mode: str | None
    approval_mode: str | None
    ignore_user_config: bool


def _sanitize_and_resolve(argv: tuple[str, ...]) -> _ParsedArgv:
    if not argv:
        raise ValueError("argv must contain an executable")

    sanitized = list(argv[:2])
    model = None
    profile = None
    reasoning_effort = None
    sandbox_mode = None
    approval_mode = None
    ignore_user_config = False
    index = 2
    while index < len(argv):
        token = argv[index]
        option, separator, attached_value = token.partition("=")
        if separator and option in {
            "--ask-for-approval",
            "--config",
            "--model",
            "--profile",
            "--sandbox",
        }:
            sanitized.append(
                _truncate(
                    f"{option}={_safe_option_value(option, attached_value)}"
                    if option == "--config"
                    else token
                )
            )
            if option == "--model":
                model = _truncate(attached_value)
            elif option == "--profile":
                profile = _truncate(attached_value)
            elif option == "--sandbox":
                sandbox_mode = _truncate(attached_value)
            elif option == "--ask-for-approval":
                approval_mode = _truncate(attached_value)
            elif option == "--config":
                key, found, config_value = attached_value.partition("=")
                if found and key == "model":
                    model = _truncate(config_value)
                elif found and key == "model_reasoning_effort":
                    reasoning_effort = _truncate(config_value)
            index += 1
            continue
        if separator and token.startswith("-"):
            sanitized.append(f"{option}=<redacted:option>")
            index += 1
            continue
        if token in _VALUE_OPTIONS and index + 1 < len(argv):
            value = argv[index + 1]
            sanitized.extend((token, _truncate(_safe_option_value(token, value))))
            if token in {"--model", "-m"}:
                model = _truncate(value)
            elif token in {"--profile", "-p"}:
                profile = _truncate(value)
            elif token == "--sandbox":
                sandbox_mode = _truncate(value)
            elif token == "--ask-for-approval":
                approval_mode = _truncate(value)
            elif token in {"--config", "-c"}:
                key, found, config_value = value.partition("=")
                if found and key == "model":
                    model = _truncate(config_value)
                elif found and key == "model_reasoning_effort":
                    reasoning_effort = _truncate(config_value)
            index += 2
            continue
        if token.startswith("-"):
            sanitized.append(_truncate(token))
            if token == "--ignore-user-config":
                ignore_user_config = True
        else:
            sanitized.append("<redacted:payload>")
        index += 1
    bounded = sanitized[:MAX_ARG_COUNT]
    if len(sanitized) > MAX_ARG_COUNT:
        bounded.append(f"<truncated:{len(sanitized) - MAX_ARG_COUNT}-args>")
    return _ParsedArgv(
        tuple(bounded),
        model,
        profile,
        reasoning_effort,
        sandbox_mode,
        approval_mode,
        ignore_user_config,
    )


def _safe_option_value(option: str, value: str) -> str:
    if option not in {"--config", "-c"}:
        return value
    key, separator, config_value = value.partition("=")
    if not separator or key not in _SAFE_CONFIG_KEYS:
        return f"{key}=<redacted:config>"
    return f"{key}={config_value}"


def _truncate(value: str) -> str:
    marker = "<truncated>"
    if len(value) <= MAX_STRING_LENGTH:
        return value
    return value[: MAX_STRING_LENGTH - len(marker)] + marker


def emit_dispatch_span(
    telemetry: DispatchTelemetry,
    *,
    outcome: DispatchOutcome,
    effective_model: str | None,
    error_type: str | None = None,
    tracer: Tracer | None = None,
    end_time_unix_nano: int | None = None,
) -> None:
    """Emit one completed dispatch span; a missing SDK remains a safe no-op."""

    attributes = telemetry.otel_attributes()
    if effective_model:
        attributes["gaia.dispatch.effective_model"] = effective_model
    if error_type:
        attributes["error.type"] = error_type
    dispatch_tracer = tracer or trace.get_tracer("agentic_pr_dash.dispatch")
    span = dispatch_tracer.start_span(
        "agent.dispatch",
        attributes=attributes,
        start_time=telemetry.start_time_unix_nano,
    )
    if outcome is not DispatchOutcome.SUCCESS:
        span.set_status(Status(StatusCode.ERROR))
    span.end(end_time=end_time_unix_nano)
