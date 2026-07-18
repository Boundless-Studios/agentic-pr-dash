"""BOU-1987 (fix 3) — surface the underlying gh failure cause in the
skip-refresh event instead of a bare "GitHub API unavailable".

During the 2026-07-11 outage the board logged only "could not list open PRs
(GitHub API unavailable)" for 9 hours while the actual causes were first a
DNS failure and then an expired App token (HTTP 401) — two very different
remediations. `GhFailure` already captures the stderr; these tests pin a
one-line classified `summary()` and its inclusion in the orchestrator's
skip-refresh event message.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentic_pr_dash import config, github_api, orchestrator
from agentic_pr_dash.observability import get_event_store


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENTIC_PR_DASH_STATE_DIR", str(state_dir))
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _failure(stderr: str, reason: str = "exit", returncode: int = 1) -> github_api.GhFailure:
    return github_api.GhFailure(
        command=["gh", "pr", "list"], returncode=returncode, stderr=stderr, reason=reason,
    )


# --------------------------------------------------------------------------- #
# GhFailure.summary() classification
# --------------------------------------------------------------------------- #


def test_summary_classifies_auth_failure():
    summary = _failure("HTTP 401: Bad credentials (https://api.github.com/graphql)").summary()
    assert summary.startswith("auth failure")
    assert "401" in summary


def test_summary_classifies_dns_network_failure():
    summary = _failure(
        "error connecting to api.github.com\ncheck your internet connection or https://githubstatus.com"
    ).summary()
    assert summary.startswith("network unreachable")


def test_summary_classifies_rate_limit():
    summary = _failure("API rate limit exceeded for installation ID 145583348").summary()
    assert summary.startswith("rate-limited")


def test_summary_classifies_timeout():
    summary = _failure("gh timed out after 30s: gh pr list").summary()
    assert summary.startswith("timeout")


def test_summary_falls_back_to_first_stderr_line_and_truncates():
    summary = _failure("mystery failure " + "x" * 300 + "\nsecond line").summary()
    assert summary.startswith("mystery failure")
    assert "second line" not in summary
    assert len(summary) <= 160


def test_summary_surfaces_non_exit_reason():
    summary = _failure("gh returned non-JSON output: 'oops'", reason="invalid-json").summary()
    assert "invalid-json" in summary


# --------------------------------------------------------------------------- #
# Orchestrator skip-refresh event includes the classified cause
# --------------------------------------------------------------------------- #


def test_skip_refresh_event_includes_failure_summary(monkeypatch, tmp_path: Path):
    tmp_repo = tmp_path / "repo"
    tmp_repo.mkdir()

    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: None)
    monkeypatch.setattr(
        github_api,
        "last_list_open_prs_failure",
        lambda: _failure("HTTP 401: Bad credentials (https://api.github.com/graphql)"),
    )
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(orchestrator, "_resolve_maintenance_roots", lambda cwd: [str(tmp_repo)])

    orch = orchestrator.Orchestrator(repo_cwd=str(tmp_repo))
    asyncio.run(orch.refresh_prs())

    events = get_event_store(str(tmp_repo)).query(kind="state_transition")
    skip_events = [
        ev for ev in events if "could not list open PRs" in ev.details.get("message", "")
    ]
    assert skip_events, "Expected a skip-refresh state_transition event"
    message = skip_events[0].details["message"]
    assert "auth failure" in message
    assert "401" in message


def test_skip_refresh_event_survives_missing_failure_detail(monkeypatch, tmp_path: Path):
    """No recorded failure (e.g. race) must not break the skip event."""
    tmp_repo = tmp_path / "repo"
    tmp_repo.mkdir()

    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: None)
    monkeypatch.setattr(github_api, "last_list_open_prs_failure", lambda: None)
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(orchestrator, "_resolve_maintenance_roots", lambda cwd: [str(tmp_repo)])

    orch = orchestrator.Orchestrator(repo_cwd=str(tmp_repo))
    asyncio.run(orch.refresh_prs())

    events = get_event_store(str(tmp_repo)).query(kind="state_transition")
    skip_events = [
        ev for ev in events if "could not list open PRs" in ev.details.get("message", "")
    ]
    assert skip_events, "Expected a skip-refresh state_transition event"
    assert "GitHub API unavailable" in skip_events[0].details["message"]
