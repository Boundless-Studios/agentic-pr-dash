from __future__ import annotations

import json
import subprocess

from agentic_pr_dash.codex_hooks import run_warden


class _Validator:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def _block_reason_for_command(self, command: str) -> str | None:
        return None

    def run(self, shared, commit_only, **kwargs) -> int:
        self.calls.append((shared, commit_only, kwargs))
        return 0


def test_run_policy_pipeline_dispatches_validators_after_warden_allow(monkeypatch) -> None:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "cwd": "/repo",
    }
    validator = _Validator()
    monkeypatch.setattr(run_warden, "run_payload", lambda _payload: 0)

    result = run_warden.run_policy_pipeline(
        json.dumps(payload),
        validator=validator,
        shared_hooks=[("shared", "shared.py")],
        commit_only_hooks=[("commit", "commit.py")],
        base_dir="/repo",
    )

    assert result == 0
    assert validator.calls[0][2]["command"] == "git status"


def test_run_policy_pipeline_skips_validators_when_trust_check_blocks() -> None:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "bash scripts/x.sh"}})
    validator = _Validator()

    result = run_warden.run_policy_pipeline(
        payload,
        validator=validator,
        shared_hooks=[],
        commit_only_hooks=[],
        base_dir="/repo",
        trust_check=lambda _command, _cwd: "untrusted script",
    )

    assert result == 0
    assert validator.calls == []


def test_run_payload_can_preserve_attended_ask(monkeypatch, capsys) -> None:
    ask = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": "confirm this command",
        }
    }
    monkeypatch.setattr(run_warden, "resolve_warden_hook", lambda _payload: "/warden")
    monkeypatch.setattr(
        run_warden.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, json.dumps(ask), ""),
    )

    result = run_warden.run_payload(
        {"tool_name": "Bash", "tool_input": {"command": "git push"}},
        preserve_ask=True,
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == ask


def test_policy_pipeline_forwards_preserve_warden_ask(monkeypatch, capsys) -> None:
    validator = _Validator()

    def fake_run_payload(_payload, *, preserve_ask=False):
        assert preserve_ask is True
        print('{"hookSpecificOutput":{"permissionDecision":"ask"}}')
        return 0

    monkeypatch.setattr(run_warden, "run_payload", fake_run_payload)
    result = run_warden.run_policy_pipeline(
        json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"}}),
        validator=validator,
        shared_hooks=[],
        commit_only_hooks=[],
        base_dir="/repo",
        preserve_warden_ask=True,
    )

    assert result == 0
    assert "permissionDecision" in capsys.readouterr().out
    assert validator.calls == []
