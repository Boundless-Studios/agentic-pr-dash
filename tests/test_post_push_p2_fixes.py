"""Regression tests for the BOU-1670 Codex P2 review findings (PR #26).

Each test pins a behavior the Codex bot flagged on the post-push CI-watch
hook + the generic ``ci_watch`` primitive:

  #1  paginate check runs
  #2  do not skip a pushed segment when a LATER &&-segment fails
  #3  do not arm for a standalone push that itself failed
  #4  treat every non-completed check status as still-pending
  #5  never SIGKILL a recycled (non-watcher) pid
  #6  no-checks is a non-blocking terminal state, not a timeout
  #7  non-success completed conclusions block
  #8  relative result/pid paths anchor under the pushed worktree
  #9  review-comment freshness keys off the pushed sha's local commit date
  #10 commit statuses are folded into the SHA snapshot
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from agentic_pr_dash import ci_watch, github_api
from agentic_pr_dash.codex_hooks import run_post_push_watch as hook


def _arm_via_hook(monkeypatch, payload, *, argv=("PostToolUse",)):
    armed: list = []
    monkeypatch.setattr(ci_watch, "arm_post_push_watch", lambda cfg: armed.append(cfg) or 0)
    monkeypatch.setattr(sys, "argv", ["run_post_push_watch.py", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hook.main() == 0
    return armed[0] if armed else None


def _cfg(tmp_path, **kw):
    return ci_watch.CIWatchConfig(
        project_dir=tmp_path,
        results_file=tmp_path / ".claude" / "ci-watch-latest.json",
        pid_file=tmp_path / ".claude" / "ci-watch.pid",
        **kw,
    )


# --- #2: a later failing segment must not skip an already-run push ---------

def test_push_arms_when_a_later_and_segment_fails(monkeypatch, tmp_path):
    # `git push && gh pr create` exiting non-zero means gh pr create failed,
    # but the push already landed (a failed push short-circuits the &&).
    payload = {"tool_name": "exec_command",
               "tool_input": {"cmd": "git push && gh pr create --fill"},
               "cwd": str(tmp_path), "tool_response": {"exit_code": 1}}
    cfg = _arm_via_hook(monkeypatch, payload)
    assert cfg is not None
    assert cfg.project_dir == tmp_path


def test_push_arms_when_trailing_notify_fails(monkeypatch, tmp_path):
    payload = {"tool_name": "exec_command",
               "tool_input": {"cmd": "make test && git push && notify"},
               "cwd": str(tmp_path), "tool_response": {"exit_code": 1}}
    cfg = _arm_via_hook(monkeypatch, payload)
    assert cfg is not None


# --- #3: a standalone failed push must NOT arm -----------------------------

def test_standalone_failed_push_does_not_arm(monkeypatch, tmp_path):
    # Non-fast-forward rejection: lone push exits non-zero → nothing landed.
    payload = {"tool_name": "exec_command",
               "tool_input": {"cmd": "git push"}, "cwd": str(tmp_path),
               "tool_response": {"exit_code": 1}}
    assert _arm_via_hook(monkeypatch, payload) is None


def test_final_push_in_chain_failed_does_not_arm(monkeypatch, tmp_path):
    payload = {"tool_name": "exec_command",
               "tool_input": {"cmd": "make build && git push"},
               "cwd": str(tmp_path), "tool_response": {"exit_code": 1}}
    assert _arm_via_hook(monkeypatch, payload) is None


def test_successful_push_still_arms(monkeypatch, tmp_path):
    payload = {"tool_name": "exec_command",
               "tool_input": {"cmd": "git push"}, "cwd": str(tmp_path),
               "tool_response": {"exit_code": 0}}
    assert _arm_via_hook(monkeypatch, payload) is not None


# --- #4: requested / waiting / pending statuses keep the poll alive --------

def test_snapshot_requested_status_is_pending(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_watch.github_api, "get_check_runs_for_commit",
                        lambda sha, cwd=None: [{"name": "Build", "status": "requested", "conclusion": None}])
    assert ci_watch.snapshot_ci("abc", tmp_path)["status"] == "pending"


def test_poll_treats_waiting_as_in_progress(monkeypatch, tmp_path):
    seq = iter([
        [{"name": "Build", "status": "waiting", "conclusion": None}],
        [{"name": "Build", "status": "completed", "conclusion": "success"}],
    ])
    monkeypatch.setattr(ci_watch.github_api, "get_check_runs_for_commit",
                        lambda sha, cwd=None: next(seq))
    cfg = _cfg(tmp_path, initial_delay_s=0, poll_interval_s=0)
    checks, outcome = ci_watch.poll_checks_for_commit("abc", cfg)
    assert outcome == ci_watch.POLL_DONE
    assert checks[0]["status"] == "completed"


# --- #5: never SIGKILL a recycled (non-watcher) pid ------------------------

def test_kill_previous_watcher_spares_foreign_pid(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.pid_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.pid_file.write_text("4242")
    monkeypatch.setattr(ci_watch, "_is_our_watcher", lambda pid: False)
    killed: list[int] = []
    monkeypatch.setattr(ci_watch.os, "kill", lambda pid, sig: killed.append(pid))
    ci_watch.kill_previous_watcher(cfg)
    assert killed == []  # foreign pid spared
    assert not cfg.pid_file.exists()  # stale pidfile cleared


def test_kill_previous_watcher_kills_our_watcher(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.pid_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.pid_file.write_text("4242")
    monkeypatch.setattr(ci_watch, "_is_our_watcher", lambda pid: True)
    killed: list[int] = []
    monkeypatch.setattr(ci_watch.os, "kill", lambda pid, sig: killed.append(pid))
    ci_watch.kill_previous_watcher(cfg)
    assert killed == [4242]


# --- #6: no-checks is a non-blocking terminal state ------------------------

def test_background_watch_no_checks_is_not_blocking(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_watch.github_api, "get_check_runs_for_commit", lambda sha, cwd=None: [])
    monkeypatch.setattr(ci_watch.github_api, "get_repo_info", lambda cwd=None: ("o", "r"))
    monkeypatch.setattr(ci_watch, "_unaddressed_comments", lambda pr, sha, d: [])
    # NO_CHECKS_GRACE_S=0 so empty checks resolve to no_checks on the second look.
    monkeypatch.setattr(ci_watch, "NO_CHECKS_GRACE_S", 0)
    cfg = _cfg(tmp_path, initial_delay_s=0, poll_interval_s=0, watch_timeout_s=5)
    ci_watch.background_watch("deadbeef", 3, "feat", cfg)
    results = json.loads(cfg.results_file.read_text())
    assert results["status"] == "no_checks"
    assert results["action_needed"] is False


# --- #7: non-success completed conclusions block ---------------------------

def test_snapshot_action_required_is_failing(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_watch.github_api, "get_check_runs_for_commit",
                        lambda sha, cwd=None: [{"name": "Deploy", "status": "completed", "conclusion": "action_required"}])
    snap = ci_watch.snapshot_ci("abc", tmp_path)
    assert snap["status"] == "failing"
    assert snap["code_failures"] == ["Deploy"]


def test_background_watch_stale_conclusion_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_watch.github_api, "get_check_runs_for_commit",
                        lambda sha, cwd=None: [{"name": "Lint", "status": "completed", "conclusion": "stale"}])
    monkeypatch.setattr(ci_watch.github_api, "get_failed_logs", lambda sha, names, cwd=None: {})
    monkeypatch.setattr(ci_watch.github_api, "get_repo_info", lambda cwd=None: ("o", "r"))
    monkeypatch.setattr(ci_watch, "_unaddressed_comments", lambda pr, sha, d: [])
    cfg = _cfg(tmp_path, initial_delay_s=0, poll_interval_s=0)
    ci_watch.background_watch("deadbeef", 8, "feat", cfg)
    results = json.loads(cfg.results_file.read_text())
    assert results["status"] == "failing"
    assert results["action_needed"] is True


def test_neutral_and_skipped_conclusions_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_watch.github_api, "get_check_runs_for_commit",
                        lambda sha, cwd=None: [
                            {"name": "A", "status": "completed", "conclusion": "neutral"},
                            {"name": "B", "status": "completed", "conclusion": "skipped"},
                        ])
    assert ci_watch.snapshot_ci("abc", tmp_path)["status"] == "passing"


# --- #8: relative result/pid paths anchor under the pushed worktree --------

def test_relative_paths_anchor_under_project_dir(tmp_path):
    cfg = ci_watch.CIWatchConfig.from_env({
        "CI_WATCH_PROJECT_DIR": str(tmp_path),
        "CI_WATCH_RESULTS_FILE": ".claude/ci-watch-latest.json",
        "CI_WATCH_PID_FILE": ".claude/ci-watch.pid",
    })
    assert cfg.results_file == tmp_path / ".claude" / "ci-watch-latest.json"
    assert cfg.pid_file == tmp_path / ".claude" / "ci-watch.pid"


def test_absolute_path_override_is_respected(tmp_path):
    abs = tmp_path / "elsewhere" / "r.json"
    cfg = ci_watch.CIWatchConfig.from_env({
        "CI_WATCH_PROJECT_DIR": str(tmp_path),
        "CI_WATCH_RESULTS_FILE": str(abs),
    })
    assert cfg.results_file == abs


# --- #9: review-comment freshness keys off the pushed sha ------------------

def test_unaddressed_comments_uses_pushed_sha_date(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_watch, "commit_date", lambda d, sha: "2026-06-01T00:00:00Z")
    seen: dict = {}

    def fake_get(pr, since, cwd=None):
        seen["since"] = since
        return []

    monkeypatch.setattr(ci_watch.github_api, "get_unaddressed_comments", fake_get)
    # Must NOT fall back to get_latest_commit when the sha date resolves.
    monkeypatch.setattr(ci_watch.github_api, "get_latest_commit",
                        lambda pr, cwd=None: (_ for _ in ()).throw(AssertionError("should not be called")))
    ci_watch._unaddressed_comments(5, "deadbeef", tmp_path)
    assert seen["since"] == "2026-06-01T00:00:00Z"


def test_unaddressed_comments_falls_back_to_latest_commit(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_watch, "commit_date", lambda d, sha: "")
    monkeypatch.setattr(ci_watch.github_api, "get_latest_commit", lambda pr, cwd=None: ("sha", "2026-05-01T00:00:00Z"))
    seen: dict = {}
    monkeypatch.setattr(ci_watch.github_api, "get_unaddressed_comments",
                        lambda pr, since, cwd=None: seen.update(since=since) or [])
    ci_watch._unaddressed_comments(5, "", tmp_path)
    assert seen["since"] == "2026-05-01T00:00:00Z"


# --- #1: check runs are paginated ------------------------------------------

def test_check_runs_request_is_paginated(monkeypatch):
    seen: dict = {}

    def fake_run(cmd, timeout_s=20, cwd=None):
        import subprocess
        if "/check-runs" in " ".join(cmd):
            seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(github_api, "_run", fake_run)
    github_api.get_check_runs_for_commit("abc", cwd=None)
    assert "--paginate" in seen["cmd"]


# --- #10: commit statuses are folded into the snapshot ---------------------

def test_commit_statuses_folded_into_snapshot(monkeypatch):
    def fake_run(cmd, timeout_s=20, cwd=None):
        import subprocess
        joined = " ".join(cmd)
        if "/check-runs" in joined:
            return subprocess.CompletedProcess(cmd, 0, "", "")  # no check runs
        if "/status" in joined:
            out = json.dumps({"context": "ci/external", "state": "pending"})
            return subprocess.CompletedProcess(cmd, 0, out + "\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(github_api, "_run", fake_run)
    checks = github_api.get_check_runs_for_commit("abc", cwd=None)
    assert checks == [{"name": "ci/external", "status": "in_progress", "conclusion": None}]


def test_commit_status_failure_maps_to_completed_failure(monkeypatch):
    def fake_run(cmd, timeout_s=20, cwd=None):
        import subprocess
        joined = " ".join(cmd)
        if "/check-runs" in joined:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "/status" in joined:
            out = json.dumps({"context": "ci/external", "state": "failure"})
            return subprocess.CompletedProcess(cmd, 0, out + "\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(github_api, "_run", fake_run)
    checks = github_api.get_check_runs_for_commit("abc", cwd=None)
    assert checks[0]["status"] == "completed"
    assert checks[0]["conclusion"] == "failure"
