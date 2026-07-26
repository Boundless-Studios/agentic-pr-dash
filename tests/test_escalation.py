"""Tests for escalation: edge-triggered at threshold, notify, marker, badge (BOU-1789 Task 7)."""
from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest

from agentic_pr_dash import config, loop
from agentic_pr_dash import iterm


@pytest.fixture(autouse=True)
def _isolation(monkeypatch, tmp_path):
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(tmp_path / "daemons"))
    monkeypatch.setenv("AGENTIC_PR_DASH_DAEMON_DIR", str(tmp_path / "daemons"))
    monkeypatch.setenv("AGENTIC_PR_DASH_ESCALATION_THRESHOLD", "3")
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _escalation_marker_path(tmp_path: Path) -> Path:
    # Repo-scoped filename — resolve through the real path helper so the test
    # tracks the namespacing instead of hardcoding the bare name.
    return loop._escalated_marker_path(str(tmp_path))


def test_maybe_escalate_fires_at_threshold(tmp_path, monkeypatch):
    """_maybe_escalate fires exactly at streak == threshold (not below, not above)."""
    cwd = str(tmp_path)
    notify_calls = []
    monkeypatch.setattr(iterm, "notify", lambda title, msg: notify_calls.append((title, msg)) or True)

    # Below threshold — no notification
    loop._maybe_escalate(cwd, 42, "error", 1)
    loop._maybe_escalate(cwd, 42, "error", 2)
    assert notify_calls == [], "Should not escalate below threshold"

    # At threshold — fires
    loop._maybe_escalate(cwd, 42, "error", 3)
    assert len(notify_calls) == 1, "Should fire exactly once at threshold"
    assert "42" in notify_calls[0][0] or "42" in notify_calls[0][1]

    # Above threshold — does NOT re-fire
    loop._maybe_escalate(cwd, 42, "error", 4)
    loop._maybe_escalate(cwd, 42, "error", 5)
    assert len(notify_calls) == 1, "Should not re-fire above threshold"


def test_maybe_escalate_writes_marker(tmp_path, monkeypatch):
    """At threshold, _maybe_escalate writes the escalation marker JSON."""
    cwd = str(tmp_path)
    monkeypatch.setattr(iterm, "notify", lambda title, msg: True)

    loop._maybe_escalate(cwd, 42, "spawn failed", 3)

    marker = _escalation_marker_path(tmp_path)
    assert marker.exists(), "Escalation marker should be written"
    data = json.loads(marker.read_text())
    assert "42" in data
    assert data["42"]["streak"] == 3
    assert "spawn failed" in data["42"]["last_error"]


def test_maybe_escalate_calls_notify(tmp_path, monkeypatch):
    """At threshold, _maybe_escalate calls iterm.notify exactly once."""
    cwd = str(tmp_path)
    notify_calls = []
    monkeypatch.setattr(iterm, "notify", lambda title, msg: notify_calls.append((title, msg)) or True)

    loop._maybe_escalate(cwd, 7, "error", 3)
    assert len(notify_calls) == 1
    # Title/message should mention the PR
    title, msg = notify_calls[0]
    assert "7" in title or "7" in msg


def test_tick_escalates_at_threshold(monkeypatch, tmp_path):
    """_tick triggers escalation when record_executor_failure returns the threshold."""
    wt = tmp_path / "wt"
    wt.mkdir()

    def fake_run(cmd, *a, **k):
        return types.SimpleNamespace(
            returncode=loop.CHECK_WORK_FOUND,
            stdout="fix prompt\nPR_NUMBER=42\n",
            stderr="",
        )

    escalate_calls = []
    monkeypatch.setattr(loop, "_discover_cwds", lambda args: [str(wt)])
    monkeypatch.setattr(loop, "_cleanup_stale_no_pr_worktree", lambda cwd, session_id="": False)
    monkeypatch.setattr(loop, "_baseline_sha", lambda cwd, pr: "sha")
    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    monkeypatch.setattr(loop, "_run_executor", lambda executor, prompt, cwd: 1)  # fail
    # BOU-2417: escalation requires a GENUINE failure, i.e. a reachable network.
    # This test stubs loop.subprocess.run wholesale, which the reachability
    # probe would otherwise pick up as its own (fake) result.
    monkeypatch.setattr(loop, "_network_reachable", lambda: True)
    monkeypatch.setattr(loop.coordinator, "heartbeat_claim", lambda *a, **k: None)
    monkeypatch.setattr(loop.coordinator, "release_claim", lambda *a, **k: None)
    # Record failure returns threshold
    monkeypatch.setattr(loop, "record_executor_failure",
                        lambda cwd, pr, err: 3)  # returns threshold
    monkeypatch.setattr(loop, "_maybe_escalate",
                        lambda cwd, pr, err, streak: escalate_calls.append((pr, streak)))

    args = types.SimpleNamespace(
        no_discover_worktrees=False, session_id="", cwd=[str(tmp_path)],
        fallback_executor="",
    )
    loop._tick(args, "codex {prompt}")
    assert any(pr == 42 and streak == 3 for pr, streak in escalate_calls)


def test_iterm_notify_best_effort_no_exception(monkeypatch):
    """iterm.notify swallows OSError and returns False without raising."""
    import subprocess as _subprocess
    monkeypatch.setattr(_subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("osascript not found")))
    # Should not raise
    result = iterm.notify("title", "message")
    assert result is False


def test_pr_data_escalated_field_default():
    """PRData.escalated defaults to False."""
    from agentic_pr_dash.models import PRData
    pr = PRData(number=1, title="T", branch="b", url="http://x.com/1")
    assert pr.escalated is False
    assert pr.escalated_reason is None


def test_reset_executor_failure_clears_escalation_marker(tmp_path, monkeypatch):
    """A recovered PR must drop out of the escalation marker so the stop-gate
    stops surfacing the escalation block (BOU-1789 false-positive-after-recovery)."""
    cwd = str(tmp_path)
    monkeypatch.setattr(iterm, "notify", lambda title, msg: True)

    # Escalate two PRs, then recover only #42.
    loop._maybe_escalate(cwd, 42, "boom", 3)
    loop._maybe_escalate(cwd, 99, "boom", 3)
    marker = _escalation_marker_path(tmp_path)
    assert set(json.loads(marker.read_text())) == {"42", "99"}

    loop.reset_executor_failure(cwd, 42)
    assert set(json.loads(marker.read_text())) == {"99"}, "Only #42 should be cleared"

    # Recovering the last PR removes the marker file entirely.
    loop.reset_executor_failure(cwd, 99)
    assert not marker.exists(), "Marker file should be removed when empty"


def test_maybe_escalate_handles_non_dict_marker(tmp_path, monkeypatch):
    """A valid-JSON-but-non-dict marker (e.g. `[]` from a bad write) must not
    raise TypeError and abort the loop at the escalation moment (review P2)."""
    cwd = str(tmp_path)
    monkeypatch.setattr(iterm, "notify", lambda title, msg: True)
    marker = _escalation_marker_path(tmp_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("[]", encoding="utf-8")  # non-dict content

    # Must not raise.
    loop._maybe_escalate(cwd, 42, "boom", 3)

    data = json.loads(marker.read_text())
    assert isinstance(data, dict) and "42" in data
