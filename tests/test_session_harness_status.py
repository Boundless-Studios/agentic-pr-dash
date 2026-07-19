from __future__ import annotations

import io
import json

import pytest

from agentic_pr_dash import cli, session_registry


def _report(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "runtime": "codex",
        "state": "warning",
        "chain_id": "chain-1",
        "conversation_id": "conversation-1",
        "generation": 2,
        "context_percent": 67.5,
        "context_tokens": 675_000,
        "window_tokens": 1_000_000,
        "cumulative_tokens": 9_500_000,
        "confidence": "confident",
        "quiescence": "busy",
        "active": {
            "turns": 1,
            "tools": 2,
            "subagents": 1,
            "critical_sections": 0,
        },
        "checkpoint_fingerprint": "abc123",
        "outbox_depth": 3,
    }
    payload.update(overrides)
    return payload


def test_status_report_projects_usage_identity_and_activity(tmp_path, monkeypatch):
    registry = tmp_path / "events.jsonl"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    payload = _report(future_metric={"supported_later": True})
    monkeypatch.setattr(session_registry, "_utc_now", lambda: "2026-07-19T10:00:00Z")

    event = session_registry.record_status_report(
        payload,
        worktree_path=str(worktree),
        branch="bou-2195",
        pid=4321,
        agent_name="brave-otter",
        path=registry,
    )
    state = session_registry.summarize_sessions(path=registry).sessions["conversation-1"]

    assert event["event"] == "harness_status"
    assert state.chain_id == "chain-1"
    assert state.generation == 2
    assert state.supervisor_state == "warning"
    assert state.context_percent == 67.5
    assert state.context_tokens == 675_000
    assert state.window_tokens == 1_000_000
    assert state.cumulative_tokens == 9_500_000
    assert state.context_confidence == "confident"
    assert state.quiescence == "busy"
    assert state.active_turns == 1
    assert state.active_tools == 2
    assert state.active_subagents == 1
    assert state.active_critical_sections == 0
    assert state.checkpoint_fingerprint == "abc123"
    assert state.outbox_depth == 3
    assert state.harness_reported_at == "2026-07-19T10:00:00Z"
    assert state.cli == "codex"
    assert state.agent_name == "brave-otter"
    assert state.pid == 4321
    assert state.worktree_path == str(worktree.resolve())


def test_status_report_rejects_invalid_contract_and_private_payloads(tmp_path):
    with pytest.raises(ValueError, match="schema_version"):
        session_registry.record_status_report(
            _report(schema_version=2),
            path=tmp_path / "invalid.jsonl",
        )
    with pytest.raises(ValueError, match="prompt_text"):
        session_registry.record_status_report(
            _report(prompt_text="must never be persisted"),
            path=tmp_path / "private.jsonl",
        )


def test_status_report_preserves_sparse_legacy_metadata(tmp_path):
    registry = tmp_path / "events.jsonl"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    session_registry.record_event(
        event="started",
        session_id="conversation-1",
        cli="codex",
        agent_name="silver-lynx",
        launch_source="worktree-deck",
        pid=1234,
        worktree_path=str(worktree),
        branch="feature/one",
        docker_mode="remote",
        path=registry,
    )

    session_registry.record_status_report(
        _report(),
        worktree_path=str(worktree),
        path=registry,
    )
    state = session_registry.summarize_sessions(path=registry).sessions["conversation-1"]

    assert state.agent_name == "silver-lynx"
    assert state.launch_source == "worktree-deck"
    assert state.pid == 1234
    assert state.branch == "feature/one"
    assert state.docker_mode == "remote"


def test_late_status_or_heartbeat_cannot_resurrect_terminal_conversation(tmp_path):
    registry = tmp_path / "events.jsonl"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    session_registry.record_event(
        event="started",
        session_id="conversation-1",
        worktree_path=str(worktree),
        pid=1234,
        path=registry,
    )
    terminal = session_registry.record_event(
        event="completed",
        session_id="conversation-1",
        worktree_path=str(worktree),
        path=registry,
    )
    session_registry.record_status_report(
        _report(state="running"),
        worktree_path=str(worktree),
        path=registry,
    )
    session_registry.record_event(
        event="heartbeat",
        session_id="conversation-1",
        worktree_path=str(worktree),
        path=registry,
    )

    state = session_registry.summarize_sessions(path=registry).sessions["conversation-1"]
    assert state.event == "completed"
    assert state.timestamp == terminal["timestamp"]
    assert state.is_terminal is True


def test_duplicate_status_report_is_idempotent(tmp_path):
    registry = tmp_path / "events.jsonl"

    first = session_registry.record_status_report(_report(), path=registry)
    second = session_registry.record_status_report(_report(), path=registry)

    assert first["event_id"] == second["event_id"]
    assert len(session_registry.read_events(path=registry)) == 1


def test_summary_preserves_multiple_conversations_per_worktree(tmp_path):
    registry = tmp_path / "events.jsonl"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    session_registry.record_status_report(
        _report(conversation_id="conversation-1"),
        worktree_path=str(worktree),
        path=registry,
    )
    session_registry.record_status_report(
        _report(conversation_id="conversation-2", generation=3),
        worktree_path=str(worktree),
        path=registry,
    )

    summary = session_registry.summarize_sessions(path=registry)
    assert set(summary.sessions) == {"conversation-1", "conversation-2"}
    assert [
        state.session_id for state in summary.by_worktree_sessions[str(worktree.resolve())]
    ] == ["conversation-2", "conversation-1"]


def test_unified_cli_ingests_status_report_from_stdin(tmp_path, monkeypatch, capsys):
    registry = tmp_path / "events.jsonl"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv("AGENTIC_PR_DASH_SESSION_REGISTRY", str(registry))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_report())))

    assert cli.main(
        [
            "session-report",
            "--json",
            "--worktree-path",
            str(worktree),
            "--pid",
            "4321",
        ]
    ) == 0

    output = json.loads(capsys.readouterr().out)
    assert output == {
        "event_id": output["event_id"],
        "ok": True,
        "session_id": "conversation-1",
    }
    assert session_registry.summarize_sessions(path=registry).sessions[
        "conversation-1"
    ].context_tokens == 675_000
