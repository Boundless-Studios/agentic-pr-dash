from __future__ import annotations

import json
from pathlib import Path

from agentic_pr_dash import stop_hook


def _request(tmp_path: Path) -> stop_hook.StopHookRequest:
    return stop_hook.StopHookRequest(
        cwd=str(tmp_path),
        session_id="session-1",
        no_waiter=True,
        policy_path=str(tmp_path / "policy.yaml"),
        ledger_path=str(tmp_path / "ledger.json"),
    )


def _state_dir(tmp_path: Path) -> Path:
    return tmp_path / ".agentic-pr-dash"


def test_adapter_forwards_typed_request_to_canonical_gate(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(stop_hook.maintenance_check, "main", lambda argv: calls.append(argv) or 2)

    assert stop_hook.run_stop_hook(_request(tmp_path)) == 2
    assert calls == [[
        "stop-gate", "--cwd", str(tmp_path), "--session-id", "session-1",
        "--policy", str(tmp_path / "policy.yaml"),
        "--ledger", str(tmp_path / "ledger.json"), "--no-waiter",
    ]]


def test_adapter_releases_after_bounded_repeated_internal_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        stop_hook.maintenance_check,
        "main",
        lambda _argv: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    request = _request(tmp_path)

    assert stop_hook.run_stop_hook(request) == 2
    assert stop_hook.run_stop_hook(request) == 2
    assert stop_hook.run_stop_hook(request) == 0

    marker = _state_dir(tmp_path) / "pr-watch.stopgate-release.jsonl"
    record = json.loads(marker.read_text().splitlines()[-1])
    assert record["event"] == "stop_gate_escape_release"
    assert record["session_id"] == "session-1"
    assert record["failure_count"] == 3
    assert "boom" not in marker.read_text()
    assert "RELEASING" in capsys.readouterr().err


def test_success_resets_prior_internal_error_streak(tmp_path, monkeypatch):
    outcomes = iter([RuntimeError("boom"), 0, RuntimeError("boom")])

    def fake_main(_argv):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(stop_hook.maintenance_check, "main", fake_main)
    request = _request(tmp_path)

    assert stop_hook.run_stop_hook(request) == 2
    assert stop_hook.run_stop_hook(request) == 0
    assert stop_hook.run_stop_hook(request) == 2
    state = json.loads((_state_dir(tmp_path) / "pr-watch.stopgate-failstreak.json").read_text())
    assert state["streak"] == 1


def test_unexpected_gate_exit_is_a_bounded_internal_error(tmp_path, monkeypatch):
    monkeypatch.setattr(stop_hook.maintenance_check, "main", lambda _argv: 1)

    assert stop_hook.run_stop_hook(_request(tmp_path)) == 2
    state = json.loads((_state_dir(tmp_path) / "pr-watch.stopgate-failstreak.json").read_text())
    assert state["streak"] == 1
