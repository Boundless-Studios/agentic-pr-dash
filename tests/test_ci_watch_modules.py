"""Focused unit tests for the _ci_watch subpackage (BOU-1690 WS3).

Each test exercises a moved function directly through its owning submodule,
verifying that the subpackage split preserved behavior.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentic_pr_dash._ci_watch import (
    adapter as _adapter,
    checks as _checks,
    config as _config,
    repo as _repo,
    results as _results,
    watcher as _watcher,
)
from agentic_pr_dash._ci_watch.config import CIWatchConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(tmp_path: Path, **kw) -> CIWatchConfig:
    return CIWatchConfig(
        project_dir=tmp_path,
        results_file=tmp_path / ".claude" / "ci-watch-latest.json",
        pid_file=tmp_path / ".claude" / "ci-watch.pid",
        **kw,
    )


# ---------------------------------------------------------------------------
# config.py — constants and CIWatchConfig.from_env
# ---------------------------------------------------------------------------

def test_config_constants_present():
    assert _config.POLL_INTERVAL_S == 20
    assert _config.INITIAL_DELAY_S == 15
    assert _config.PR_RETRY_S == 30
    assert _config.WATCH_TIMEOUT_S == 540
    assert _config.COMPLETED_STATUS == "completed"
    assert "success" in _config.PASSING_CONCLUSIONS
    assert _config.POLL_DONE == "done"
    assert _config.POLL_NO_CHECKS == "no_checks"
    assert _config.POLL_TIMEOUT == "timeout"
    assert _config.NO_CHECKS_GRACE_S == 90


def test_config_from_env_defaults(tmp_path):
    cfg = CIWatchConfig.from_env({"CI_WATCH_PROJECT_DIR": str(tmp_path)})
    assert cfg.project_dir == tmp_path
    assert cfg.results_file == tmp_path / ".claude" / "ci-watch-latest.json"
    assert cfg.pid_file == tmp_path / ".claude" / "ci-watch.pid"
    assert cfg.status_command is None
    assert cfg.complete_command is None
    assert cfg.poll_interval_s == _config.POLL_INTERVAL_S
    assert cfg.initial_delay_s == _config.INITIAL_DELAY_S
    assert cfg.watch_timeout_s == _config.WATCH_TIMEOUT_S
    assert cfg.pr_retry_s == _config.PR_RETRY_S


def test_config_from_env_overrides(tmp_path):
    cfg = CIWatchConfig.from_env({
        "CI_WATCH_PROJECT_DIR": str(tmp_path),
        "CI_WATCH_RESULTS_FILE": str(tmp_path / "r.json"),
        "CI_WATCH_PID_FILE": str(tmp_path / "w.pid"),
        "CI_WATCH_STATUS_COMMAND": "echo {status}",
        "CI_WATCH_COMPLETE_COMMAND": "echo done",
        "CI_WATCH_POLL_INTERVAL_S": "5",
        "CI_WATCH_INITIAL_DELAY_S": "0",
        "CI_WATCH_TIMEOUT_S": "120",
        "CI_WATCH_PR_RETRY_S": "10",
    })
    assert cfg.results_file == tmp_path / "r.json"
    assert cfg.pid_file == tmp_path / "w.pid"
    assert cfg.status_command == "echo {status}"
    assert cfg.complete_command == "echo done"
    assert cfg.poll_interval_s == 5
    assert cfg.initial_delay_s == 0
    assert cfg.watch_timeout_s == 120
    assert cfg.pr_retry_s == 10


def test_config_from_env_relative_paths_anchor_under_project(tmp_path):
    cfg = CIWatchConfig.from_env({
        "CI_WATCH_PROJECT_DIR": str(tmp_path),
        "CI_WATCH_RESULTS_FILE": ".claude/ci.json",
        "CI_WATCH_PID_FILE": ".claude/ci.pid",
    })
    assert cfg.results_file == tmp_path / ".claude" / "ci.json"
    assert cfg.pid_file == tmp_path / ".claude" / "ci.pid"


def test_config_from_env_invalid_int_falls_back_to_default(tmp_path):
    cfg = CIWatchConfig.from_env({
        "CI_WATCH_PROJECT_DIR": str(tmp_path),
        "CI_WATCH_POLL_INTERVAL_S": "not-a-number",
    })
    assert cfg.poll_interval_s == _config.POLL_INTERVAL_S


def test_eprint_writes_to_stderr(capsys):
    _config.eprint("hello world")
    captured = capsys.readouterr()
    assert captured.err == "hello world\n"
    assert captured.out == ""


# ---------------------------------------------------------------------------
# checks.py — _is_infra_check and snapshot_ci
# ---------------------------------------------------------------------------

def test_is_infra_check_matches_known_patterns():
    assert _checks._is_infra_check("tofu plan")
    assert _checks._is_infra_check("terraform apply")


def test_is_infra_check_does_not_match_code_checks():
    assert not _checks._is_infra_check("Unit Tests")
    assert not _checks._is_infra_check("Frontend Tests")
    assert not _checks._is_infra_check("Build")


def test_snapshot_ci_passing(monkeypatch):
    from agentic_pr_dash import github_api
    monkeypatch.setattr(github_api, "get_check_runs_for_commit",
                        lambda sha, cwd=None: [{"name": "Unit", "status": "completed", "conclusion": "success"}])
    snap = _checks.snapshot_ci("abc", Path("/tmp"))
    assert snap["status"] == "passing"
    assert snap["infra_failures"] == []


def test_snapshot_ci_code_failure(monkeypatch):
    from agentic_pr_dash import github_api
    monkeypatch.setattr(github_api, "get_check_runs_for_commit",
                        lambda sha, cwd=None: [{"name": "Unit", "status": "completed", "conclusion": "failure"}])
    snap = _checks.snapshot_ci("abc", Path("/tmp"))
    assert snap["status"] == "failing"
    assert snap["code_failures"] == ["Unit"]


def test_snapshot_ci_infra_failure_not_code(monkeypatch):
    from agentic_pr_dash import github_api
    monkeypatch.setattr(github_api, "get_check_runs_for_commit",
                        lambda sha, cwd=None: [
                            {"name": "tofu plan", "status": "completed", "conclusion": "failure"},
                            {"name": "Unit", "status": "completed", "conclusion": "success"},
                        ])
    snap = _checks.snapshot_ci("abc", Path("/tmp"))
    assert snap["status"] == "passing"
    assert "tofu plan" in snap["infra_failures"]


def test_snapshot_ci_no_checks(monkeypatch):
    from agentic_pr_dash import github_api
    monkeypatch.setattr(github_api, "get_check_runs_for_commit", lambda sha, cwd=None: [])
    snap = _checks.snapshot_ci("abc", Path("/tmp"))
    assert snap["status"] == "no_checks"


def test_snapshot_ci_pending(monkeypatch):
    from agentic_pr_dash import github_api
    monkeypatch.setattr(github_api, "get_check_runs_for_commit",
                        lambda sha, cwd=None: [{"name": "Build", "status": "in_progress", "conclusion": None}])
    snap = _checks.snapshot_ci("abc", Path("/tmp"))
    assert snap["status"] == "pending"


# ---------------------------------------------------------------------------
# adapter.py — _render and run_adapter
# ---------------------------------------------------------------------------

def test_render_substitutes_all_fields():
    result = _adapter._render("status={status} msg={message}", {"status": "ok", "message": "all good"})
    assert result == "status=ok msg=all good"


def test_render_unknown_placeholder_left_as_is():
    result = _adapter._render("{status} {unknown}", {"status": "pass"})
    assert result == "pass {unknown}"


def test_run_adapter_none_is_noop(tmp_path):
    # Must not raise
    _adapter.run_adapter(None, {"status": "x"}, tmp_path)


def test_run_adapter_renders_and_runs(tmp_path):
    out_file = tmp_path / "out.txt"
    _adapter.run_adapter(
        f"echo {{status}}:{{message}} > {out_file}",
        {"status": "passing", "message": "great"},
        tmp_path,
    )
    assert out_file.read_text().strip() == "passing:great"


def test_run_adapter_swallows_nonzero_exit(tmp_path):
    # A failing command must not raise — adapters are advisory.
    _adapter.run_adapter("exit 7", {"status": "x"}, tmp_path)


# ---------------------------------------------------------------------------
# results.py — write_results round-trip
# ---------------------------------------------------------------------------

def test_write_results_creates_parent_and_writes_json(tmp_path):
    cfg = _cfg(tmp_path)
    _results.write_results(cfg, {"status": "passing", "action_needed": False})
    data = json.loads(cfg.results_file.read_text())
    assert data["status"] == "passing"
    assert data["action_needed"] is False


def test_write_results_overwrites_previous(tmp_path):
    cfg = _cfg(tmp_path)
    _results.write_results(cfg, {"status": "watching"})
    _results.write_results(cfg, {"status": "passing"})
    data = json.loads(cfg.results_file.read_text())
    assert data["status"] == "passing"


def test_write_results_trailing_newline(tmp_path):
    cfg = _cfg(tmp_path)
    _results.write_results(cfg, {"x": 1})
    assert cfg.results_file.read_text().endswith("\n")


# ---------------------------------------------------------------------------
# watcher.py — _is_our_watcher and kill_previous_watcher
# ---------------------------------------------------------------------------

def test_is_our_watcher_false_for_dead_pid():
    # PID 999999999 almost certainly doesn't exist; ps returns non-zero.
    assert _watcher._is_our_watcher(999999999) is False


def test_kill_previous_watcher_no_pidfile_is_noop(tmp_path):
    # Must not raise when pid file is absent.
    _watcher.kill_previous_watcher(_cfg(tmp_path))


def test_kill_previous_watcher_clears_stale_pidfile(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.pid_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.pid_file.write_text("999999999")
    _watcher.kill_previous_watcher(cfg)
    assert not cfg.pid_file.exists()


def test_kill_previous_watcher_spares_foreign_pid(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.pid_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.pid_file.write_text("4242")
    monkeypatch.setattr(_watcher, "_is_our_watcher", lambda pid: False)
    killed: list[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    _watcher.kill_previous_watcher(cfg)
    assert killed == []
    assert not cfg.pid_file.exists()


def test_kill_previous_watcher_kills_confirmed_watcher(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.pid_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.pid_file.write_text("4242")
    monkeypatch.setattr(_watcher, "_is_our_watcher", lambda pid: True)
    killed: list[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    _watcher.kill_previous_watcher(cfg)
    assert killed == [4242]
    assert not cfg.pid_file.exists()
