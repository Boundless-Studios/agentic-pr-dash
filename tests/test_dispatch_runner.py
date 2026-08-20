from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

from agentic_pr_dash.codex_hooks import (
    run_codex_dispatch_logger,
    run_opencode_dispatch_logger,
)
from agentic_pr_dash.codex_hooks.dispatch_runner import (
    DispatchHookRequest,
    observation_from_agent_payload,
    run_dispatch_hook,
)
from agentic_pr_dash.dispatch_observation import (
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
) -> DispatchHookRequest:
    payload: dict[str, object] = {
        "session_id": "session-1",
        "cwd": "/repo/wt",
        "tool_input": {"command": command},
        "tool_response": response or {"exit_code": 0},
    }
    if classification is not None:
        payload["dispatch_classification"] = classification
    return DispatchHookRequest(
        provider=provider,
        source=source,
        payload=payload,
        ledger_path=tmp_path / "dispatch.jsonl",
        availability_path=tmp_path / "availability.json",
    )


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
    assert persisted == result.observation.to_dict()


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
