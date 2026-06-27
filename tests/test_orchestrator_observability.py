"""BOU-1801 — Orchestrator observability event emission.

Tests that the orchestrator emits structured observability events at key
lifecycle points (comment_scan, poll_tick, dispatch, state_transition) and
that all emission is best-effort / never raises into the caller.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_pr_dash import config, github_api, orchestrator
from agentic_pr_dash.models import PRData, PRStatus, ReviewComment, ThreadDecision
from agentic_pr_dash.observability import get_event_store


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _raw_pr(number: int = 42, branch: str = "feature/obs-test") -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "headRefName": branch,
        "baseRefName": "main",
        "url": f"https://github.com/org/repo/pull/{number}",
        "isDraft": False,
        "reviewDecision": "",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "labels": [],
        "createdAt": "2026-06-27T10:00:00Z",
    }


def _comment() -> ReviewComment:
    return ReviewComment(
        id=99,
        author="reviewer",
        body="needs work",
        path="src/foo.py",
        line=10,
        created_at="2026-06-27T10:00:00Z",
    )


def _decision(decision: str = "PICKED") -> ThreadDecision:
    return ThreadDecision(
        thread_id="t1",
        author="rev",
        created_at="2026-06-27T10:00:00Z",
        decision=decision,
    )


# --------------------------------------------------------------------------- #
# Fixture: isolate state_dir so events land under tmp_path
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path: Path):
    """Point AGENTIC_PR_DASH_STATE_DIR at tmp_path so the event store writes
    under the tmp directory and the lru_cache is cleared between tests."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENTIC_PR_DASH_STATE_DIR", str(state_dir))
    config.load.cache_clear()
    yield
    config.load.cache_clear()


# --------------------------------------------------------------------------- #
# Shared stubs for _enrich_pr boundary calls
# --------------------------------------------------------------------------- #


def _stub_enrich_boundaries(monkeypatch, tmp_repo: Path, *, comment_return=None):
    """Patch all github_api boundaries that _enrich_pr calls so tests stay fast
    and don't hit the real GitHub CLI."""
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr(42)])
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(github_api, "get_mergeability", lambda num, cwd=None: ("CLEAN", "MERGEABLE"))
    monkeypatch.setattr(
        github_api, "get_latest_commit", lambda num, cwd=None: ("sha123", "2026-06-27T10:00:00Z")
    )
    monkeypatch.setattr(github_api, "get_ci_checks", lambda num, cwd=None: [])
    scan_return = comment_return if comment_return is not None else ([], [])
    monkeypatch.setattr(
        github_api,
        "scan_review_threads",
        lambda num, latest, cwd=None: scan_return,
    )
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda branch, root=None: None)
    monkeypatch.setattr(orchestrator, "_resolve_maintenance_roots", lambda cwd: [str(tmp_repo)])


# --------------------------------------------------------------------------- #
# Test A — driven enrich: comment_scan and poll_tick events are emitted
# --------------------------------------------------------------------------- #


def test_enrich_emits_comment_scan_and_poll_tick(monkeypatch, tmp_path: Path):
    """_enrich_pr via refresh_prs emits 'comment_scan' (with decision) and
    'poll_tick' events into the event store keyed by repo root."""
    tmp_repo = tmp_path / "repo"
    tmp_repo.mkdir()

    decision = _decision("PICKED")
    _stub_enrich_boundaries(monkeypatch, tmp_repo, comment_return=([_comment()], [decision]))

    orch = orchestrator.Orchestrator(repo_cwd=str(tmp_repo))
    asyncio.run(orch.refresh_prs())

    store = get_event_store(str(tmp_repo))

    # comment_scan event should exist with decision in details
    scan_events = store.query(pr_number=42, kind="comment_scan")
    assert scan_events, "Expected at least one 'comment_scan' event"
    ev = scan_events[0]
    assert ev.details.get("picked") == 1
    decisions = ev.details.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "PICKED"
    assert decisions[0]["thread_id"] == "t1"

    # poll_tick event should exist
    tick_events = store.query(pr_number=42, kind="poll_tick")
    assert tick_events, "Expected at least one 'poll_tick' event"
    tick = tick_events[0]
    assert "status" in tick.details
    assert tick.details["comment_count"] == 1


# --------------------------------------------------------------------------- #
# Test B — best-effort: a raising event store must never propagate into callers
# --------------------------------------------------------------------------- #


def test_emit_is_best_effort_swallows_store_errors(monkeypatch, tmp_path: Path):
    """When get_event_store raises, _emit and log() must NOT raise."""
    tmp_repo = tmp_path / "repo"
    tmp_repo.mkdir()

    # Make get_event_store blow up on every call.
    def _exploding_store(cwd=None):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(orchestrator, "get_event_store", _exploding_store)

    orch = orchestrator.Orchestrator(repo_cwd=str(tmp_repo))

    # log() must not raise even when the event store explodes.
    orch.log("test message", pr_number=1)  # must not raise

    # _emit directly must not raise.
    orch._emit("some_kind", pr_number=1, details={"x": 1})  # must not raise

    # _enrich_pr must not raise (via refresh_prs) even with a broken store.
    _stub_enrich_boundaries(monkeypatch, tmp_repo)
    asyncio.run(orch.refresh_prs())  # must not raise


# --------------------------------------------------------------------------- #
# Test C — direct _emit: event lands; if store raises, it's swallowed silently
# --------------------------------------------------------------------------- #


def test_emit_writes_event_to_store(monkeypatch, tmp_path: Path):
    """_emit writes an ObservabilityEvent that can be read back via get_event_store."""
    tmp_repo = tmp_path / "repo"
    tmp_repo.mkdir()

    orch = orchestrator.Orchestrator(repo_cwd=str(tmp_repo))
    orch._emit("test_kind", pr_number=7, repo=str(tmp_repo), details={"foo": "bar"})

    store = get_event_store(str(tmp_repo))
    events = store.query(pr_number=7, kind="test_kind")
    assert len(events) == 1
    assert events[0].kind == "test_kind"
    assert events[0].details == {"foo": "bar"}


def test_emit_swallows_when_store_raises(monkeypatch, tmp_path: Path):
    """_emit with a raising get_event_store never propagates the exception."""
    tmp_repo = tmp_path / "repo"
    tmp_repo.mkdir()

    monkeypatch.setattr(orchestrator, "get_event_store", lambda cwd=None: (_ for _ in ()).throw(RuntimeError("boom")))

    orch = orchestrator.Orchestrator(repo_cwd=str(tmp_repo))
    # Must not raise:
    orch._emit("some_event", pr_number=5, details={"k": "v"})


# --------------------------------------------------------------------------- #
# Test D — log() write-through: state_transition event emitted without recursion
# --------------------------------------------------------------------------- #


def test_log_emits_state_transition_event(monkeypatch, tmp_path: Path):
    """log() writes both to the in-memory ring AND emits a 'state_transition' event."""
    tmp_repo = tmp_path / "repo"
    tmp_repo.mkdir()

    orch = orchestrator.Orchestrator(repo_cwd=str(tmp_repo))
    orch.log("PR is now clean", pr_number=99, level="success")

    # In-memory ring updated.
    assert any(e.message == "PR is now clean" for e in orch.events)

    # Observability event emitted.
    store = get_event_store(str(tmp_repo))
    events = store.query(pr_number=99, kind="state_transition")
    assert events, "Expected at least one 'state_transition' event"
    ev = events[0]
    assert ev.details["message"] == "PR is now clean"
    assert ev.details["level"] == "success"


def test_log_does_not_recurse_when_emit_fails(monkeypatch, tmp_path: Path):
    """_emit must NOT call log(); if it did the chain would recurse infinitely."""
    tmp_repo = tmp_path / "repo"
    tmp_repo.mkdir()

    call_count = [0]
    original_emit = orchestrator.Orchestrator._emit

    def counting_emit(self, kind, **kwargs):
        call_count[0] += 1
        if call_count[0] > 10:
            raise AssertionError("_emit called more than 10 times — recursion detected")
        original_emit(self, kind, **kwargs)

    monkeypatch.setattr(orchestrator.Orchestrator, "_emit", counting_emit)

    orch = orchestrator.Orchestrator(repo_cwd=str(tmp_repo))
    orch.log("hello", pr_number=1)
    # log() should trigger exactly one _emit call, not an infinite chain.
    assert call_count[0] == 1


# --------------------------------------------------------------------------- #
# Test E — scan_review_threads with empty decisions still emits comment_scan
# --------------------------------------------------------------------------- #


def test_comment_scan_event_emitted_with_empty_decisions(monkeypatch, tmp_path: Path):
    """When scan_review_threads returns ([], []), comment_scan event still fires."""
    tmp_repo = tmp_path / "repo"
    tmp_repo.mkdir()

    _stub_enrich_boundaries(monkeypatch, tmp_repo, comment_return=([], []))

    orch = orchestrator.Orchestrator(repo_cwd=str(tmp_repo))
    asyncio.run(orch.refresh_prs())

    store = get_event_store(str(tmp_repo))
    events = store.query(pr_number=42, kind="comment_scan")
    assert events, "Expected 'comment_scan' event even with empty results"
    assert events[0].details["picked"] == 0
    assert events[0].details["decisions"] == []
