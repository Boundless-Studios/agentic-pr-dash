"""Tests for the agent-agnostic maintenance loop (loop.py)."""

import os
import subprocess

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
