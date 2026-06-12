"""Tests for the agent-agnostic maintenance loop (loop.py)."""

import os
import subprocess
import types

from agentic_pr_dash import loop


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _make_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("a\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def _fake_gh(bin_dir, body):
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text("#!/usr/bin/env bash\n" + body + "\n")
    gh.chmod(0o755)
    return gh


def test_baseline_prefers_remote_pr_head(tmp_path, monkeypatch):
    # The PR's published head — NOT local HEAD — is the baseline, so an executor
    # that adds local commits doesn't shrink the fix range to empty.
    local_head = _make_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    _fake_gh(bin_dir, 'echo "remote-pr-head-sha"')
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    assert loop._baseline_sha(str(tmp_path), 7) == "remote-pr-head-sha"
    assert local_head != "remote-pr-head-sha"


def test_baseline_falls_back_to_local_head_when_gh_fails(tmp_path, monkeypatch):
    local_head = _make_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    _fake_gh(bin_dir, "exit 1")  # gh unavailable / no PR
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    assert loop._baseline_sha(str(tmp_path), None) == local_head


def test_baseline_falls_back_when_gh_missing(tmp_path, monkeypatch):
    # A PATH with NO gh at all -> subprocess raises OSError -> must fall back to
    # the local HEAD, never propagate the error or hang the loop.
    local_head = _make_repo(tmp_path)
    only_git = tmp_path / "only-git"
    only_git.mkdir()
    real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    (only_git / "git").symlink_to(real_git)
    monkeypatch.setenv("PATH", str(only_git))
    assert loop._baseline_sha(str(tmp_path), 7) == local_head


def test_parse_pr_number_reads_trailer():
    out = "some prompt text\nPR_NUMBER=123\nmore\n"
    assert loop._parse_pr_number(out) == 123
    assert loop._parse_pr_number("no trailer here") is None


def test_parse_coordinator_claim_id_reads_trailer():
    out = "some prompt text\nCOORDINATOR_CLAIM_ID=abc123\nmore\n"
    assert loop._parse_coordinator_claim_id(out) == "abc123"
    assert loop._parse_coordinator_claim_id("no trailer here") is None


def _args(**kw):
    base = dict(no_discover_worktrees=False, session_id="sess", cwd=["/fallback"])
    base.update(kw)
    return types.SimpleNamespace(**base)


def _stub_list_owned(monkeypatch, *, stdout, returncode):
    def fake_run(cmd, *a, **k):
        return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)
    monkeypatch.setattr(loop.subprocess, "run", fake_run)


def test_discover_empty_owned_set_does_not_fall_back_to_cwd(monkeypatch):
    # rc 0 + no paths = "owns nothing this tick" — authoritative. Falling back to
    # cwd here would route the loop to service a foreign worktree (BOU-1540 #5).
    _stub_list_owned(monkeypatch, stdout="", returncode=0)
    assert loop._discover_cwds(_args()) == []


def test_discover_falls_back_to_cwd_on_command_failure(monkeypatch):
    # Non-zero rc = genuine discovery failure → fall back to the explicit --cwd.
    _stub_list_owned(monkeypatch, stdout="", returncode=1)
    assert loop._discover_cwds(_args()) == ["/fallback"]


def test_discover_returns_owned_paths(monkeypatch):
    _stub_list_owned(monkeypatch, stdout="/wt/a\n/wt/b\n", returncode=0)
    assert loop._discover_cwds(_args()) == ["/wt/a", "/wt/b"]


def test_discover_passes_cwd_to_list_owned(monkeypatch):
    # list-owned must run `git worktree list` in the configured --cwd, not the
    # loop process's cwd, or rc-0-empty could mean "wrong dir" (BOU-1540 #Z).
    captured = {}

    def fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    loop._discover_cwds(_args(cwd=["/repo/root"]))
    assert "--cwd" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--cwd") + 1] == "/repo/root"


def test_discover_iterates_all_configured_cwds(monkeypatch):
    # Repeatable --cwd: list-owned runs once per configured repo and the owned
    # paths are merged; a repo whose discovery fails falls back to its own root
    # (BOU-1540 #6).
    def fake_run(cmd, *a, **k):
        cwd = cmd[cmd.index("--cwd") + 1]
        if cwd == "/repo/a":
            return types.SimpleNamespace(stdout="/repo/a/wt1\n", stderr="", returncode=0)
        if cwd == "/repo/b":
            return types.SimpleNamespace(stdout="", stderr="", returncode=1)  # failed
        if cwd == "/repo/c":
            return types.SimpleNamespace(stdout="/repo/c/wt2\n/repo/a/wt1\n", stderr="", returncode=0)
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    result = loop._discover_cwds(_args(cwd=["/repo/a", "/repo/b", "/repo/c"]))
    # a's owned path, b's fallback-to-root (failed), c's owned path; deduped.
    assert result == ["/repo/a/wt1", "/repo/b", "/repo/c/wt2"]


def test_tick_heartbeats_and_releases_coordinator_claim(monkeypatch, tmp_path):
    calls = []
    worktree = tmp_path / "wt"
    worktree.mkdir()

    def fake_discover(args):
        return [str(worktree)]

    def fake_run(cmd, *a, **k):
        if cmd[:3] == [loop.sys.executable, "-m", "agentic_pr_dash"] and "check" in cmd:
            return types.SimpleNamespace(
                returncode=loop.CHECK_WORK_FOUND,
                stdout="fix prompt\nPR_NUMBER=7\nCOORDINATOR_CLAIM_ID=claim-1\n",
                stderr="",
            )
        if cmd[:3] == [loop.sys.executable, "-m", "agentic_pr_dash"] and "complete" in cmd:
            calls.append(("complete", cmd))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    monkeypatch.setattr(loop, "_discover_cwds", fake_discover)
    monkeypatch.setattr(loop, "_baseline_sha", lambda cwd, pr: "base-sha")
    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    monkeypatch.setattr(loop, "_run_executor", lambda executor, prompt, cwd: calls.append(("executor", cwd)) or 0)
    monkeypatch.setattr(loop.coordinator, "heartbeat_claim_id", lambda claim_id, session_id: calls.append(("heartbeat", claim_id, session_id)))
    monkeypatch.setattr(loop.coordinator, "release_claim_id", lambda claim_id, session_id, reason: calls.append(("release", claim_id, session_id, reason)))

    loop._tick(_args(cwd=["/repo/root"], session_id="sess-1"), "codex {prompt}")

    assert ("heartbeat", "claim-1", "sess-1") in calls
    assert ("executor", str(worktree)) in calls
    assert ("release", "claim-1", "sess-1", "completed") in calls


def test_tick_releases_claim_when_executor_launch_raises(monkeypatch, tmp_path):
    """If the executor cannot be spawned (e.g. binary missing → OSError), the
    coordinator claim must be released so the PR is not left wrongly owned until
    the lease expires."""
    calls = []
    worktree = tmp_path / "wt"
    worktree.mkdir()

    def fake_run(cmd, *a, **k):
        if cmd[:3] == [loop.sys.executable, "-m", "agentic_pr_dash"] and "check" in cmd:
            return types.SimpleNamespace(
                returncode=loop.CHECK_WORK_FOUND,
                stdout="fix prompt\nPR_NUMBER=7\nCOORDINATOR_CLAIM_ID=claim-1\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    def boom(executor, prompt, cwd):
        raise FileNotFoundError("codex: command not found")

    monkeypatch.setattr(loop, "_discover_cwds", lambda args: [str(worktree)])
    monkeypatch.setattr(loop, "_baseline_sha", lambda cwd, pr: "base-sha")
    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    monkeypatch.setattr(loop, "_run_executor", boom)
    monkeypatch.setattr(loop.coordinator, "heartbeat_claim_id", lambda claim_id, session_id: calls.append(("heartbeat", claim_id, session_id)))
    monkeypatch.setattr(loop.coordinator, "release_claim_id", lambda claim_id, session_id, reason: calls.append(("release", claim_id, session_id, reason)))

    loop._tick(_args(cwd=["/repo/root"], session_id="sess-1"), "codex {prompt}")

    assert ("heartbeat", "claim-1", "sess-1") in calls
    assert ("release", "claim-1", "sess-1", "executor_failed") in calls
