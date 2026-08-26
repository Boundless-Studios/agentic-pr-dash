from __future__ import annotations

import json
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from agentic_pr_dash.dispatch_observation import (
    ClassificationAuthority,
    DispatchOutcome,
    DispatchProvider,
    DispatchSource,
)
from agentic_pr_dash.dispatch_telemetry import (
    MAX_ARG_COUNT,
    MAX_STRING_LENGTH,
    DispatchResolutionSource,
    DispatchTelemetry,
    emit_dispatch_span,
)


def _telemetry(*argv: str) -> DispatchTelemetry:
    return DispatchTelemetry.from_argv(
        provider=DispatchProvider.CODEX,
        source=DispatchSource.DETACHED_RUNNER,
        argv=argv,
        cwd="/repo/worktree",
        task_type="review",
        session_id="session-1",
    )


def test_structured_codex_argv_uses_otel_semantic_attributes() -> None:
    telemetry = _telemetry(
        "/usr/local/bin/codex",
        "exec",
        "--model",
        "gpt-5.6-sol",
        "--profile",
        "maintenance",
        "--config",
        "model_reasoning_effort=high",
        "review this private diff",
    )

    attributes = telemetry.otel_attributes()

    assert attributes["process.command"] == "codex"
    assert attributes["process.executable.path"] == "/usr/local/bin/codex"
    assert attributes["process.args_count"] == 9
    assert attributes["process.command_args"] == (
        "/usr/local/bin/codex",
        "exec",
        "--model",
        "gpt-5.6-sol",
        "--profile",
        "maintenance",
        "--config",
        "model_reasoning_effort=high",
        "<redacted:payload>",
    )
    assert attributes["gen_ai.provider.name"] == "openai"
    assert attributes["gen_ai.request.model"] == "gpt-5.6-sol"
    assert attributes["gaia.dispatch.source"] == "detached_runner"
    assert attributes["gaia.dispatch.task_type"] == "review"
    assert attributes["gaia.dispatch.profile"] == "maintenance"
    assert attributes["gaia.dispatch.reasoning_effort"] == "high"
    assert attributes["gaia.dispatch.worktree"] == "/repo/worktree"
    assert attributes["gaia.dispatch.parse_status"] == "structured"


@pytest.mark.parametrize(
    ("argv", "expected_model"),
    [
        (("codex", "exec", "--model=gpt-5.6-sol", "prompt"), "gpt-5.6-sol"),
        (("opencode", "run", "-m", "openai/gpt-5", "prompt"), "openai/gpt-5"),
    ],
)
def test_model_option_forms_are_resolved_once(
    argv: tuple[str, ...], expected_model: str
) -> None:
    provider = (
        DispatchProvider.CODEX if argv[0] == "codex" else DispatchProvider.OPENCODE
    )

    telemetry = DispatchTelemetry.from_argv(
        provider=provider,
        source=DispatchSource.DETACHED_RUNNER,
        argv=argv,
        cwd="/repo",
        task_type="exec",
        session_id="session-1",
    )

    assert telemetry.requested_model == expected_model
    assert telemetry.resolution_source is DispatchResolutionSource.EXPLICIT_FLAG


def test_config_model_has_distinct_resolution_source() -> None:
    telemetry = _telemetry(
        "codex", "exec", "--config", "model=gpt-5.6-sol", "prompt"
    )

    assert telemetry.requested_model == "gpt-5.6-sol"
    assert telemetry.otel_attributes()[
        "gaia.dispatch.resolution_source"
    ] == "config_override"


@pytest.mark.parametrize(
    ("option", "expected"),
    [
        ("-mgpt-5", ("requested_model", "gpt-5")),
        ("-pmaintenance", ("requested_profile", "maintenance")),
        ("-cmodel=gpt-5", ("requested_model", "gpt-5")),
    ],
)
def test_attached_short_option_values_are_resolved(
    option: str, expected: tuple[str, str]
) -> None:
    telemetry = _telemetry("codex", "exec", option, "prompt")

    assert getattr(telemetry, expected[0]) == expected[1]


def test_toml_config_model_is_normalized() -> None:
    telemetry = _telemetry("codex", "exec", "-c", 'model="gpt-5"', "prompt")

    assert telemetry.requested_model == "gpt-5"


def test_profile_is_preserved_as_resolution_source_for_adapter_model() -> None:
    telemetry = _telemetry("codex", "exec", "--profile", "maintenance", "prompt")

    assert telemetry.requested_model is None
    assert telemetry.resolution_source is DispatchResolutionSource.PROFILE


def test_missing_option_value_and_double_dash_do_not_change_routing() -> None:
    missing = _telemetry(
        "codex", "exec", "--model", "--profile", "maintenance", "prompt"
    )
    terminated = _telemetry(
        "codex", "exec", "--", "--model", "secret-payload-model"
    )

    assert missing.requested_model is None
    assert missing.requested_profile == "maintenance"
    assert terminated.requested_model is None
    assert terminated.sanitized_argv[-2:] == (
        "<redacted:payload>",
        "<redacted:payload>",
    )


def test_sensitive_config_and_prompt_values_never_serialize() -> None:
    secret = "sk-private-token"
    telemetry = _telemetry(
        "codex",
        "exec",
        "--config",
        f"openai_api_key={secret}",
        f"prompt includes {secret}",
    )

    serialized = json.dumps(telemetry.otel_attributes())

    assert secret not in serialized
    assert "openai_api_key=<redacted:config>" in serialized
    assert "<redacted:payload>" in serialized


def test_environment_is_allowlisted_instead_of_copied() -> None:
    telemetry = DispatchTelemetry.from_argv(
        provider=DispatchProvider.CODEX,
        source=DispatchSource.DETACHED_RUNNER,
        argv=("codex", "exec", "prompt"),
        cwd="/repo",
        task_type="exec",
        session_id="session-1",
        environment={
            "CODEX_HOME": "/safe/codex-home",
            "OPENAI_API_KEY": "sk-never-record",
            "ARBITRARY": "private",
        },
    )

    serialized = json.dumps(telemetry.otel_attributes())

    assert telemetry.codex_home == "/safe/codex-home"
    assert telemetry.otel_attributes()[
        "process.environment_variable.CODEX_HOME"
    ] == "/safe/codex-home"
    assert "sk-never-record" not in serialized
    assert "ARBITRARY" not in serialized
    assert "private" not in serialized


def test_unknown_attached_option_value_is_redacted() -> None:
    secret = "secret-attached-value"
    telemetry = _telemetry(
        "codex",
        "exec",
        f"--api-key={secret}",
        "--config=model_reasoning_effort=xhigh",
        "prompt",
    )

    attributes = telemetry.otel_attributes()
    serialized = json.dumps(attributes)

    assert secret not in serialized
    assert "--api-key=<redacted:option>" in serialized
    assert attributes["gaia.dispatch.reasoning_effort"] == "xhigh"


def test_execution_policy_options_are_structured_attributes() -> None:
    telemetry = _telemetry(
        "codex",
        "exec",
        "--sandbox",
        "workspace-write",
        "--ask-for-approval=on-request",
        "--ignore-user-config",
        "prompt",
    )

    attributes = telemetry.otel_attributes()

    assert attributes["gaia.dispatch.sandbox_mode"] == "workspace-write"
    assert attributes["gaia.dispatch.approval_mode"] == "on-request"
    assert attributes["gaia.dispatch.ignore_user_config"] is True


def test_short_sandbox_option_is_structured() -> None:
    telemetry = _telemetry("codex", "exec", "-s", "workspace-write", "prompt")

    assert telemetry.sandbox_mode == "workspace-write"


def test_short_approval_option_is_structured() -> None:
    telemetry = _telemetry("codex", "exec", "-a", "never", "prompt")

    assert telemetry.approval_mode == "never"


def test_opencode_session_option_is_redacted_without_codex_sandbox_semantics() -> None:
    session_id = "private-session-id"
    telemetry = DispatchTelemetry.from_argv(
        provider=DispatchProvider.OPENCODE,
        source=DispatchSource.DETACHED_RUNNER,
        argv=("opencode", "run", "-s", session_id, "prompt"),
        cwd="/repo",
        task_type="exec",
        session_id="session-1",
    )

    assert telemetry.sandbox_mode is None
    assert session_id not in json.dumps(telemetry.otel_attributes())
    assert telemetry.sanitized_argv == (
        "opencode",
        "run",
        "-s",
        "<redacted:option>",
        "<redacted:payload>",
    )


@pytest.mark.parametrize("option", ["-i/private/image.png", "-o/private/result.txt"])
def test_attached_file_option_values_are_redacted(option: str) -> None:
    telemetry = _telemetry("codex", "exec", option, "prompt")

    serialized = json.dumps(telemetry.otel_attributes())
    assert "/private/" not in serialized
    assert telemetry.sanitized_argv[2] in {
        "-i<redacted:option>",
        "-o<redacted:option>",
    }


@pytest.mark.parametrize(
    ("option", "value"),
    [("-C", "../other-repo"), ("--cd", "/other/repo")],
)
def test_working_root_override_replaces_launch_cwd(option: str, value: str) -> None:
    telemetry = _telemetry("codex", "exec", option, value, "prompt")

    expected = (
        str(Path("/repo/worktree", value).resolve())
        if not value.startswith("/")
        else value
    )
    assert telemetry.cwd == expected


def test_telemetry_bounds_argument_count_and_values_deterministically() -> None:
    long_model = "m" * (MAX_STRING_LENGTH + 10)
    argv = ("codex", "exec", "--model", long_model) + tuple(
        f"payload-{index}" for index in range(MAX_ARG_COUNT)
    )

    attributes = _telemetry(*argv).otel_attributes()
    command_args = attributes["process.command_args"]

    assert attributes["process.args_count"] == len(argv)
    assert isinstance(command_args, tuple)
    assert len(command_args) == MAX_ARG_COUNT + 1
    assert command_args[-1] == f"<truncated:{len(argv) - MAX_ARG_COUNT}-args>"
    assert len(command_args[3]) <= MAX_STRING_LENGTH
    assert command_args[3].endswith("<truncated>")


def test_completed_dispatch_emits_one_otel_span() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = DispatchTelemetry.from_argv(
        provider=DispatchProvider.CODEX,
        source=DispatchSource.DETACHED_RUNNER,
        argv=("codex", "exec", "--model", "gpt-5.6-sol", "private prompt"),
        cwd="/repo",
        task_type="review",
        session_id="session-1",
        start_time_unix_nano=1_000_000_000,
    )

    emit_dispatch_span(
        telemetry,
        outcome=DispatchOutcome.FAILURE,
        effective_model="gpt-5.6-sol",
        error_type="process.exit_code.1",
        tracer=provider.get_tracer("test"),
        end_time_unix_nano=2_000_000_000,
    )

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "agent.dispatch"
    assert spans[0].status.status_code.name == "ERROR"
    assert spans[0].attributes["error.type"] == "process.exit_code.1"
    assert spans[0].attributes["gen_ai.request.model"] == "gpt-5.6-sol"
    assert spans[0].attributes["gaia.dispatch.effective_model"] == "gpt-5.6-sol"
    assert spans[0].start_time == 1_000_000_000
    assert spans[0].end_time == 2_000_000_000


def test_relative_executable_omits_path_and_all_string_attributes_are_bounded() -> None:
    telemetry = DispatchTelemetry.from_argv(
        provider=DispatchProvider.CODEX,
        source=DispatchSource.DETACHED_RUNNER,
        argv=("codex", "exec", "prompt"),
        cwd="w" * (MAX_STRING_LENGTH + 20),
        task_type="t" * (MAX_STRING_LENGTH + 20),
        session_id="session-1",
    )

    attributes = telemetry.otel_attributes()

    assert "process.executable.path" not in attributes
    assert all(
        len(value) <= MAX_STRING_LENGTH
        for value in attributes.values()
        if isinstance(value, str)
    )


def test_dispatch_projects_to_existing_redacted_observation() -> None:
    telemetry = _telemetry(
        "codex", "exec", "--model", "gpt-5.6-sol", "private prompt"
    )

    observation = telemetry.to_dispatch_observation(
        outcome=DispatchOutcome.SUCCESS,
        effective_model="gpt-5.6-sol",
    )

    assert observation.command == "<redacted>"
    assert observation.requested_model == "gpt-5.6-sol"
    assert observation.resolved_model == "gpt-5.6-sol"
    assert observation.classification_authority is ClassificationAuthority.DECLARED
    assert observation.classification_framework == "coding-agent/v1"
    serialized = json.dumps(observation.to_persisted_dict())
    assert "private prompt" not in serialized


def test_compatibility_projection_bounds_string_fields() -> None:
    oversized = "x" * (MAX_STRING_LENGTH + 20)
    telemetry = DispatchTelemetry.from_argv(
        provider=DispatchProvider.CODEX,
        source=DispatchSource.DETACHED_RUNNER,
        argv=("codex", "exec", "prompt"),
        cwd=oversized,
        task_type=oversized,
        session_id=oversized,
    )

    observation = telemetry.to_dispatch_observation(
        outcome=DispatchOutcome.SUCCESS,
        effective_model=oversized,
    )

    persisted = observation.to_persisted_dict()
    for key in ("session_id", "worktree_root", "task_type", "resolved_model"):
        assert len(persisted[key]) <= MAX_STRING_LENGTH
