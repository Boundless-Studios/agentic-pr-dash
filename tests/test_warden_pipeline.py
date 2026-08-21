from __future__ import annotations

import json

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
