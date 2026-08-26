from __future__ import annotations

import json
import sys
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path

import pytest

from agentic_pr_dash.codex_hooks import (
    run_codex_dispatch_logger,
    run_opencode_dispatch_logger,
)
from agentic_pr_dash.codex_hooks.dispatch_runner import (
    DispatchHookRequest,
    has_provider_invocation,
    observation_from_agent_payload,
    run_dispatch_hook,
)
from agentic_pr_dash.dispatch_observation import (
    DispatchObservation,
    DispatchOutcome,
    DispatchProvider,
    DispatchSource,
)


def _request(
    tmp_path: Path,
    *,
    provider: DispatchProvider,
    command: str,
    response: dict[str, object] | None = None,
    source: DispatchSource = DispatchSource.INTERACTIVE_HOOK,
    classification: dict[str, str] | None = None,
    model_resolution: dict[str, str] | None = None,
) -> DispatchHookRequest:
    payload: dict[str, object] = {
        "session_id": "session-1",
        "cwd": "/repo/wt",
        "tool_input": {"command": command},
        "tool_response": response or {"exit_code": 0},
    }
    if classification is not None:
        payload["dispatch_classification"] = classification
    if model_resolution is not None:
        payload["dispatch_model_resolution"] = model_resolution
    return DispatchHookRequest(
        provider=provider,
        source=source,
        payload=payload,
        ledger_path=tmp_path / "dispatch.jsonl",
        availability_path=tmp_path / "availability.json",
    )


@pytest.mark.parametrize(
    ("provider", "command"),
    [
        (DispatchProvider.CODEX, "codex exec review"),
        (DispatchProvider.CODEX, "OPENAI_API_KEY=secret codex exec review"),
        (DispatchProvider.CODEX, "cd /repo && codex exec review"),
        (DispatchProvider.OPENCODE, "timeout 900 opencode run review"),
    ],
)
def test_provider_invocation_accepts_supported_shell_prefixes(
    provider: DispatchProvider, command: str
) -> None:
    assert has_provider_invocation(command, provider)


@pytest.mark.parametrize(
    ("provider", "command"),
    [
        (DispatchProvider.CODEX, "echo codex exec"),
        (DispatchProvider.CODEX, "env FOO=1 grep codex exec"),
        (DispatchProvider.OPENCODE, "printf 'opencode run'"),
    ],
)
def test_provider_invocation_rejects_provider_words_as_arguments(
    provider: DispatchProvider, command: str
) -> None:
    assert not has_provider_invocation(command, provider)


def test_false_positive_command_cannot_supply_declared_policy_or_verdict(
    tmp_path: Path,
) -> None:
    result = run_dispatch_hook(
        _request(
            tmp_path,
            provider=DispatchProvider.CODEX,
            command="echo codex exec",
            classification={"task_type": "review", "framework": "coding-agent/v1"},
            response={"exit_code": 0, "review_verdict": {"findings": []}},
        )
    )

    assert result.observation is not None
    assert result.observation.classification_authority.value == "legacy_inferred"
    assert result.observation.review_verdict is None


@pytest.mark.parametrize(
    ("resolution", "expected"),
    [
        (
            {
                "configured_model": "gpt-5.6-sol",
                "default_model": "codex-default",
            },
            "gpt-5.6-sol",
        ),
        ({"configured_model": "", "default_model": "codex-default"}, "codex-default"),
    ],
)
def test_flagless_dispatch_resolves_adapter_model_candidates(
    tmp_path: Path, resolution: dict[str, str], expected: str
) -> None:
    result = run_dispatch_hook(
        _request(
            tmp_path,
            provider=DispatchProvider.CODEX,
            command="codex exec review",
            model_resolution=resolution,
        )
    )

    assert result.observation is not None
    assert result.observation.requested_model is None
    assert result.observation.resolved_model == expected


def test_explicit_model_wins_over_adapter_resolution(tmp_path: Path) -> None:
    result = run_dispatch_hook(
        _request(
            tmp_path,
            provider=DispatchProvider.OPENCODE,
            command="opencode run --model explicit review",
            model_resolution={
                "configured_model": "configured",
                "default_model": "provider-default",
            },
        )
    )

    assert result.observation is not None
    assert result.observation.requested_model == "explicit"
    assert result.observation.resolved_model == "explicit"


@pytest.mark.parametrize(
    ("provider", "command", "model"),
    [
        (
            DispatchProvider.CODEX,
            "codex exec --model gpt-5.6-sol review",
            "gpt-5.6-sol",
        ),
        (
            DispatchProvider.OPENCODE,
            "opencode run -m openai/gpt-5 review changes",
            "openai/gpt-5",
        ),
    ],
)
def test_interactive_provider_dispatch_is_normalized_and_persisted(
    tmp_path: Path,
    provider: DispatchProvider,
    command: str,
    model: str,
) -> None:
    result = run_dispatch_hook(_request(tmp_path, provider=provider, command=command))

    assert result.observation is not None
    assert result.observation.provider is provider
    assert result.observation.source is DispatchSource.INTERACTIVE_HOOK
    assert result.observation.requested_model == model
    assert result.observation.resolved_model == model
    assert result.observation.outcome is DispatchOutcome.SUCCESS
    persisted = json.loads((tmp_path / "dispatch.jsonl").read_text(encoding="utf-8"))
    assert persisted == result.observation.to_persisted_dict()


def test_persisted_dispatch_omits_raw_command_content(tmp_path: Path) -> None:
    secret = "sk-sensitive-inline-token"
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command=f"OPENAI_API_KEY={secret} codex exec review embedded-diff",
        classification={"task_type": "review", "framework": "coding-agent/v1"},
    )

    result = run_dispatch_hook(request)

    assert result.observation is not None
    assert secret in result.observation.command
    persisted_text = request.ledger_path.read_text(encoding="utf-8")
    persisted = json.loads(persisted_text)
    assert secret not in persisted_text
    assert persisted["command"] == "<redacted>"
    assert persisted["provider"] == "codex"
    assert persisted["task_type"] == "review"
    assert DispatchObservation.from_dict(persisted).command == "<redacted>"


def test_structured_dispatch_is_authoritative_over_raw_command(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command="codex exec --model wrong-model legacy prompt",
    )
    request.payload["dispatch_telemetry"] = {
        "argv": [
            "/usr/local/bin/codex",
            "exec",
            "--model",
            "gpt-5.6-sol",
            "private prompt",
        ],
        "task_type": "review",
    }

    result = run_dispatch_hook(request)

    assert result.observation is not None
    assert result.observation.command == "<redacted>"
    assert result.observation.requested_model == "gpt-5.6-sol"
    assert result.observation.resolved_model == "gpt-5.6-sol"
    assert result.observation.classification_authority.value == "declared"
    persisted = json.loads(request.ledger_path.read_text(encoding="utf-8"))
    assert persisted["requested_model"] == "gpt-5.6-sol"
    assert "wrong-model" not in json.dumps(persisted)


@pytest.mark.parametrize(
    "metadata",
    [
        {"argv": "codex exec review", "task_type": "review"},
        {"argv": ["codex", "exec", "review"], "task_type": ""},
        {
            "argv": ["codex", "exec", "review"],
            "task_type": "review",
            "start_time_unix_nano": "invalid",
        },
    ],
)
def test_present_but_invalid_structured_metadata_fails_closed(
    tmp_path: Path, metadata: dict[str, object]
) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command="codex exec --model wrong-model legacy prompt",
    )
    request.payload["dispatch_telemetry"] = metadata

    result = run_dispatch_hook(request)

    assert result.observation is None
    assert not request.ledger_path.exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "status"],
        ["opencode", "run", "review"],
        ["codex", "review"],
    ],
)
def test_structured_metadata_rejects_wrong_provider_or_subcommand(
    tmp_path: Path, argv: list[str]
) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command="codex exec review",
    )
    request.payload["dispatch_telemetry"] = {
        "argv": argv,
        "task_type": "review",
    }

    result = run_dispatch_hook(request)

    assert result.observation is None
    assert not request.ledger_path.exists()


def test_legacy_dispatch_emits_typed_parse_status(
    tmp_path: Path, monkeypatch
) -> None:
    emitted = []
    monkeypatch.setattr(
        "agentic_pr_dash.codex_hooks.dispatch_runner.emit_dispatch_span",
        lambda telemetry, **kwargs: emitted.append((telemetry, kwargs)),
    )

    result = run_dispatch_hook(
        _request(
            tmp_path,
            provider=DispatchProvider.CODEX,
            command="codex exec review",
        )
    )

    assert result.observation is not None
    assert emitted[0][0].parse_status.value == "legacy_parsed"


def test_legacy_config_model_does_not_claim_explicit_flag_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    emitted = []
    monkeypatch.setattr(
        "agentic_pr_dash.codex_hooks.dispatch_runner.emit_dispatch_span",
        lambda telemetry, **kwargs: emitted.append((telemetry, kwargs)),
    )

    run_dispatch_hook(
        _request(
            tmp_path,
            provider=DispatchProvider.CODEX,
            command="codex exec -c model=gpt-5.6-sol review",
        )
    )

    assert emitted[0][0].resolution_source.value == "unavailable"


def test_unavailable_span_has_low_cardinality_error_type(
    tmp_path: Path, monkeypatch
) -> None:
    emitted = []
    monkeypatch.setattr(
        "agentic_pr_dash.codex_hooks.dispatch_runner.emit_dispatch_span",
        lambda telemetry, **kwargs: emitted.append((telemetry, kwargs)),
    )
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command="codex exec review",
        response={"exit_code": 1, "stderr": "quota exhausted"},
    )
    request.payload["dispatch_telemetry"] = {
        "argv": ["codex", "exec", "review"],
        "task_type": "review",
    }

    run_dispatch_hook(request)

    assert emitted[0][1]["error_type"] == "provider.unavailable"


@pytest.mark.parametrize("provider", list(DispatchProvider))
def test_unrelated_commands_are_ignored(
    tmp_path: Path, provider: DispatchProvider
) -> None:
    result = run_dispatch_hook(
        _request(tmp_path, provider=provider, command="git status")
    )

    assert result.observation is None
    assert not (tmp_path / "dispatch.jsonl").exists()


def test_nonzero_exit_is_failure(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command="codex exec implement feature",
        response={"exit_code": 1, "stderr": "tool crashed"},
    )

    result = run_dispatch_hook(request)

    assert result.observation is not None
    assert result.observation.outcome is DispatchOutcome.FAILURE


def test_quota_failure_is_persisted_as_unavailable(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.OPENCODE,
        command="opencode run review changes",
        response={"exit_code": 1, "stderr": "quota exceeded"},
    )

    result = run_dispatch_hook(request)

    assert result.observation is not None
    assert result.observation.outcome is DispatchOutcome.UNAVAILABLE
    availability = json.loads(request.availability_path.read_text(encoding="utf-8"))
    assert availability["provider"] == "opencode"
    assert availability["available"] is False


def test_detached_dispatch_never_invokes_repository_callback(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command="codex exec review",
        source=DispatchSource.DETACHED_RUNNER,
    )

    result = run_dispatch_hook(request, lambda _observation: "should not run")

    assert result.observation is not None
    assert result.additional_context is None


def test_completed_review_persists_before_repository_callback(tmp_path: Path) -> None:
    events: list[str] = []
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command="codex exec review",
        response={
            "exit_code": 0,
            "review_verdict": {"status": "clean", "findings": []},
        },
        classification={"task_type": "review", "framework": "coding-agent/v1"},
    )

    def callback(observation) -> str | None:
        assert request.ledger_path.read_text(encoding="utf-8")
        events.append(observation.task_type)
        return "Gaia review budget updated"

    result = run_dispatch_hook(request, callback)

    assert events == ["review"]
    assert result.additional_context == "Gaia review budget updated"


def test_heuristic_review_label_cannot_invoke_repository_callback(
    tmp_path: Path,
) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command="codex exec review",
    )
    callbacks: list[str] = []

    result = run_dispatch_hook(
        request,
        lambda observation: callbacks.append(observation.task_type) or "policy ran",
    )

    assert result.observation is not None
    assert result.observation.classification_authority.value == "legacy_inferred"
    assert callbacks == []
    assert result.additional_context is None


def test_command_environment_declaration_is_telemetry_only(
    tmp_path: Path,
) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command=(
            "AGENT_DISPATCH_TASK_TYPE=review "
            "AGENT_DISPATCH_FRAMEWORK=coding-agent/v1 "
            "codex exec review"
        ),
    )

    callbacks: list[str] = []
    result = run_dispatch_hook(
        request,
        lambda observation: callbacks.append(observation.task_type) or "policy ran",
    )

    assert result.observation is not None
    assert result.observation.classification_authority.value == "legacy_inferred"
    assert result.observation.classification_framework is None
    assert callbacks == []
    assert result.additional_context is None


def test_declaration_on_earlier_shell_segment_is_not_authoritative(
    tmp_path: Path,
) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command=(
            "AGENT_DISPATCH_TASK_TYPE=review "
            "AGENT_DISPATCH_FRAMEWORK=coding-agent/v1 "
            "true && codex exec implement feature"
        ),
    )
    callbacks: list[str] = []

    result = run_dispatch_hook(
        request,
        lambda observation: callbacks.append(observation.task_type) or "policy ran",
    )

    assert result.observation is not None
    assert result.observation.classification_authority.value == "legacy_inferred"
    assert callbacks == []
    assert result.additional_context is None


def test_duplicate_command_environment_assignments_are_not_authoritative(
    tmp_path: Path,
) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command=(
            "AGENT_DISPATCH_TASK_TYPE=review "
            "AGENT_DISPATCH_TASK_TYPE=exec "
            "AGENT_DISPATCH_FRAMEWORK=coding-agent/v1 "
            "codex exec implement feature"
        ),
    )
    callbacks: list[str] = []

    result = run_dispatch_hook(
        request,
        lambda observation: callbacks.append(observation.task_type) or "policy ran",
    )

    assert result.observation is not None
    assert result.observation.classification_authority.value == "legacy_inferred"
    assert callbacks == []


@pytest.mark.parametrize(
    ("provider", "executable", "subcommand"),
    [
        (DispatchProvider.CODEX, "/usr/local/bin/codex", "exec"),
        (DispatchProvider.OPENCODE, "./bin/opencode", "run"),
    ],
)
def test_path_qualified_provider_keeps_attached_classification(
    tmp_path: Path,
    provider: DispatchProvider,
    executable: str,
    subcommand: str,
) -> None:
    request = _request(
        tmp_path,
        provider=provider,
        command=(
            "AGENT_DISPATCH_TASK_TYPE=review "
            "AGENT_DISPATCH_FRAMEWORK=coding-agent/v1 "
            f"{executable} {subcommand} review"
        ),
        classification={"task_type": "review", "framework": "coding-agent/v1"},
    )

    result = run_dispatch_hook(request, lambda _observation: "policy ran")

    assert result.observation is not None
    assert result.observation.classification_authority.value == "declared"
    assert result.additional_context == "policy ran"


def test_canonical_multiline_review_with_stdin_redirect_keeps_classification(
    tmp_path: Path,
) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command=(
            "codex exec --sandbox workspace-write \"Review this diff:\n"
            "+print('safe; quoted')\" </dev/null"
        ),
        classification={"task_type": "review", "framework": "coding-agent/v1"},
    )

    result = run_dispatch_hook(request, lambda _observation: "policy ran")

    assert result.observation is not None
    assert result.observation.classification_authority.value == "declared"
    assert result.additional_context == "policy ran"


def test_provider_words_used_as_arguments_cannot_reach_policy(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command=(
            "printf '%s' AGENT_DISPATCH_TASK_TYPE=review "
            "AGENT_DISPATCH_FRAMEWORK=coding-agent/v1 codex exec review"
        ),
        classification={"task_type": "review", "framework": "coding-agent/v1"},
    )
    callbacks: list[str] = []

    result = run_dispatch_hook(
        request,
        lambda observation: callbacks.append(observation.task_type) or "policy ran",
    )

    assert result.observation is not None
    assert result.observation.classification_authority.value == "legacy_inferred"
    assert callbacks == []


def test_unexecuted_conditional_provider_cannot_reach_policy(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command="true || codex exec review",
        classification={"task_type": "review", "framework": "coding-agent/v1"},
    )
    callbacks: list[str] = []

    result = run_dispatch_hook(
        request,
        lambda observation: callbacks.append(observation.task_type) or "policy ran",
    )

    assert result.observation is not None
    assert result.observation.classification_authority.value == "legacy_inferred"
    assert callbacks == []


def test_shell_sequence_cannot_reach_policy(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command="codex --version; codex exec review",
        classification={"task_type": "review", "framework": "coding-agent/v1"},
    )

    callbacks: list[str] = []
    result = run_dispatch_hook(
        request,
        lambda observation: callbacks.append(observation.task_type) or "policy ran",
    )

    assert result.observation is not None
    assert result.observation.classification_authority.value == "legacy_inferred"
    assert callbacks == []


def test_newline_separated_dispatch_cannot_reach_policy(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command="cd /repo\ncodex exec review",
        classification={"task_type": "review", "framework": "coding-agent/v1"},
    )

    callbacks: list[str] = []
    result = run_dispatch_hook(
        request,
        lambda observation: callbacks.append(observation.task_type) or "policy ran",
    )

    assert result.observation is not None
    assert result.observation.classification_authority.value == "legacy_inferred"
    assert callbacks == []


@pytest.mark.parametrize(
    "separator",
    [
        " |& ",
        "\n",
    ],
)
def test_dispatch_followed_by_shell_separator_cannot_reach_policy(
    tmp_path: Path, separator: str
) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command=f"codex exec review{separator}true",
        classification={"task_type": "review", "framework": "coding-agent/v1"},
    )

    callbacks: list[str] = []
    result = run_dispatch_hook(
        request,
        lambda observation: callbacks.append(observation.task_type) or "policy ran",
    )

    assert result.observation is not None
    assert result.observation.classification_authority.value == "legacy_inferred"
    assert callbacks == []


@pytest.mark.parametrize(
    "command",
    [
        "cat <<'EOF'\ncodex exec review\nEOF",
        "'X=y' codex exec review; true",
    ],
)
def test_shell_data_cannot_reach_policy(tmp_path: Path, command: str) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command=command,
        classification={"task_type": "review", "framework": "coding-agent/v1"},
    )
    callbacks: list[str] = []

    result = run_dispatch_hook(
        request,
        lambda observation: callbacks.append(observation.task_type) or "policy ran",
    )

    assert result.observation is not None
    assert result.observation.classification_authority.value == "legacy_inferred"
    assert callbacks == []


def test_quoted_shell_separator_cannot_create_authoritative_classification(
    tmp_path: Path,
) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command=(
            "printf '%s' '&&' AGENT_DISPATCH_TASK_TYPE=review "
            "AGENT_DISPATCH_FRAMEWORK=coding-agent/v1 codex exec review"
        ),
    )
    callbacks: list[str] = []

    result = run_dispatch_hook(
        request,
        lambda observation: callbacks.append(observation.task_type) or "policy ran",
    )

    assert result.observation is not None
    assert result.observation.classification_authority.value == "legacy_inferred"
    assert callbacks == []


@pytest.mark.parametrize("framework", ["other/v1", "coding-agent/v2", ""])
def test_unsupported_typed_framework_is_not_authoritative(
    tmp_path: Path, framework: str
) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command="codex exec review",
        classification={"task_type": "review", "framework": framework},
    )
    callbacks: list[str] = []

    result = run_dispatch_hook(
        request,
        lambda observation: callbacks.append(observation.task_type) or "policy ran",
    )

    assert result.observation is not None
    assert result.observation.classification_authority.value == "legacy_inferred"
    assert callbacks == []


def test_callback_failure_does_not_undo_persisted_observation(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command="codex exec review",
        classification={"task_type": "review", "framework": "coding-agent/v1"},
    )

    def fail(_observation) -> str | None:
        raise RuntimeError("repository callback failed")

    result = run_dispatch_hook(request, fail)

    assert result.additional_context is None
    assert request.ledger_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "entrypoint",
    [run_codex_dispatch_logger, run_opencode_dispatch_logger],
)
def test_entrypoints_ignore_malformed_json(entrypoint, monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", StringIO("not json"))

    assert entrypoint.main([]) == 0


def test_codex_entrypoint_renders_repository_context(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(tmp_path / "dispatch.jsonl"))
    monkeypatch.setattr(
        sys,
        "stdin",
        StringIO(
            json.dumps(
                {
                    "session_id": "s",
                    "cwd": str(tmp_path),
                    "tool_input": {"command": "codex exec review"},
                    "tool_response": {"exit_code": 0},
                    "dispatch_classification": {
                        "task_type": "review",
                        "framework": "coding-agent/v1",
                    },
                }
            )
        ),
    )

    result = run_codex_dispatch_logger.main(
        [], lambda _observation: "Gaia review budget updated"
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["additionalContext"] == (
        "Gaia review budget updated"
    )


def test_detached_opencode_entrypoint_persists_without_context(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    ledger = tmp_path / "dispatch.jsonl"
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(ledger))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))

    result = run_opencode_dispatch_logger.main(
        ["--command", "opencode run review", "--exit-code", "0"],
        lambda _observation: "should not run",
    )

    assert result == 0
    assert json.loads(ledger.read_text(encoding="utf-8"))["source"] == (
        "detached_runner"
    )
    assert capsys.readouterr().out == ""


def test_detached_entrypoint_accepts_adapter_model_resolution(
    tmp_path: Path, monkeypatch
) -> None:
    ledger = tmp_path / "dispatch.jsonl"
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(ledger))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))

    result = run_opencode_dispatch_logger.main(
        [
            "--command",
            "opencode run review",
            "--exit-code",
            "0",
            "--configured-model",
            "kimi-for-coding/k3-256k",
            "--default-model",
            "opencode-default",
        ]
    )

    assert result == 0
    persisted = json.loads(ledger.read_text(encoding="utf-8"))
    assert persisted["requested_model"] is None
    assert persisted["resolved_model"] == "kimi-for-coding/k3-256k"


def test_detached_entrypoint_accepts_nul_delimited_structured_argv(
    tmp_path: Path, monkeypatch
) -> None:
    ledger = tmp_path / "dispatch.jsonl"
    secret = "private prompt body"
    argv = ("codex", "exec", "--model", "gpt-5.6-sol", secret)
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(ledger))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(
        sys,
        "stdin",
        TextIOWrapper(BytesIO(b"\0".join(value.encode() for value in argv) + b"\0")),
    )

    result = run_codex_dispatch_logger.main(
        [
            "--structured-stdin",
            "--task-type",
            "review",
            "--exit-code",
            "0",
        ]
    )

    assert result == 0
    serialized = ledger.read_text(encoding="utf-8")
    persisted = json.loads(serialized)
    assert persisted["requested_model"] == "gpt-5.6-sol"
    assert persisted["resolved_model"] == "gpt-5.6-sol"
    assert persisted["classification_authority"] == "declared"
    assert secret not in serialized


def test_structured_detached_failure_preserves_unavailability_detection(
    tmp_path: Path, monkeypatch
) -> None:
    ledger = tmp_path / "dispatch.jsonl"
    availability = tmp_path / "codex-availability.json"
    error_file = tmp_path / "codex-error.txt"
    error_file.write_text("usage quota exhausted", encoding="utf-8")
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(ledger))
    monkeypatch.setenv("DISPATCH_AVAILABILITY_PATH", str(availability))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(
        sys,
        "stdin",
        TextIOWrapper(BytesIO(b"codex\0exec\0private prompt\0")),
    )

    result = run_codex_dispatch_logger.main(
        [
            "--structured-stdin",
            "--task-type",
            "exec",
            "--exit-code",
            "1",
            "--error-file",
            str(error_file),
            "--started-at-unix-nano",
            "1000000000",
        ]
    )

    assert result == 0
    assert json.loads(ledger.read_text(encoding="utf-8"))["outcome"] == "unavailable"
    assert json.loads(availability.read_text(encoding="utf-8"))["available"] is False


def test_detached_failure_reads_error_file_for_unavailability(
    tmp_path: Path, monkeypatch
) -> None:
    ledger = tmp_path / "dispatch.jsonl"
    availability = tmp_path / "codex-availability.json"
    error_file = tmp_path / "codex-error.txt"
    error_file.write_text("usage quota exhausted", encoding="utf-8")
    original_read_text = Path.read_text

    def reject_unbounded_error_read(path: Path, *args, **kwargs) -> str:
        if path == error_file:
            raise AssertionError("error files must be scanned incrementally")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_unbounded_error_read)
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(ledger))
    monkeypatch.setenv("DISPATCH_AVAILABILITY_PATH", str(availability))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))

    result = run_codex_dispatch_logger.main(
        [
            "--command",
            "codex exec review",
            "--exit-code",
            "1",
            "--error-file",
            str(error_file),
        ]
    )

    assert result == 0
    assert json.loads(ledger.read_text(encoding="utf-8"))["outcome"] == "unavailable"
    assert json.loads(availability.read_text(encoding="utf-8"))["available"] is False


def test_agent_dispatch_produces_normalized_observation() -> None:
    observation = observation_from_agent_payload(
        {
            "tool_name": "Agent",
            "session_id": "s",
            "tool_input": {
                "description": "Implement the parser",
                "prompt": "Modify the code",
                "model": "sonnet",
                "subagent_type": "general-purpose",
            },
        },
        "/repo/wt",
    )

    assert observation is not None
    assert observation.provider is DispatchProvider.CLAUDE
    assert observation.task_type == "small_impl"
    assert observation.requested_model == "sonnet"
    assert observation.resolved_model == "sonnet-4.6"
    assert observation.outcome is DispatchOutcome.SUCCESS
