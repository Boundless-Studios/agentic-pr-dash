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


class DispatchResolutionSource(str, Enum):
    """Authority that selected the attributed model."""

    EXPLICIT_FLAG = "explicit_flag"
    CONFIG_OVERRIDE = "config_override"
    PROFILE = "profile"
    TASK_ROUTING = "task_routing"
    BASE_CONFIG = "base_config"
    UNAVAILABLE = "unavailable"


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
    argv: tuple[str, ...] = field(repr=False)
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
    resolution_source: DispatchResolutionSource
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
            resolution_source=parsed.resolution_source,
        )

    def otel_attributes(self) -> dict[str, OtelAttribute]:
        """Return sanitized standard and Gaia-extension OTel attributes."""

        attributes: dict[str, OtelAttribute] = {
            "process.command": _executable_name(self.argv[0]),
            "process.command_args": self.sanitized_argv,
            "process.args_count": len(self.argv),
            "gen_ai.provider.name": _PROVIDER_NAMES[self.provider],
            "gaia.dispatch.source": self.source.value,
            "gaia.dispatch.id": self.dispatch_id,
            "gaia.dispatch.task_type": self.task_type,
            "gaia.dispatch.worktree": self.cwd,
            "gaia.dispatch.parse_status": self.parse_status.value,
            "gaia.dispatch.ignore_user_config": self.ignore_user_config,
            "gaia.dispatch.resolution_source": self.resolution_source.value,
        }
        if Path(self.argv[0]).is_absolute():
            attributes["process.executable.path"] = self.argv[0]
        optional = {
            "gen_ai.request.model": self.requested_model,
            "process.environment_variable.CODEX_HOME": self.codex_home,
            "gaia.dispatch.profile": self.requested_profile,
            "gaia.dispatch.reasoning_effort": self.reasoning_effort,
            "gaia.dispatch.sandbox_mode": self.sandbox_mode,
            "gaia.dispatch.approval_mode": self.approval_mode,
        }
        attributes.update({key: value for key, value in optional.items() if value})
        return {key: _bound_attribute(value) for key, value in attributes.items()}

    @classmethod
    def from_legacy_observation(
        cls,
        observation: DispatchObservation,
        *,
        parse_status: DispatchParseStatus,
    ) -> DispatchTelemetry:
        """Represent one already-parsed legacy observation without raw text."""

        executable = observation.provider.value
        return cls(
            provider=observation.provider,
            source=observation.source,
            argv=(executable, "<legacy:redacted>"),
            sanitized_argv=(executable, "<legacy:redacted>"),
            cwd=observation.worktree_root,
            task_type=observation.task_type,
            session_id=observation.session_id,
            requested_model=observation.requested_model,
            requested_profile=None,
            reasoning_effort=None,
            codex_home=None,
            sandbox_mode=None,
            approval_mode=None,
            ignore_user_config=False,
            start_time_unix_nano=None,
            resolution_source=DispatchResolutionSource.UNAVAILABLE,
            parse_status=parse_status,
            observed_at=observation.observed_at,
        )

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
            session_id=_truncate(self.session_id),
            worktree_root=_truncate(self.cwd),
            command="<redacted>",
            task_type=_truncate(self.task_type),
            requested_model=(
                _truncate(self.requested_model) if self.requested_model else None
            ),
            resolved_model=(
                _truncate(effective_model or self.requested_model)
                if effective_model or self.requested_model
                else None
            ),
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
    resolution_source: DispatchResolutionSource


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
    resolution_source = DispatchResolutionSource.UNAVAILABLE
    index = 2
    while index < len(argv):
        token = argv[index]
        if token == "--":
            sanitized.append(token)
            sanitized.extend("<redacted:payload>" for _value in argv[index + 1 :])
            break
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
                resolution_source = DispatchResolutionSource.EXPLICIT_FLAG
            elif option == "--profile":
                profile = _truncate(attached_value)
                if resolution_source is DispatchResolutionSource.UNAVAILABLE:
                    resolution_source = DispatchResolutionSource.PROFILE
            elif option == "--sandbox":
                sandbox_mode = _truncate(attached_value)
            elif option == "--ask-for-approval":
                approval_mode = _truncate(attached_value)
            elif option == "--config":
                key, found, config_value = attached_value.partition("=")
                if found and key == "model":
                    model = _truncate(config_value)
                    resolution_source = DispatchResolutionSource.CONFIG_OVERRIDE
                elif found and key == "model_reasoning_effort":
                    reasoning_effort = _truncate(config_value)
            index += 1
            continue
        if separator and token.startswith("-"):
            sanitized.append(f"{option}=<redacted:option>")
            index += 1
            continue
        if (
            token in _VALUE_OPTIONS
            and index + 1 < len(argv)
            and not argv[index + 1].startswith("-")
        ):
            value = argv[index + 1]
            sanitized.extend((token, _truncate(_safe_option_value(token, value))))
            if token in {"--model", "-m"}:
                model = _truncate(value)
                resolution_source = DispatchResolutionSource.EXPLICIT_FLAG
            elif token in {"--profile", "-p"}:
                profile = _truncate(value)
                if resolution_source is DispatchResolutionSource.UNAVAILABLE:
                    resolution_source = DispatchResolutionSource.PROFILE
            elif token == "--sandbox":
                sandbox_mode = _truncate(value)
            elif token == "--ask-for-approval":
                approval_mode = _truncate(value)
            elif token in {"--config", "-c"}:
                key, found, config_value = value.partition("=")
                if found and key == "model":
                    model = _truncate(config_value)
                    resolution_source = DispatchResolutionSource.CONFIG_OVERRIDE
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
        resolution_source,
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


def _executable_name(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def _bound_attribute(value: OtelAttribute) -> OtelAttribute:
    if isinstance(value, str):
        return _truncate(value)
    if isinstance(value, tuple):
        return tuple(_truncate(item) for item in value)
    return value


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
        attributes["gaia.dispatch.effective_model"] = _truncate(effective_model)
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
