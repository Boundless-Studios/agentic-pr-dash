"""Tests for per-PR executor-failure streak tracking in loop.py (BOU-1789 Task 5)."""
from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest

from agentic_pr_dash import loop, config


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch, tmp_path):
    """Point daemon dir at tmp_path for health file isolation."""
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(tmp_path / "daemons"))
    monkeypatch.setenv("AGENTIC_PR_DASH_DAEMON_DIR", str(tmp_path / "daemons"))
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _health_file(tmp_path: Path) -> Path:
    # Repo-scoped filename — resolve through the real path helper.
    return loop._health_file(str(tmp_path))


def test_executor_failure_streak_starts_at_zero(tmp_path):
    """Before any failures, streak is 0."""
    cwd = str(tmp_path)
    assert loop.executor_failure_streak(cwd, 42) == 0


def test_record_executor_failure_increments_streak(tmp_path):
    """record_executor_failure increments the streak and returns the new count."""
    cwd = str(tmp_path)
    streak = loop.record_executor_failure(cwd, 42, "spawn failed")
    assert streak == 1
    assert loop.executor_failure_streak(cwd, 42) == 1


def test_record_executor_failure_accumulates(tmp_path):
    """Multiple failures accumulate correctly."""
    cwd = str(tmp_path)
    loop.record_executor_failure(cwd, 42, "err1")
    loop.record_executor_failure(cwd, 42, "err2")
    streak = loop.record_executor_failure(cwd, 42, "err3")
    assert streak == 3
    assert loop.executor_failure_streak(cwd, 42) == 3


def test_reset_executor_failure_clears_streak(tmp_path):
    """reset_executor_failure clears the streak for a PR."""
    cwd = str(tmp_path)
    loop.record_executor_failure(cwd, 42, "err")
    loop.record_executor_failure(cwd, 42, "err2")
    loop.reset_executor_failure(cwd, 42)
    assert loop.executor_failure_streak(cwd, 42) == 0


def test_streaks_are_per_pr(tmp_path):
    """Streak for PR 42 doesn't affect PR 99."""
    cwd = str(tmp_path)
    loop.record_executor_failure(cwd, 42, "err")
    loop.record_executor_failure(cwd, 42, "err")
    assert loop.executor_failure_streak(cwd, 99) == 0
    loop.record_executor_failure(cwd, 99, "err")
    assert loop.executor_failure_streak(cwd, 99) == 1
    assert loop.executor_failure_streak(cwd, 42) == 2


def test_health_file_persists(tmp_path):
    """Streak is persisted to the health JSON file."""
    cwd = str(tmp_path)
    loop.record_executor_failure(cwd, 42, "spawn error")
    hf = _health_file(tmp_path)
    assert hf.exists(), "Health file should be written"
    data = json.loads(hf.read_text())
    assert "42" in data
    assert data["42"]["streak"] == 1
    assert data["42"]["last_error"] == "spawn error"


def test_tick_increments_streak_on_spawn_failure(monkeypatch, tmp_path):
    """When _run_executor raises, _tick increments the failure streak."""
    wt = tmp_path / "wt"
    wt.mkdir()

    def fake_check(cmd, *a, **k):
        return types.SimpleNamespace(
            returncode=loop.CHECK_WORK_FOUND,
            stdout="fix prompt\nPR_NUMBER=7\n",
            stderr="",
        )

    def boom(executor, prompt, cwd):
        raise FileNotFoundError("executor not found")

    recorded_failures = []

    monkeypatch.setattr(loop, "_discover_cwds", lambda args: [str(wt)])
    monkeypatch.setattr(loop, "_cleanup_stale_no_pr_worktree", lambda cwd, session_id="": False)
    monkeypatch.setattr(loop, "_baseline_sha", lambda cwd, pr: "sha")
    monkeypatch.setattr(loop.subprocess, "run", lambda cmd, *a, **k: fake_check(cmd, *a, **k))
    monkeypatch.setattr(loop, "_run_executor", boom)
    monkeypatch.setattr(loop.coordinator, "heartbeat_claim_id", lambda *a, **k: None)
    monkeypatch.setattr(loop.coordinator, "release_claim_id", lambda *a, **k: None)
    monkeypatch.setattr(loop, "record_executor_failure",
                        lambda cwd, pr, err: recorded_failures.append((cwd, pr, err)) or 1)
    monkeypatch.setattr(loop, "_maybe_escalate", lambda *a, **k: None)

    args = types.SimpleNamespace(
        no_discover_worktrees=False, session_id="", cwd=[str(tmp_path)],
        fallback_executor="",
    )
    loop._tick(args, "codex {prompt}")
    assert any(pr == 7 for _, pr, _ in recorded_failures), "Should record failure for PR 7"


def test_tick_increments_streak_on_nonzero_exit(monkeypatch, tmp_path):
    """When executor exits non-zero, _tick increments the failure streak."""
    wt = tmp_path / "wt"
    wt.mkdir()

    def fake_run(cmd, *a, **k):
        return types.SimpleNamespace(
            returncode=loop.CHECK_WORK_FOUND,
            stdout="fix prompt\nPR_NUMBER=8\n",
            stderr="",
        )

    recorded_failures = []

    monkeypatch.setattr(loop, "_discover_cwds", lambda args: [str(wt)])
    monkeypatch.setattr(loop, "_cleanup_stale_no_pr_worktree", lambda cwd, session_id="": False)
    monkeypatch.setattr(loop, "_baseline_sha", lambda cwd, pr: "sha")
    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    monkeypatch.setattr(loop, "_run_executor", lambda executor, prompt, cwd: 1)  # non-zero
    monkeypatch.setattr(loop.coordinator, "heartbeat_claim_id", lambda *a, **k: None)
    monkeypatch.setattr(loop.coordinator, "release_claim_id", lambda *a, **k: None)
    monkeypatch.setattr(loop, "record_executor_failure",
                        lambda cwd, pr, err: recorded_failures.append((cwd, pr, err)) or 1)
    monkeypatch.setattr(loop, "_maybe_escalate", lambda *a, **k: None)

    args = types.SimpleNamespace(
        no_discover_worktrees=False, session_id="", cwd=[str(tmp_path)],
        fallback_executor="",
    )
    loop._tick(args, "codex {prompt}")
    assert any(pr == 8 for _, pr, _ in recorded_failures)


def test_tick_resets_streak_on_success(monkeypatch, tmp_path):
    """When executor succeeds, _tick resets the failure streak."""
    wt = tmp_path / "wt"
    wt.mkdir()

    calls = []

    def fake_run(cmd, *a, **k):
        if "check" in cmd:
            return types.SimpleNamespace(
                returncode=loop.CHECK_WORK_FOUND,
                stdout="fix prompt\nPR_NUMBER=9\n",
                stderr="",
            )
        # complete command
        calls.append(("complete", cmd))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    reset_calls = []

    monkeypatch.setattr(loop, "_discover_cwds", lambda args: [str(wt)])
    monkeypatch.setattr(loop, "_cleanup_stale_no_pr_worktree", lambda cwd, session_id="": False)
    monkeypatch.setattr(loop, "_baseline_sha", lambda cwd, pr: "sha")
    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    monkeypatch.setattr(loop, "_run_executor", lambda executor, prompt, cwd: 0)  # success
    monkeypatch.setattr(loop.coordinator, "heartbeat_claim_id", lambda *a, **k: None)
    monkeypatch.setattr(loop.coordinator, "release_claim_id", lambda *a, **k: None)
    monkeypatch.setattr(loop, "reset_executor_failure",
                        lambda cwd, pr: reset_calls.append((cwd, pr)))

    args = types.SimpleNamespace(
        no_discover_worktrees=False, session_id="", cwd=[str(tmp_path)],
        fallback_executor="",
    )
    loop._tick(args, "codex {prompt}")
    assert any(pr == 9 for _, pr in reset_calls), "Should reset streak on success"


def test_streak_is_namespaced_by_repo(tmp_path):
    """Two repos sharing the daemon dir must NOT collide on a bare PR number —
    repo A's PR #42 streak is invisible to repo B's PR #42 (review P2)."""
    repo_a = tmp_path / "repo-a"; repo_a.mkdir()
    repo_b = tmp_path / "repo-b"; repo_b.mkdir()

    loop.record_executor_failure(str(repo_a), 42, "boom")
    loop.record_executor_failure(str(repo_a), 42, "boom")

    assert loop.executor_failure_streak(str(repo_a), 42) == 2
    assert loop.executor_failure_streak(str(repo_b), 42) == 0, "repo B must not see repo A's streak"
    # Distinct on-disk files.
    assert loop._health_file(str(repo_a)) != loop._health_file(str(repo_b))


def test_clear_recovered_streak_clears_when_pr_clean(tmp_path, monkeypatch):
    """On a clean check, a recorded streak/marker for the worktree's PR is
    dropped without an executor dispatch (review P2: external recovery)."""
    cwd = str(tmp_path)
    loop.record_executor_failure(cwd, 42, "boom")
    assert loop.executor_failure_streak(cwd, 42) == 1

    # gh pr view --json number resolves the worktree's current (now-clean) PR.
    monkeypatch.setattr(
        loop.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout='{"number": 42}', stderr=""),
    )
    loop._clear_recovered_streak(cwd)
    assert loop.executor_failure_streak(cwd, 42) == 0


def test_clear_recovered_streak_skips_pr_resolution_without_state(tmp_path, monkeypatch):
    """No `gh pr view` PR-resolution when this repo has no recorded streak/
    escalation state (repo-slug detection may still run, and is cheap/cached)."""
    cmds = []
    def _spy(cmd, *a, **k):
        cmds.append(cmd)
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")
    monkeypatch.setattr(loop.subprocess, "run", _spy)
    loop._clear_recovered_streak(str(tmp_path))
    assert not any("pr" in c and "view" in c for c in cmds), \
        "must not resolve the PR via `gh pr view` when there is no recorded state"
