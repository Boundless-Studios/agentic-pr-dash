"""Tests for the agent-agnostic maintenance loop (loop.py)."""

import os
import subprocess
import types

import pytest

from agent_coordinator.service import StaleClaimError

from agentic_pr_dash import loop
from agentic_pr_dash import worktrees


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def test_loop_pidfile_write_and_remove(tmp_path):
    pidfile = tmp_path / "daemons" / "pr-maintenance-loop.pid"

    loop._write_loop_pidfile(pidfile)
    assert pidfile.read_text(encoding="utf-8").strip() == str(os.getpid())

    # Only remove a pidfile that still holds our pid.
    pidfile.write_text("999999", encoding="utf-8")
    loop._remove_loop_pidfile(pidfile)
    assert pidfile.exists()  # not ours → left alone

    pidfile.write_text(str(os.getpid()), encoding="utf-8")
    loop._remove_loop_pidfile(pidfile)
    assert not pidfile.exists()


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


def test_parse_coordinator_claim_handle_reads_fenced_trailer():
    out = (
        "some prompt text\n"
        "COORDINATOR_CLAIM_ID=abc123\n"
        "COORDINATOR_LEASE_EPOCH=7\n"
        "more\n"
    )
    assert loop._parse_coordinator_claim_handle(out) == loop.coordinator.ClaimHandle(
        claim_id="abc123",
        lease_epoch=7,
    )
    assert loop._parse_coordinator_claim_handle("no trailer here") is None
    assert loop._parse_coordinator_claim_handle(
        "COORDINATOR_CLAIM_ID=abc123\n"
    ) is None


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


def test_discover_machine_wide_spans_all_maintenance_roots(monkeypatch):
    # BOU-1546: a machine-wide loop (no --session-id) must enumerate worktrees
    # across [anchor] + maintenance_repo_roots, not just args.cwd[0].
    from agentic_pr_dash import maintenance_check as mc
    monkeypatch.setattr(mc, "_resolve_maintenance_roots",
                        lambda c: ["/repoA", "/repoB"])

    def fake_run(cmd, *a, **k):
        cwd = k.get("cwd")
        if cwd == "/repoA":
            return types.SimpleNamespace(
                stdout="worktree /repoA/wt1\nworktree /repoA/wt2\n",
                stderr="", returncode=0)
        if cwd == "/repoB":
            return types.SimpleNamespace(
                stdout="worktree /repoB/wt1\n", stderr="", returncode=0)
        return types.SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    got = loop._discover_cwds(_args(session_id="", cwd=["/repoA"]))
    assert got == ["/repoA/wt1", "/repoA/wt2", "/repoB/wt1"]


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


def test_tick_cleans_stale_no_pr_worktree_without_running_pr_check(monkeypatch, tmp_path):
    stale_worktree = tmp_path / "stale-no-pr"
    active_worktree = tmp_path / "active-pr"
    stale_worktree.mkdir()
    active_worktree.mkdir()
    calls = []

    monkeypatch.setattr(loop, "_discover_cwds", lambda args: [str(stale_worktree), str(active_worktree)])
    # Repo-scoped streak path: stub the gh-backed slug. The clean check now runs
    # _clear_recovered_streak, which no-ops here (no recorded state).
    monkeypatch.setattr(loop, "_repo_slug", lambda cwd: "testrepo")
    monkeypatch.setattr(
        loop,
        "_cleanup_stale_no_pr_worktree",
        lambda cwd, session_id="": calls.append(("cleanup", cwd)) or cwd == str(stale_worktree),
    )

    def fake_run(cmd, *a, **k):
        if cmd[:3] == [loop.sys.executable, "-m", "agentic_pr_dash"] and "check" in cmd:
            calls.append(("check", cmd[cmd.index("--cwd") + 1]))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    monkeypatch.setattr(loop.subprocess, "run", fake_run)

    loop._tick(_args(cwd=["/repo/root"], session_id=""), "codex {prompt}")

    assert ("cleanup", str(stale_worktree)) in calls
    assert ("cleanup", str(active_worktree)) in calls
    assert ("check", str(stale_worktree)) not in calls
    assert ("check", str(active_worktree)) in calls


def test_cleanup_reason_fails_closed_when_pr_lookup_errors(monkeypatch, tmp_path):
    worktree = tmp_path / "stale-with-hidden-pr"
    worktree.mkdir()

    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["git", "-C"] and cmd[2] == str(worktree) and "status" in cmd:
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[0] == "gh":
            return types.SimpleNamespace(returncode=1, stdout="", stderr="auth expired")
        if cmd[:2] == ["git", "-C"] and "merge-base" in cmd:
            return types.SimpleNamespace(returncode=0, stdout="base-sha\n", stderr="")
        if cmd[:2] == ["git", "-C"] and "rev-list" in cmd:
            return types.SimpleNamespace(returncode=0, stdout="1\n", stderr="")
        if cmd[:2] == ["git", "-C"] and "log" in cmd:
            old = worktrees._now_epoch() - (worktrees.OTHER_STALE_SECS + 60)
            return types.SimpleNamespace(returncode=0, stdout=f"{old}\n", stderr="")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    monkeypatch.setattr(worktrees.subprocess, "run", fake_run)

    eligible, reason = worktrees.selected_worktree_cleanup_reason(
        {"path": str(worktree), "branch": "feature/hidden-pr"},
        [],
        main_repo=str(tmp_path),
    )

    assert eligible is False
    assert "PR lookup" in reason


def test_zero_commit_unknown_birth_time_is_not_stale(monkeypatch, tmp_path):
    worktree = tmp_path / "fresh-zero-commit"
    worktree.mkdir()

    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["git", "-C"] and cmd[2] == str(worktree) and "status" in cmd:
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[0] == "gh":
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "-C"] and "merge-base" in cmd:
            return types.SimpleNamespace(returncode=0, stdout="base-sha\n", stderr="")
        if cmd[:2] == ["git", "-C"] and "rev-list" in cmd:
            return types.SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        if cmd[0] == "stat":
            return types.SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    monkeypatch.setattr(worktrees.subprocess, "run", fake_run)

    eligible, reason = worktrees.selected_worktree_cleanup_reason(
        {"path": str(worktree), "branch": "feature/fresh-zero"},
        [],
        main_repo=str(tmp_path),
    )

    assert eligible is False
    assert "not stale" in reason


def test_cleanup_skips_live_idle_feature_pipeline_owner(monkeypatch, tmp_path):
    worktree = tmp_path / "idle-owned"
    worktree.mkdir()
    removed = []

    monkeypatch.setattr(loop, "find_worktree_for_path", lambda cwd: {"path": cwd, "branch": "feature/idle-owned"})
    monkeypatch.setattr(loop, "discover_active_agents", lambda paths: {})
    monkeypatch.setattr(loop, "selected_worktree_cleanup_reason", lambda worktree, active_agents: (True, "stale orphan"))
    monkeypatch.setattr(loop, "_live_independent_owner_paths", lambda paths, session_id: {str(worktree)}, raising=False)
    monkeypatch.setattr(loop, "remove_worktree", lambda cwd: removed.append(cwd) or (True, ""))

    assert loop._cleanup_stale_no_pr_worktree(str(worktree), session_id="loop-session") is False
    assert removed == []


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
                stdout=(
                    "fix prompt\nPR_NUMBER=7\n"
                    "COORDINATOR_CLAIM_ID=claim-1\n"
                    "COORDINATOR_LEASE_EPOCH=4\n"
                ),
                stderr="",
            )
        if cmd[:3] == [loop.sys.executable, "-m", "agentic_pr_dash"] and "complete" in cmd:
            calls.append(("complete", cmd))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    monkeypatch.setattr(loop, "_discover_cwds", fake_discover)
    monkeypatch.setattr(loop, "_cleanup_stale_no_pr_worktree", lambda cwd, session_id="": False)
    monkeypatch.setattr(loop, "_baseline_sha", lambda cwd, pr: "base-sha")
    monkeypatch.setattr(loop, "_repo_slug", lambda cwd: "testrepo")
    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    monkeypatch.setattr(
        loop, "_run_executor",
        lambda executor, prompt, cwd: (calls.append(("executor", cwd)), (0, ""))[1])
    monkeypatch.setattr(
        loop.coordinator,
        "heartbeat_claim",
        lambda handle, session_id: calls.append(
            ("heartbeat", handle.claim_id, handle.lease_epoch, session_id)
        ),
    )
    monkeypatch.setattr(
        loop.coordinator,
        "release_claim",
        lambda handle, session_id, reason: calls.append(
            ("release", handle.claim_id, handle.lease_epoch, session_id, reason)
        ),
    )

    loop._tick(_args(cwd=["/repo/root"], session_id="sess-1"), "codex {prompt}")

    assert ("heartbeat", "claim-1", 4, "sess-1") in calls
    assert ("executor", str(worktree)) in calls
    assert ("release", "claim-1", 4, "sess-1", "completed") in calls


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
                stdout=(
                    "fix prompt\nPR_NUMBER=7\n"
                    "COORDINATOR_CLAIM_ID=claim-1\n"
                    "COORDINATOR_LEASE_EPOCH=4\n"
                ),
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    def boom(executor, prompt, cwd):
        raise FileNotFoundError("codex: command not found")

    monkeypatch.setattr(loop, "_discover_cwds", lambda args: [str(worktree)])
    monkeypatch.setattr(loop, "_cleanup_stale_no_pr_worktree", lambda cwd, session_id="": False)
    monkeypatch.setattr(loop, "_baseline_sha", lambda cwd, pr: "base-sha")
    monkeypatch.setattr(loop, "_repo_slug", lambda cwd: "testrepo")
    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    monkeypatch.setattr(loop, "_run_executor", boom)
    monkeypatch.setattr(
        loop.coordinator,
        "heartbeat_claim",
        lambda handle, session_id: calls.append(
            ("heartbeat", handle.claim_id, handle.lease_epoch, session_id)
        ),
    )
    monkeypatch.setattr(
        loop.coordinator,
        "release_claim",
        lambda handle, session_id, reason: calls.append(
            ("release", handle.claim_id, handle.lease_epoch, session_id, reason)
        ),
    )

    loop._tick(_args(cwd=["/repo/root"], session_id="sess-1"), "codex {prompt}")

    assert ("heartbeat", "claim-1", 4, "sess-1") in calls
    assert ("release", "claim-1", 4, "sess-1", "executor_failed") in calls


@pytest.mark.parametrize("stale_operation", ["heartbeat", "release"])
def test_tick_treats_stale_claim_as_handoff_and_continues(
    monkeypatch, tmp_path, stale_operation,
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    calls = []

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == [loop.sys.executable, "-m", "agentic_pr_dash"] and "check" in cmd:
            cwd = cmd[cmd.index("--cwd") + 1]
            calls.append(("check", cwd))
            if cwd == str(first):
                return types.SimpleNamespace(
                    returncode=loop.CHECK_WORK_FOUND,
                    stdout=(
                        "fix prompt\nPR_NUMBER=7\n"
                        "COORDINATOR_CLAIM_ID=claim-1\n"
                        "COORDINATOR_LEASE_EPOCH=4\n"
                    ),
                    stderr="",
                )
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == [loop.sys.executable, "-m", "agentic_pr_dash"] and "complete" in cmd:
            calls.append(("complete", cmd))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    def heartbeat_claim(handle, session_id):
        calls.append(("heartbeat", handle.claim_id, handle.lease_epoch, session_id))
        if stale_operation == "heartbeat":
            raise StaleClaimError(expected_epoch=5, received_epoch=4)

    def release_claim(handle, session_id, reason):
        calls.append(("release", handle.claim_id, handle.lease_epoch, session_id, reason))
        if stale_operation == "release":
            raise StaleClaimError(expected_epoch=5, received_epoch=4)

    monkeypatch.setattr(loop, "_discover_cwds", lambda args: [str(first), str(second)])
    monkeypatch.setattr(loop, "_cleanup_stale_no_pr_worktree", lambda cwd, session_id="": False)
    monkeypatch.setattr(loop, "_baseline_sha", lambda cwd, pr: "base-sha")
    monkeypatch.setattr(loop, "_repo_slug", lambda cwd: "testrepo")
    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    monkeypatch.setattr(
        loop,
        "_run_executor",
        lambda executor, prompt, cwd: (calls.append(("executor", cwd)), (0, ""))[1],
    )
    monkeypatch.setattr(loop.coordinator, "heartbeat_claim", heartbeat_claim)
    monkeypatch.setattr(loop.coordinator, "release_claim", release_claim)

    loop._tick(_args(cwd=["/repo/root"], session_id="sess-1"), "codex {prompt}")

    assert ("check", str(second)) in calls
    if stale_operation == "heartbeat":
        assert ("executor", str(first)) not in calls
    else:
        assert ("executor", str(first)) in calls
