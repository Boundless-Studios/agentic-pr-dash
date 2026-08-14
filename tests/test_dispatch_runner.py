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
) -> DispatchHookRequest:
    return DispatchHookRequest(
        provider=provider,
        source=source,
        payload={
            "session_id": "session-1",
            "cwd": "/repo/wt",
            "tool_input": {"command": command},
            "tool_response": response or {"exit_code": 0},
        },
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
    )

    def callback(observation) -> str | None:
        assert request.ledger_path.read_text(encoding="utf-8")
        events.append(observation.task_type)
        return "Gaia review budget updated"

    result = run_dispatch_hook(request, callback)

    assert events == ["review"]
    assert result.additional_context == "Gaia review budget updated"


def test_callback_failure_does_not_undo_persisted_observation(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        provider=DispatchProvider.CODEX,
        command="codex exec review",
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
