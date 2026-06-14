"""Tests for the post-push CI-watch hook + generic primitive (BOU-1670)."""

import io
import json
import sys
from pathlib import Path

from agentic_pr_dash import ci_watch
from agentic_pr_dash.codex_hooks import run_post_push_watch as hook


# ---------------------------------------------------------------------------
# Hook: git-push detection across runtimes / compound commands
# ---------------------------------------------------------------------------

def _arm_via_hook(monkeypatch, payload, *, argv=("PostToolUse",)):
    """Run hook.main with arming stubbed; return the CIWatchConfig it armed (or None)."""
    armed: list[ci_watch.CIWatchConfig] = []

    def fake_arm(cfg):
        armed.append(cfg)
        return 0

    monkeypatch.setattr(ci_watch, "arm_post_push_watch", fake_arm)
    monkeypatch.setattr(sys, "argv", ["run_post_push_watch.py", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert hook.main() == 0
    return armed[0] if armed else None


def test_codex_exec_git_push_arms(monkeypatch, tmp_path):
    payload = {"tool_name": "exec_command", "tool_input": {"cmd": "git push"}, "cwd": str(tmp_path)}
    cfg = _arm_via_hook(monkeypatch, payload)
    assert cfg is not None
    assert cfg.project_dir == tmp_path


def test_claude_bash_git_push_arms(monkeypatch, tmp_path):
    payload = {"tool_name": "Bash", "tool_input": {"command": "git push origin HEAD"}, "cwd": str(tmp_path)}
    cfg = _arm_via_hook(monkeypatch, payload)
    assert cfg is not None
    assert cfg.project_dir == tmp_path


def test_non_push_command_is_noop(monkeypatch, tmp_path):
    payload = {"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": str(tmp_path)}
    assert _arm_via_hook(monkeypatch, payload) is None


def test_cd_relocates_push_cwd(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    payload = {"tool_name": "exec_command",
               "tool_input": {"cmd": "cd wt && git push"}, "cwd": str(tmp_path)}
    cfg = _arm_via_hook(monkeypatch, payload)
    assert cfg is not None
    assert cfg.project_dir == worktree.resolve()


def test_git_dash_c_resolves_effective_cwd(monkeypatch, tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    payload = {"tool_name": "exec_command",
               "tool_input": {"cmd": "git -C nested push"}, "cwd": str(tmp_path)}
    cfg = _arm_via_hook(monkeypatch, payload)
    assert cfg is not None
    assert cfg.project_dir == nested


def test_or_guarded_push_is_not_armed(monkeypatch, tmp_path):
    payload = {"tool_name": "exec_command",
               "tool_input": {"cmd": "make build || git push"}, "cwd": str(tmp_path)}
    assert _arm_via_hook(monkeypatch, payload) is None


def test_and_guarded_push_skipped_when_prior_failed(monkeypatch, tmp_path):
    payload = {"tool_name": "exec_command",
               "tool_input": {"cmd": "make ok && git push"}, "cwd": str(tmp_path),
               "tool_response": {"exit_code": 1}}
    assert _arm_via_hook(monkeypatch, payload) is None


def test_non_posttooluse_phase_is_noop(monkeypatch, tmp_path):
    payload = {"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": str(tmp_path)}
    assert _arm_via_hook(monkeypatch, payload, argv=("SessionStart",)) is None


def test_malformed_payload_is_best_effort(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_post_push_watch.py", "PostToolUse"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("{"))
    assert hook.main() == 0


# ---------------------------------------------------------------------------
# CIWatchConfig: env-driven results-file / pid / adapter wiring
# ---------------------------------------------------------------------------

def test_config_defaults_to_dot_claude_paths(tmp_path):
    cfg = ci_watch.CIWatchConfig.from_env({"CI_WATCH_PROJECT_DIR": str(tmp_path)})
    assert cfg.results_file == tmp_path / ".claude" / "ci-watch-latest.json"
    assert cfg.pid_file == tmp_path / ".claude" / "ci-watch.pid"
    assert cfg.status_command is None
    assert cfg.complete_command is None


def test_config_overrides_from_env(tmp_path):
    cfg = ci_watch.CIWatchConfig.from_env({
        "CI_WATCH_PROJECT_DIR": str(tmp_path),
        "CI_WATCH_RESULTS_FILE": str(tmp_path / "results.json"),
        "CI_WATCH_PID_FILE": str(tmp_path / "watch.pid"),
        "CI_WATCH_STATUS_COMMAND": "echo {status}",
        "CI_WATCH_COMPLETE_COMMAND": "echo done {message}",
    })
    assert cfg.results_file == tmp_path / "results.json"
    assert cfg.pid_file == tmp_path / "watch.pid"
    assert cfg.status_command == "echo {status}"
    assert cfg.complete_command == "echo done {message}"


# ---------------------------------------------------------------------------
# CI-watch primitive: snapshot / results / adapter / pid management
# ---------------------------------------------------------------------------

def _cfg(tmp_path, **kw):
    return ci_watch.CIWatchConfig(
        project_dir=tmp_path,
        results_file=tmp_path / ".claude" / "ci-watch-latest.json",
        pid_file=tmp_path / ".claude" / "ci-watch.pid",
        **kw,
    )


def test_snapshot_passing(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_watch.github_api, "get_check_runs_for_commit",
                        lambda sha, cwd=None: [{"name": "Unit", "status": "completed", "conclusion": "success"}])
    snap = ci_watch.snapshot_ci("abc", tmp_path)
    assert snap["status"] == "passing"


def test_snapshot_code_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_watch.github_api, "get_check_runs_for_commit",
                        lambda sha, cwd=None: [{"name": "Unit", "status": "completed", "conclusion": "failure"}])
    snap = ci_watch.snapshot_ci("abc", tmp_path)
    assert snap["status"] == "failing"
    assert snap["code_failures"] == ["Unit"]


def test_snapshot_infra_failure_is_not_code_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_watch.github_api, "get_check_runs_for_commit",
                        lambda sha, cwd=None: [
                            {"name": "tofu plan", "status": "completed", "conclusion": "failure"},
                            {"name": "Unit", "status": "completed", "conclusion": "success"},
                        ])
    snap = ci_watch.snapshot_ci("abc", tmp_path)
    assert snap["status"] == "passing"
    assert "tofu plan" in snap["infra_failures"]


def test_snapshot_no_checks(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_watch.github_api, "get_check_runs_for_commit", lambda sha, cwd=None: [])
    assert ci_watch.snapshot_ci("abc", tmp_path)["status"] == "no_checks"


def test_write_results_creates_parent(tmp_path):
    cfg = _cfg(tmp_path)
    ci_watch.write_results(cfg, {"status": "watching"})
    assert json.loads(cfg.results_file.read_text())["status"] == "watching"


def test_run_adapter_renders_and_runs(tmp_path):
    cfg = _cfg(tmp_path, status_command=f"echo {{status}}:{{message}} > {tmp_path / 'out.txt'}")
    ci_watch.run_adapter(cfg.status_command, {"status": "passing", "message": "all good"}, tmp_path)
    assert (tmp_path / "out.txt").read_text().strip() == "passing:all good"


def test_run_adapter_none_is_noop(tmp_path):
    ci_watch.run_adapter(None, {"status": "x"}, tmp_path)  # must not raise


def test_run_adapter_swallows_failures(tmp_path):
    # A failing adapter command must never raise — advisory mirror.
    ci_watch.run_adapter("exit 7", {"status": "x"}, tmp_path)


def test_kill_previous_watcher_removes_stale_pidfile(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.pid_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.pid_file.write_text("999999999")  # almost-certainly-dead pid
    ci_watch.kill_previous_watcher(cfg)
    assert not cfg.pid_file.exists()


def test_kill_previous_watcher_no_pidfile_is_noop(tmp_path):
    ci_watch.kill_previous_watcher(_cfg(tmp_path))  # must not raise


# ---------------------------------------------------------------------------
# background_watch end-to-end (network stubbed)
# ---------------------------------------------------------------------------

def test_background_watch_passing_writes_results_and_calls_complete(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_watch.github_api, "get_check_runs_for_commit",
                        lambda sha, cwd=None: [{"name": "Unit", "status": "completed", "conclusion": "success"}])
    monkeypatch.setattr(ci_watch.github_api, "get_latest_commit", lambda pr, cwd=None: ("sha", "2026-01-01"))
    monkeypatch.setattr(ci_watch.github_api, "get_unaddressed_comments", lambda pr, d, cwd=None: [])
    monkeypatch.setattr(ci_watch.github_api, "get_repo_info", lambda cwd=None: ("o", "r"))

    complete_calls: list[dict] = []
    monkeypatch.setattr(ci_watch, "run_adapter",
                        lambda tmpl, fields, cwd: complete_calls.append(fields) if tmpl == "C" else None)

    cfg = _cfg(tmp_path, initial_delay_s=0, poll_interval_s=0, complete_command="C", status_command="S")
    ci_watch.background_watch("deadbeef", 42, "feature-x", cfg)

    results = json.loads(cfg.results_file.read_text())
    assert results["status"] == "passing"
    assert results["action_needed"] is False
    assert any(c["status"] == "passing" for c in complete_calls)


def test_background_watch_failing_includes_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_watch.github_api, "get_check_runs_for_commit",
                        lambda sha, cwd=None: [{"name": "Unit", "status": "completed", "conclusion": "failure"}])
    monkeypatch.setattr(ci_watch.github_api, "get_failed_logs",
                        lambda sha, names, cwd=None: {"Unit": "boom"})
    monkeypatch.setattr(ci_watch.github_api, "get_latest_commit", lambda pr, cwd=None: ("sha", ""))
    monkeypatch.setattr(ci_watch.github_api, "get_unaddressed_comments", lambda pr, d, cwd=None: [])
    monkeypatch.setattr(ci_watch.github_api, "get_repo_info", lambda cwd=None: ("o", "r"))

    cfg = _cfg(tmp_path, initial_delay_s=0, poll_interval_s=0)
    ci_watch.background_watch("deadbeef", 7, "feature-y", cfg)

    results = json.loads(cfg.results_file.read_text())
    assert results["status"] == "failing"
    assert results["action_needed"] is True
    assert results["code_failures"] == ["Unit"]
    assert results["failed_logs"] == {"Unit": "boom"}


def test_background_watch_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_watch.github_api, "get_check_runs_for_commit",
                        lambda sha, cwd=None: [{"name": "Unit", "status": "in_progress", "conclusion": None}])
    monkeypatch.setattr(ci_watch.github_api, "get_repo_info", lambda cwd=None: ("o", "r"))

    cfg = _cfg(tmp_path, initial_delay_s=0, poll_interval_s=0, watch_timeout_s=0)
    ci_watch.background_watch("deadbeef", 9, "feature-z", cfg)

    results = json.loads(cfg.results_file.read_text())
    assert results["status"] == "timeout"
    assert results["action_needed"] is True


# ---------------------------------------------------------------------------
# arm_post_push_watch foreground: skip rules + spawn
# ---------------------------------------------------------------------------

def test_arm_skips_on_main_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_watch, "current_branch", lambda d: "main")
    spawned: list = []
    monkeypatch.setattr(ci_watch, "spawn_background_watcher", lambda *a: spawned.append(a))
    assert ci_watch.arm_post_push_watch(_cfg(tmp_path)) == 0
    assert spawned == []


def test_arm_skips_when_no_pr(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_watch, "current_branch", lambda d: "feature-x")
    monkeypatch.setattr(ci_watch, "head_sha", lambda d: "abc123")
    monkeypatch.setattr(ci_watch, "get_pr_number", lambda b, d: None)
    spawned: list = []
    monkeypatch.setattr(ci_watch, "spawn_background_watcher", lambda *a: spawned.append(a))
    cfg = _cfg(tmp_path, pr_retry_s=0)
    assert ci_watch.arm_post_push_watch(cfg) == 0
    assert spawned == []


def test_arm_spawns_when_pr_present(monkeypatch, tmp_path):
    monkeypatch.setattr(ci_watch, "current_branch", lambda d: "feature-x")
    monkeypatch.setattr(ci_watch, "head_sha", lambda d: "abc123")
    monkeypatch.setattr(ci_watch, "get_pr_number", lambda b, d: 55)
    monkeypatch.setattr(ci_watch, "snapshot_ci", lambda sha, d: {"status": "pending", "message": "1 pending"})
    monkeypatch.setattr(ci_watch.github_api, "get_repo_info", lambda cwd=None: ("o", "r"))
    spawned: list = []
    monkeypatch.setattr(ci_watch, "spawn_background_watcher", lambda *a: spawned.append(a))
    assert ci_watch.arm_post_push_watch(_cfg(tmp_path)) == 0
    assert len(spawned) == 1
    assert spawned[0][:3] == ("abc123", 55, "feature-x")
