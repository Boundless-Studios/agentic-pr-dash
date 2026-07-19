from __future__ import annotations

import io
import json
from concurrent.futures import ThreadPoolExecutor
import time

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


def test_status_report_rejects_invalid_contract(tmp_path):
    with pytest.raises(ValueError, match="schema_version"):
        session_registry.record_status_report(
            _report(schema_version=2),
            path=tmp_path / "invalid.jsonl",
        )


@pytest.mark.parametrize(
    "private_field",
    (
        "prompt_text",
        "raw_transcript",
        "tool_input",
        "tool_output",
        "messages",
        "password",
        "api_key",
        "environment",
    ),
)
def test_status_report_rejects_private_payloads(tmp_path, private_field):
    with pytest.raises(ValueError, match=private_field):
        session_registry.record_status_report(
            _report(**{private_field: "must never be persisted"}),
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


def test_duplicate_producer_event_id_is_idempotent(tmp_path, monkeypatch):
    registry = tmp_path / "events.jsonl"
    times = iter(("2026-07-19T10:00:00Z", "2026-07-19T10:00:05Z"))
    monkeypatch.setattr(session_registry, "_utc_now", lambda: next(times))

    first = session_registry.record_status_report(
        _report(event_id="observation-1"), path=registry
    )
    second = session_registry.record_status_report(
        _report(event_id="observation-1"), path=registry
    )

    assert second == first
    assert len(session_registry.read_events(path=registry)) == 1


def test_duplicate_status_event_id_is_idempotent_across_busy_registry(
    tmp_path, monkeypatch
):
    registry = tmp_path / "events.jsonl"
    monkeypatch.setattr(session_registry, "_utc_now", lambda: "2026-07-19T10:00:00Z")
    first = session_registry.record_status_report(
        _report(event_id="observation-1"), path=registry
    )
    with registry.open("a", encoding="utf-8") as handle:
        for index in range(300):
            handle.write(
                json.dumps(
                    {
                        "event_id": f"other-{index}",
                        "event": "heartbeat",
                        "session_id": f"other-session-{index}",
                        "timestamp": "2026-07-19T10:00:00Z",
                    }
                )
                + "\n"
            )

    second = session_registry.record_status_report(
        _report(event_id="observation-1"), path=registry
    )
    matching = [
        event
        for event in session_registry.read_events(path=registry, limit=0)
        if event.get("event_id") == first["event_id"]
    ]

    assert second["event_id"] == first["event_id"]
    assert len(matching) == 1


def test_unchanged_status_report_refreshes_after_observation_window(
    tmp_path, monkeypatch
):
    registry = tmp_path / "events.jsonl"
    times = iter(("2026-07-19T10:00:00Z", "2026-07-19T10:02:00Z"))
    monkeypatch.setattr(session_registry, "_utc_now", lambda: next(times))

    first = session_registry.record_status_report(_report(), path=registry)
    second = session_registry.record_status_report(_report(), path=registry)
    state = session_registry.summarize_sessions(path=registry).sessions["conversation-1"]

    assert first["event_id"] != second["event_id"]
    assert len(session_registry.read_events(path=registry)) == 2
    assert state.harness_reported_at == "2026-07-19T10:02:00Z"


def test_status_report_a_b_a_transition_is_not_mistaken_for_retry(
    tmp_path, monkeypatch
):
    registry = tmp_path / "events.jsonl"
    times = iter(
        (
            "2026-07-19T10:00:00Z",
            "2026-07-19T10:00:05Z",
            "2026-07-19T10:00:10Z",
        )
    )
    monkeypatch.setattr(session_registry, "_utc_now", lambda: next(times))

    session_registry.record_status_report(_report(state="warning"), path=registry)
    session_registry.record_status_report(_report(state="draining"), path=registry)
    session_registry.record_status_report(_report(state="warning"), path=registry)
    state = session_registry.summarize_sessions(path=registry).sessions["conversation-1"]

    assert len(session_registry.read_events(path=registry)) == 3
    assert state.supervisor_state == "warning"


def test_unknown_extensions_do_not_change_idempotence_or_persist(tmp_path):
    registry = tmp_path / "events.jsonl"

    first = session_registry.record_status_report(
        _report(event_id="observation-1", future_metric={"value": 1}),
        path=registry,
    )
    second = session_registry.record_status_report(
        _report(event_id="observation-1", future_metric={"value": 2}),
        path=registry,
    )

    assert second == first
    assert "future_metric" not in first
    assert "observation-1" not in first["event_id"]
    assert len(session_registry.read_events(path=registry)) == 1


def test_concurrent_duplicate_producer_event_is_appended_once(
    tmp_path, monkeypatch
):
    registry = tmp_path / "events.jsonl"
    original_lookup = session_registry._registry_event_by_id

    def delayed_lookup(*args, **kwargs):
        existing = original_lookup(*args, **kwargs)
        time.sleep(0.02)
        return existing

    monkeypatch.setattr(session_registry, "_registry_event_by_id", delayed_lookup)
    payload = _report(event_id="observation-1")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: session_registry.record_status_report(payload, path=registry),
                range(2),
            )
        )

    assert results[0] == results[1]
    assert len(session_registry.read_events(path=registry)) == 1


def test_producer_event_id_is_namespaced_by_session_identity(tmp_path):
    registry = tmp_path / "events.jsonl"

    first = session_registry.record_status_report(
        _report(event_id="1", conversation_id="conversation-1"),
        path=registry,
    )
    second = session_registry.record_status_report(
        _report(event_id="1", conversation_id="conversation-2"),
        path=registry,
    )

    assert first["event_id"] != second["event_id"]
    assert len(session_registry.read_events(path=registry)) == 2


def test_non_keyed_report_skips_idempotency_scan(tmp_path, monkeypatch):
    registry = tmp_path / "events.jsonl"

    def fail_lookup(*args, **kwargs):
        raise AssertionError("non-keyed reports must not scan the registry")

    monkeypatch.setattr(session_registry, "_registry_event_by_id", fail_lookup)

    session_registry.record_status_report(_report(), path=registry)

    assert len(session_registry.read_events(path=registry)) == 1


def test_auto_compaction_keeps_only_latest_status_per_active_session(
    tmp_path, monkeypatch
):
    registry = tmp_path / "events.jsonl"
    monkeypatch.setenv("AGENTIC_PR_DASH_REGISTRY_COMPACT_THRESHOLD", "3")
    monkeypatch.setenv("AGENTIC_PR_DASH_REGISTRY_READ_LIMIT", "0")

    for generation in range(5):
        session_registry.record_status_report(
            _report(generation=generation, state=f"state-{generation}"),
            path=registry,
        )
    events = session_registry.read_events(path=registry)
    state = session_registry.summarize_sessions(path=registry).sessions[
        "conversation-1"
    ]

    assert [event["event"] for event in events] == ["harness_status"]
    assert state.generation == 4
    assert state.supervisor_state == "state-4"


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
