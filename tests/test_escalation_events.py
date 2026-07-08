"""BOU-1880: escalation failure-injection proof + events.jsonl persistence.

Drives the REAL streak→escalation path (record_executor_failure + _maybe_escalate,
as _tick runs them) with injected failures to threshold — NOT stubbing
_maybe_escalate — and asserts the end-to-end side-effects: marker written, notify
fired once, AND streak/escalation events persisted to the decoupled events.jsonl.
"""
from __future__ import annotations

import json

import pytest

from agentic_pr_dash import config, iterm, loop
from agentic_pr_dash.observability.event_store import get_event_store


@pytest.fixture(autouse=True)
def _isolation(monkeypatch, tmp_path):
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(tmp_path / "daemons"))
    monkeypatch.setenv("AGENTIC_PR_DASH_DAEMON_DIR", str(tmp_path / "daemons"))
    monkeypatch.setenv("AGENTIC_PR_DASH_ESCALATION_THRESHOLD", "3")
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def test_failure_injection_escalation_emits_events(tmp_path, monkeypatch):
    cwd = str(tmp_path)
    notify_calls: list = []
    monkeypatch.setattr(iterm, "notify", lambda t, m: notify_calls.append((t, m)) or True)

    # Inject 3 consecutive executor failures for PR 42 — the exact streak→escalation
    # sequence _tick runs on a failing dispatch, with NO stubbing of _maybe_escalate.
    for _ in range(3):
        streak = loop.record_executor_failure(cwd, 42, "spawn failed")
        loop._maybe_escalate(cwd, 42, "spawn failed", streak)

    # (a) escalation marker written at threshold
    marker = loop._escalated_marker_path(cwd)
    assert marker.exists()
    assert json.loads(marker.read_text(encoding="utf-8"))["42"]["streak"] == 3

    # (b) notify fired exactly once (edge-triggered at the crossing)
    assert len(notify_calls) == 1

    # (c) BOU-1880: events.jsonl persists the 3 streak events + 1 escalation
    store = get_event_store(cwd)
    assert len(store.query(kind="streak", pr_number=42)) == 3
    esc = store.query(kind="escalation", pr_number=42)
    assert len(esc) == 1
    assert esc[0].details.get("streak") == 3
    assert esc[0].details.get("threshold") == 3


def test_streak_reset_and_recovery_stops_events(tmp_path, monkeypatch):
    # A recovered PR clears its escalation; no further escalation event is emitted.
    cwd = str(tmp_path)
    monkeypatch.setattr(iterm, "notify", lambda t, m: True)
    for _ in range(3):
        streak = loop.record_executor_failure(cwd, 42, "spawn failed")
        loop._maybe_escalate(cwd, 42, "spawn failed", streak)
    loop.reset_executor_failure(cwd, 42)
    # A fresh failure post-recovery starts a NEW streak at 1 → below threshold → no escalation.
    streak = loop.record_executor_failure(cwd, 42, "spawn failed")
    loop._maybe_escalate(cwd, 42, "spawn failed", streak)
    assert streak == 1
    assert len(get_event_store(cwd).query(kind="escalation", pr_number=42)) == 1  # still just the first
