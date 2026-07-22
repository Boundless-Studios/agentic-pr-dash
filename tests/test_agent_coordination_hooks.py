"""Tests for the agent-coordination hooks extracted in BOU-1674:

  * run_agent_activity      — turn-lifecycle activity stamp
  * run_ask_user_agentflow  — AskUserQuestion -> AgentFlow routing
  * run_permission_agentflow — PermissionRequest -> AgentFlow routing
  * run_model_dispatch_logger — Agent-dispatch classification + logging
  * agentflow               — shared hub client
"""

import fcntl
import io
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from agentic_pr_dash import app
from agentic_pr_dash.codex_hooks import (
    agentflow,
    run_agent_activity,
    run_ask_user_agentflow,
    run_model_dispatch_logger,
    run_permission_agentflow,
)


def _run(module, payload, *, argv=None, capsys=None):
    """Drive a hook ``main()`` with a stdin payload; return (rc, stdout)."""
    import io as _io

    sys.stdin = _io.StringIO(json.dumps(payload))
    old_argv = sys.argv
    sys.argv = ["hook.py", *(argv or [])]
    try:
        rc = module.main()
    finally:
        sys.argv = old_argv
    out = capsys.readouterr().out if capsys else ""
    return rc, out


# ── agent-activity ───────────────────────────────────────────────────


def _activity_file(tmp_path):
    # Fresh worktree (no legacy .gaia present) → modern .agentic-pr-dash default.
    return tmp_path / ".agentic-pr-dash" / "agent-activity.json"


def test_activity_user_prompt_starts_busy_turn(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": str(tmp_path), "owner_pid": os.getpid()}
    rc, _ = _run(run_agent_activity, payload, argv=["UserPromptSubmit"])
    assert rc == 0
    data = json.loads(_activity_file(tmp_path).read_text())
    rec = data["sessions"]["s1"]
    assert rec["state"] == "busy"
    assert rec["busy_since"]


def test_activity_stop_marks_idle(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    base = {"session_id": "s1", "cwd": str(tmp_path), "owner_pid": os.getpid()}
    _run(run_agent_activity, {**base, "hook_event_name": "UserPromptSubmit"}, argv=["UserPromptSubmit"])
    _run(run_agent_activity, {**base, "hook_event_name": "Stop"}, argv=["Stop"])
    rec = json.loads(_activity_file(tmp_path).read_text())["sessions"]["s1"]
    assert rec["state"] == "idle"
    assert rec["busy_since"] == ""


def test_activity_pretooluse_preserves_busy_since(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    base = {"session_id": "s1", "cwd": str(tmp_path), "owner_pid": os.getpid()}
    _run(run_agent_activity, {**base, "hook_event_name": "UserPromptSubmit"}, argv=["UserPromptSubmit"])
    started = json.loads(_activity_file(tmp_path).read_text())["sessions"]["s1"]["busy_since"]
    _run(run_agent_activity, {**base, "hook_event_name": "PreToolUse"}, argv=["PreToolUse"])
    rec = json.loads(_activity_file(tmp_path).read_text())["sessions"]["s1"]
    assert rec["state"] == "busy"
    assert rec["busy_since"] == started


def test_activity_untracked_event_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    payload = {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": str(tmp_path)}
    rc, _ = _run(run_agent_activity, payload, argv=["SessionStart"])
    assert rc == 0
    assert not _activity_file(tmp_path).exists()


def test_activity_two_sessions_coexist(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    _run(run_agent_activity, {"hook_event_name": "UserPromptSubmit", "session_id": "A", "cwd": str(tmp_path), "owner_pid": os.getpid()}, argv=["UserPromptSubmit"])
    _run(run_agent_activity, {"hook_event_name": "UserPromptSubmit", "session_id": "B", "cwd": str(tmp_path), "owner_pid": os.getpid()}, argv=["UserPromptSubmit"])
    # A's Stop must not clobber B's busy turn.
    _run(run_agent_activity, {"hook_event_name": "Stop", "session_id": "A", "cwd": str(tmp_path), "owner_pid": os.getpid()}, argv=["Stop"])
    sessions = json.loads(_activity_file(tmp_path).read_text())["sessions"]
    assert sessions["A"]["state"] == "idle"
    assert sessions["B"]["state"] == "busy"


def test_activity_waits_for_contended_lock_instead_of_overwriting_newer_snapshot(
    tmp_path,
    monkeypatch,
):
    """A writer queued past the old 0.5s limit must never write unlocked."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    activity_file = _activity_file(tmp_path)
    activity_file.parent.mkdir()
    activity_file.write_text(json.dumps({"sessions": {}}))
    lock_path = activity_file.parent / ".agent-activity.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)

    stale_sessions = {}

    def finish_first_writer():
        time.sleep(0.65)
        run_agent_activity._write_atomic(
            str(activity_file.parent),
            str(activity_file),
            {
                **stale_sessions,
                "first": {
                    "state": "busy",
                    "busy_since": "2026-07-21T00:00:00Z",
                    "updated": "2026-07-21T00:00:00Z",
                    "pid": os.getpid(),
                },
            },
        )
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    holder = threading.Thread(target=finish_first_writer)
    holder.start()
    _run(
        run_agent_activity,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "second",
            "cwd": str(tmp_path),
            "owner_pid": os.getpid(),
        },
        argv=["UserPromptSubmit"],
    )
    holder.join(timeout=2)

    assert not holder.is_alive()
    assert set(json.loads(activity_file.read_text())["sessions"]) == {
        "first",
        "second",
    }


@pytest.mark.parametrize("failure_mode", ["open", "acquire"])
def test_activity_lock_failure_preserves_existing_snapshot(
    tmp_path,
    monkeypatch,
    failure_mode,
):
    """An advisory hook may skip its event, but must not mutate without a lock."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    activity_file = _activity_file(tmp_path)
    activity_file.parent.mkdir()
    original = {
        "sessions": {
            "existing": {
                "state": "busy",
                "busy_since": "2026-07-21T00:00:00Z",
                "updated": "2026-07-21T00:00:00Z",
                "pid": os.getpid(),
            }
        }
    }
    activity_file.write_text(json.dumps(original))

    if failure_mode == "open":
        real_open = run_agent_activity.os.open

        def fail_lock_open(path, flags, mode=0o777):
            if os.fspath(path).endswith(".agent-activity.lock"):
                raise OSError("synthetic lock-open failure")
            return real_open(path, flags, mode)

        monkeypatch.setattr(run_agent_activity.os, "open", fail_lock_open)
    else:
        monkeypatch.setattr(run_agent_activity, "_acquire", lambda _fd: False)

    _run(
        run_agent_activity,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "new",
            "cwd": str(tmp_path),
            "owner_pid": os.getpid(),
        },
        argv=["UserPromptSubmit"],
    )

    assert json.loads(activity_file.read_text()) == original


def test_activity_stop_timeout_still_makes_dashboard_idle(tmp_path, monkeypatch):
    """A contended Stop must not leave a live session visibly busy forever."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    activity_file = _activity_file(tmp_path)
    activity_file.parent.mkdir()
    old = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    activity_file.write_text(
        json.dumps(
            {
                "sessions": {
                    "s1": {
                        "state": "busy",
                        "busy_since": old,
                        "updated": old,
                        "pid": os.getpid(),
                    }
                }
            }
        )
    )
    real_acquire = run_agent_activity._acquire
    monkeypatch.setattr(run_agent_activity, "_acquire", lambda _fd: False)

    _run(
        run_agent_activity,
        {
            "hook_event_name": "Stop",
            "session_id": "s1",
            "cwd": str(tmp_path),
            "owner_pid": os.getpid(),
        },
        argv=["Stop"],
    )

    assert app._legacy_agent_activity_state(str(tmp_path)) == "idle"

    monkeypatch.setattr(run_agent_activity, "_acquire", real_acquire)
    monkeypatch.setattr(app, "_ACTIVITY_DEBOUNCE_SECONDS", 0)
    _run(
        run_agent_activity,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s1",
            "cwd": str(tmp_path),
            "owner_pid": os.getpid(),
        },
        argv=["UserPromptSubmit"],
    )

    assert app._legacy_agent_activity_state(str(tmp_path)) == "working"


def test_activity_prunes_dead_pid_sibling(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    act_dir = tmp_path / ".agentic-pr-dash"
    act_dir.mkdir()
    # Seed a record owned by a definitely-dead pid.
    (act_dir / "agent-activity.json").write_text(json.dumps({"sessions": {"dead": {"state": "busy", "busy_since": "x", "updated": "x", "pid": 999999}}}))
    _run(run_agent_activity, {"hook_event_name": "UserPromptSubmit", "session_id": "live", "cwd": str(tmp_path), "owner_pid": os.getpid()}, argv=["UserPromptSubmit"])
    sessions = json.loads(_activity_file(tmp_path).read_text())["sessions"]
    assert "dead" not in sessions
    assert "live" in sessions


def test_activity_default_is_modern_state_dir(tmp_path, monkeypatch):
    """Fresh install with no legacy dir → stamps into .agentic-pr-dash, the same
    dir the dashboard reads (not the legacy .gaia)."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("AGENT_ACTIVITY_SUBDIR", raising=False)
    _run(run_agent_activity, {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": str(tmp_path), "owner_pid": os.getpid()}, argv=["UserPromptSubmit"])
    assert (tmp_path / ".agentic-pr-dash" / "agent-activity.json").exists()
    assert not (tmp_path / ".gaia").exists()


def test_activity_adopts_existing_legacy_dir(tmp_path, monkeypatch):
    """A pre-existing legacy .gaia is honored so installs keep one source of truth."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("AGENT_ACTIVITY_SUBDIR", raising=False)
    (tmp_path / ".gaia").mkdir()
    _run(run_agent_activity, {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": str(tmp_path), "owner_pid": os.getpid()}, argv=["UserPromptSubmit"])
    assert (tmp_path / ".gaia" / "agent-activity.json").exists()
    assert not (tmp_path / ".agentic-pr-dash").exists()


def test_activity_custom_subdir(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("AGENT_ACTIVITY_SUBDIR", ".activity")
    _run(run_agent_activity, {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": str(tmp_path), "owner_pid": os.getpid()}, argv=["UserPromptSubmit"])
    assert (tmp_path / ".activity" / "agent-activity.json").exists()


def test_activity_malformed_payload_is_best_effort(monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    sys.stdin = io.StringIO("{not json")
    sys.argv = ["hook.py", "UserPromptSubmit"]
    assert run_agent_activity.main() == 0  # unknown event from empty payload → noop


# ── model-dispatch logger ────────────────────────────────────────────


def test_dispatch_classifies_and_logs(tmp_path, monkeypatch, capsys):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(log))
    monkeypatch.delenv("MODEL_DISPATCH_BD_AUDIT", raising=False)
    payload = {"tool_name": "Agent", "tool_input": {"description": "Debug the failing test", "prompt": "", "model": "sonnet"}}
    rc, _ = _run(run_model_dispatch_logger, payload, capsys=capsys)
    assert rc == 0
    entry = json.loads(log.read_text().strip())
    assert entry["kind"] == "model_dispatch"
    assert entry["model"] == "sonnet-4.6"
    assert "task_type=debugging" in entry["prompt"]
    assert "fallback=true" in entry["prompt"]


def test_dispatch_explore_subagent_is_exploration(tmp_path, monkeypatch, capsys):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(log))
    payload = {"tool_name": "Agent", "tool_input": {"description": "anything", "subagent_type": "Explore", "model": "haiku"}}
    _run(run_model_dispatch_logger, payload, capsys=capsys)
    entry = json.loads(log.read_text().strip())
    assert "task_type=exploration" in entry["prompt"]
    assert entry["model"] == "haiku-4.5"


def test_dispatch_non_agent_tool_is_noop(tmp_path, monkeypatch, capsys):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(log))
    rc, _ = _run(run_model_dispatch_logger, {"tool_name": "Bash", "tool_input": {}}, capsys=capsys)
    assert rc == 0
    assert not log.exists()


def test_dispatch_skip_env_disables(tmp_path, monkeypatch, capsys):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(log))
    monkeypatch.setenv("SKIP_MODEL_DISPATCH", "1")
    _run(run_model_dispatch_logger, {"tool_name": "Agent", "tool_input": {"description": "build"}}, capsys=capsys)
    assert not log.exists()


def test_dispatch_default_log_path(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MODEL_DISPATCH_LOG", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    _run(run_model_dispatch_logger, {"tool_name": "Agent", "tool_input": {"description": "explore the code"}}, capsys=capsys)
    assert (tmp_path / ".beads" / "interactions.jsonl").exists()


def test_dispatch_codex_spawn_agent_logged(tmp_path, monkeypatch, capsys):
    """Codex subagent launches (spawn_agent / functions.spawn_agent) are logged,
    not just Claude's Agent tool."""
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(log))
    for tool in ("spawn_agent", "functions.spawn_agent"):
        log.unlink(missing_ok=True)
        payload = {"tool_name": tool, "tool_input": {"description": "Review the changes", "model": "sonnet"}}
        rc, _ = _run(run_model_dispatch_logger, payload, capsys=capsys)
        assert rc == 0
        entry = json.loads(log.read_text().strip())
        assert entry["kind"] == "model_dispatch"
        assert "task_type=code_review" in entry["prompt"]


def test_dispatch_classify_helpers():
    assert run_model_dispatch_logger.classify("", "", "Explore") == "exploration"
    assert run_model_dispatch_logger.classify("implement a feature", "", "") == "small_impl"
    assert run_model_dispatch_logger.classify("nothing matches here", "", "") == "general"
    assert run_model_dispatch_logger.resolve_model_name("") == "opus-4.6"
    assert run_model_dispatch_logger.resolve_model_name("gpt-5") == "gpt-5"
    assert run_model_dispatch_logger.is_fallback("debugging", "sonnet") is True
    assert run_model_dispatch_logger.is_fallback("debugging", "opus") is False


# ── model-dispatch ledger row stamping (BOU-2159) ────────────────────


def test_dispatch_stamps_source_and_cwd(tmp_path, monkeypatch, capsys):
    """Rows carry in-session provenance so scoped ledger replays can attribute
    Agent dispatches instead of skipping them."""
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(log))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("MODEL_DISPATCH_EXTRA", raising=False)
    payload = {"tool_name": "Agent", "tool_input": {"description": "Review the changes"}}
    rc, _ = _run(run_model_dispatch_logger, payload, capsys=capsys)
    assert rc == 0
    entry = json.loads(log.read_text().strip())
    assert entry["source"] == "session"
    assert entry["cwd"] == str(tmp_path)


def test_dispatch_cwd_falls_back_to_process_cwd(tmp_path, monkeypatch, capsys):
    """CLAUDE_PROJECT_DIR-first, os.getcwd() fallback — same resolution as the
    codex-dispatch logger."""
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(log))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("MODEL_DISPATCH_EXTRA", raising=False)
    monkeypatch.chdir(tmp_path)
    _run(run_model_dispatch_logger, {"tool_name": "Agent", "tool_input": {"description": "build"}}, capsys=capsys)
    entry = json.loads(log.read_text().strip())
    assert entry["source"] == "session"
    assert entry["cwd"] == os.getcwd()


def test_dispatch_extra_fields_round_trip(tmp_path, monkeypatch, capsys):
    """A wrapper hook injects verdict/task_type via MODEL_DISPATCH_EXTRA and the
    fields land on the single upstream-written row (no double-write)."""
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(log))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv(
        "MODEL_DISPATCH_EXTRA",
        json.dumps({"verdict": "no_findings", "task_type": "code_review"}),
    )
    payload = {"tool_name": "Agent", "tool_input": {"description": "Review the changes"}}
    rc, _ = _run(run_model_dispatch_logger, payload, capsys=capsys)
    assert rc == 0
    entry = json.loads(log.read_text().strip())
    assert entry["verdict"] == "no_findings"
    assert entry["task_type"] == "code_review"
    # Base shape and provenance stamps are intact alongside the extras.
    assert entry["kind"] == "model_dispatch"
    assert entry["response"] == "dispatched"
    assert entry["source"] == "session"
    assert entry["cwd"] == str(tmp_path)


def test_dispatch_extra_fields_cannot_override_reserved(tmp_path, monkeypatch, capsys):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(log))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv(
        "MODEL_DISPATCH_EXTRA",
        json.dumps({"source": "forged", "cwd": "/elsewhere", "kind": "bogus", "verdict": "found_criticals"}),
    )
    _run(run_model_dispatch_logger, {"tool_name": "Agent", "tool_input": {"description": "Review the changes"}}, capsys=capsys)
    entry = json.loads(log.read_text().strip())
    assert entry["source"] == "session"
    assert entry["cwd"] == str(tmp_path)
    assert entry["kind"] == "model_dispatch"
    assert entry["verdict"] == "found_criticals"


@pytest.mark.parametrize("bad", ["{not json", '"a string"', "[1, 2]", ""])
def test_dispatch_malformed_extra_ignored(tmp_path, monkeypatch, capsys, bad):
    """Malformed extension payloads never block logging: the row is still
    written with the provenance stamps and no extra keys."""
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(log))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_DISPATCH_EXTRA", bad)
    rc, _ = _run(run_model_dispatch_logger, {"tool_name": "Agent", "tool_input": {"description": "build"}}, capsys=capsys)
    assert rc == 0
    entry = json.loads(log.read_text().strip())
    assert entry["source"] == "session"
    assert set(entry) == {"kind", "timestamp", "model", "prompt", "response", "source", "cwd"}


def test_dispatch_extra_null_values_dropped(tmp_path, monkeypatch, capsys):
    """An unparsed verdict (null) stays absent — absent fields are the tolerant
    default for readers."""
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(log))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_DISPATCH_EXTRA", json.dumps({"verdict": None, "task_type": "code_review"}))
    _run(run_model_dispatch_logger, {"tool_name": "Agent", "tool_input": {"description": "Review the changes"}}, capsys=capsys)
    entry = json.loads(log.read_text().strip())
    assert "verdict" not in entry
    assert entry["task_type"] == "code_review"


def test_dispatch_legacy_row_shape_preserved(tmp_path, monkeypatch, capsys):
    """Stamping is purely additive: the legacy base keys keep their exact shape
    so pre-BOU-2159 readers keep parsing new rows, and readers written against
    the new shape must tolerate legacy rows missing source/cwd."""
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("MODEL_DISPATCH_LOG", str(log))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("MODEL_DISPATCH_EXTRA", raising=False)
    _run(run_model_dispatch_logger, {"tool_name": "Agent", "tool_input": {"description": "Debug the failing test", "model": "sonnet"}}, capsys=capsys)
    entry = json.loads(log.read_text().strip())
    # Legacy shape is a strict subset of the new row.
    assert {"kind", "timestamp", "model", "prompt", "response"} <= set(entry)
    assert entry["kind"] == "model_dispatch"
    assert entry["model"] == "sonnet-4.6"
    assert entry["response"] == "dispatched"
    # A legacy row (no source/cwd) is still valid input for tolerant readers.
    legacy = {"kind": "model_dispatch", "timestamp": "t", "model": "m", "prompt": "p", "response": "dispatched"}
    assert legacy.get("source") is None
    assert legacy.get("cwd") is None


# ── AgentFlow client ─────────────────────────────────────────────────


def test_runtime_files_override(monkeypatch, tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    monkeypatch.setenv("AGENTFLOW_RUNTIME_FILES", os.pathsep.join([str(a), str(b)]))
    assert agentflow.runtime_files() == [a, b]


def test_get_hub_url_reads_first_present(monkeypatch, tmp_path):
    f = tmp_path / "rt.json"
    f.write_text(json.dumps({"base_url": "http://hub:9000"}))
    monkeypatch.setenv("AGENTFLOW_RUNTIME_FILES", str(f))
    assert agentflow.get_hub_url() == "http://hub:9000"


def test_get_hub_url_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTFLOW_RUNTIME_FILES", str(tmp_path / "nope.json"))
    assert agentflow.get_hub_url() is None


def test_healthy_hub_skips_stale_first_file(monkeypatch, tmp_path):
    """A stale preferred runtime file pointing at a dead hub must not strand a
    healthy later file."""
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps({"base_url": "http://dead"}))
    b.write_text(json.dumps({"base_url": "http://live"}))
    monkeypatch.setenv("AGENTFLOW_RUNTIME_FILES", os.pathsep.join([str(a), str(b)]))
    monkeypatch.setattr(agentflow, "hub_is_healthy", lambda u: u == "http://live")
    assert agentflow.get_healthy_hub_url() == "http://live"


def test_healthy_hub_none_when_all_dead(monkeypatch, tmp_path):
    a = tmp_path / "a.json"
    a.write_text(json.dumps({"base_url": "http://dead"}))
    monkeypatch.setenv("AGENTFLOW_RUNTIME_FILES", str(a))
    monkeypatch.setattr(agentflow, "hub_is_healthy", lambda u: False)
    assert agentflow.get_healthy_hub_url() is None


def test_parse_permission_response():
    assert run_permission_agentflow.parse_permission_response("yes") is True
    assert run_permission_agentflow.parse_permission_response("ALLOW it") is True
    assert run_permission_agentflow.parse_permission_response("no") is False
    assert run_permission_agentflow.parse_permission_response("deny") is False


def test_format_tool_description_bash():
    desc = run_permission_agentflow.format_tool_description("Bash", {"command": "ls", "description": "list"})
    assert "Run command: `ls`" in desc and "list" in desc


# ── ask-user routing ─────────────────────────────────────────────────


def test_ask_user_wrong_tool_is_noop(monkeypatch, capsys):
    rc, out = _run(run_ask_user_agentflow, {"tool_name": "Bash"}, capsys=capsys)
    assert rc == 0 and out == ""


def test_ask_user_no_hub_falls_through(monkeypatch, capsys):
    monkeypatch.setattr(agentflow, "get_healthy_hub_url", lambda: None)
    payload = {"tool_name": "AskUserQuestion", "tool_input": {"questions": [{"question": "x"}]}}
    rc, out = _run(run_ask_user_agentflow, payload, capsys=capsys)
    assert rc == 0 and out == ""


def test_ask_user_returns_response_when_hub_replies(monkeypatch, capsys):
    monkeypatch.setattr(agentflow, "get_healthy_hub_url", lambda: "http://hub")
    monkeypatch.setattr(agentflow, "register_session", lambda u, n, source: "sess")
    monkeypatch.setattr(agentflow, "create_request", lambda *a, **k: "req")
    monkeypatch.setattr(agentflow, "await_response", lambda *a, **k: "Option A")
    payload = {"tool_name": "AskUserQuestion", "tool_input": {"questions": [{"question": "Pick", "options": [{"label": "A"}]}]}}
    rc, out = _run(run_ask_user_agentflow, payload, capsys=capsys)
    assert rc == 0
    parsed = json.loads(out.strip().splitlines()[-1])
    # Advisory: surface the answer as non-blocking context, never block/deny.
    assert "decision" not in parsed
    hso = parsed["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "Option A" in hso["additionalContext"]
    assert "permissionDecision" not in hso


def test_ask_user_forwards_all_questions(monkeypatch, capsys):
    """All 1–4 questions reach AgentFlow, not just the first."""
    monkeypatch.setattr(agentflow, "get_healthy_hub_url", lambda: "http://hub")
    monkeypatch.setattr(agentflow, "register_session", lambda u, n, source: "sess")
    sent = {}
    monkeypatch.setattr(agentflow, "create_request", lambda *a, **k: sent.update(k) or "req")
    monkeypatch.setattr(agentflow, "await_response", lambda *a, **k: "ok")
    payload = {"tool_name": "AskUserQuestion", "tool_input": {"questions": [
        {"question": "First?", "header": "Q1"},
        {"question": "Second?", "header": "Q2"},
    ]}}
    rc, _ = _run(run_ask_user_agentflow, payload, capsys=capsys)
    assert rc == 0
    assert "First?" in sent["question"] and "Second?" in sent["question"]


def test_ask_user_timeout_falls_through(monkeypatch, capsys):
    monkeypatch.setattr(agentflow, "get_healthy_hub_url", lambda: "http://hub")
    monkeypatch.setattr(agentflow, "register_session", lambda u, n, source: "sess")
    monkeypatch.setattr(agentflow, "create_request", lambda *a, **k: "req")
    monkeypatch.setattr(agentflow, "await_response", lambda *a, **k: None)
    payload = {"tool_name": "AskUserQuestion", "tool_input": {"questions": [{"question": "Pick"}]}}
    rc, out = _run(run_ask_user_agentflow, payload, capsys=capsys)
    assert rc == 0
    assert out == ""


# ── permission routing ───────────────────────────────────────────────


def test_permission_wrong_event_is_noop(monkeypatch, capsys):
    rc, out = _run(run_permission_agentflow, {"hook_event_name": "Stop"}, capsys=capsys)
    assert rc == 0 and out == ""


def test_permission_disabled_env_skips(monkeypatch, capsys):
    """DISABLE_AGENTFLOW forces the local dialog without probing the hub."""
    monkeypatch.delenv("FORCE_AGENTFLOW", raising=False)
    monkeypatch.setenv("DISABLE_AGENTFLOW", "1")
    called = {"hub": False}
    monkeypatch.setattr(agentflow, "get_healthy_hub_url", lambda: called.__setitem__("hub", True))
    payload = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    rc, out = _run(run_permission_agentflow, payload, capsys=capsys)
    assert rc == 0 and out == ""
    assert called["hub"] is False  # never even probes the hub


def test_permission_routes_by_default_no_tty_guard(monkeypatch, capsys):
    """No TTY guard: routing happens whenever a healthy hub exists (stdin is a
    pipe for every hook, so isatty must not gate this)."""
    monkeypatch.delenv("DISABLE_AGENTFLOW", raising=False)
    monkeypatch.delenv("FORCE_AGENTFLOW", raising=False)
    monkeypatch.setattr(agentflow, "get_healthy_hub_url", lambda: "http://hub")
    monkeypatch.setattr(agentflow, "register_session", lambda u, n, source: "sess")
    monkeypatch.setattr(agentflow, "create_request", lambda *a, **k: "req")
    monkeypatch.setattr(agentflow, "await_response", lambda *a, **k: "yes")
    payload = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    rc, out = _run(run_permission_agentflow, payload, capsys=capsys)
    assert rc == 0
    decision = json.loads(out.strip().splitlines()[-1])["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow"


def test_permission_allow_decision(monkeypatch, capsys):
    monkeypatch.setattr(agentflow, "get_healthy_hub_url", lambda: "http://hub")
    monkeypatch.setattr(agentflow, "register_session", lambda u, n, source: "sess")
    monkeypatch.setattr(agentflow, "create_request", lambda *a, **k: "req")
    monkeypatch.setattr(agentflow, "await_response", lambda *a, **k: "yes")
    payload = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    rc, out = _run(run_permission_agentflow, payload, capsys=capsys)
    decision = json.loads(out.strip().splitlines()[-1])["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow"


def test_permission_deny_decision(monkeypatch, capsys):
    monkeypatch.setattr(agentflow, "get_healthy_hub_url", lambda: "http://hub")
    monkeypatch.setattr(agentflow, "register_session", lambda u, n, source: "sess")
    monkeypatch.setattr(agentflow, "create_request", lambda *a, **k: "req")
    monkeypatch.setattr(agentflow, "await_response", lambda *a, **k: "no thanks")
    payload = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}
    rc, out = _run(run_permission_agentflow, payload, capsys=capsys)
    decision = json.loads(out.strip().splitlines()[-1])["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny"
