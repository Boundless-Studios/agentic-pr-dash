from __future__ import annotations

import json

import pytest

from agentic_pr_dash.dispatch_observation import DispatchProvider, DispatchSource
from agentic_pr_dash.dispatch_telemetry import (
    MAX_ARG_COUNT,
    MAX_STRING_LENGTH,
    DispatchTelemetry,
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
    assert "sk-never-record" not in serialized
    assert "ARBITRARY" not in serialized
    assert "private" not in serialized


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
