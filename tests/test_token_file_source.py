"""BOU-1991: gh calls source the automation token from the rotating token
file per invocation, so long-lived processes (dashboard server, maintenance
loop) never hold a stale spawn-time ``GH_TOKEN`` and their supervisor no
longer needs to restart them on every 45-minute rotation (the restart was
user-visible as CI state vanishing from the board for the first poll cycle).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from agentic_pr_dash import github_api


@pytest.fixture()
def token_env(tmp_path, monkeypatch):
    """Isolated XDG config root + captured subprocess.run env."""
    config_dir = tmp_path / "agentic-pr-dash"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(github_api, "_TOKEN_FILE_CACHE", None)

    captured: list[dict[str, str] | None] = []

    def fake_run(cmd, **kwargs):
        captured.append(kwargs.get("env"))
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setattr(github_api.subprocess, "run", fake_run)
    return config_dir / "gh-automation-token", captured


def _write_token(path, value, mtime=None):
    path.write_text(value + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_gh_call_uses_fresh_file_token_when_env_stale(token_env, monkeypatch):
    token_file, captured = token_env
    monkeypatch.setenv("GH_TOKEN", "stale-spawn-token")
    _write_token(token_file, "fresh-file-token")

    github_api._run(["gh", "pr", "list"])

    assert captured[-1] is not None
    assert captured[-1]["GH_TOKEN"] == "fresh-file-token"


def test_rotation_is_picked_up_without_restart(token_env, monkeypatch):
    token_file, captured = token_env
    monkeypatch.setenv("GH_TOKEN", "stale-spawn-token")
    _write_token(token_file, "token-generation-1", mtime=1_000_000)
    github_api._run(["gh", "pr", "list"])
    assert captured[-1]["GH_TOKEN"] == "token-generation-1"

    # The refresh daemon rotates the file mid-flight; the very next gh call
    # must use the new token with no process restart.
    _write_token(token_file, "token-generation-2", mtime=2_000_000)
    github_api._run(["gh", "pr", "list"])
    assert captured[-1]["GH_TOKEN"] == "token-generation-2"


def test_unchanged_mtime_serves_cached_token(token_env, monkeypatch):
    token_file, captured = token_env
    monkeypatch.setenv("GH_TOKEN", "stale-spawn-token")
    _write_token(token_file, "cached-token", mtime=1_000_000)
    github_api._run(["gh", "pr", "list"])
    assert captured[-1]["GH_TOKEN"] == "cached-token"

    # Same mtime -> no re-read (rotation always bumps mtime via atomic
    # replace, so equal mtime means "unchanged" by contract).
    _write_token(token_file, "rewritten-in-place", mtime=1_000_000)
    github_api._run(["gh", "pr", "list"])
    assert captured[-1]["GH_TOKEN"] == "cached-token"


def test_no_token_file_inherits_process_env(token_env, monkeypatch):
    _token_file, captured = token_env
    monkeypatch.setenv("GH_TOKEN", "spawn-token")

    github_api._run(["gh", "pr", "list"])

    assert captured[-1] is None


def test_human_identity_untouched_when_gh_token_unset(token_env, monkeypatch):
    """A process launched WITHOUT the automation identity (interactive human,
    keyring auth) must not be silently switched onto the automation token
    just because the file exists on the machine."""
    token_file, captured = token_env
    monkeypatch.delenv("GH_TOKEN", raising=False)
    _write_token(token_file, "automation-token")

    github_api._run(["gh", "pr", "list"])

    assert captured[-1] is None


def test_explicit_env_wins_over_file_token(token_env, monkeypatch):
    """Callers that construct a deliberate env (the resolve-capable-identity
    fallback) must never have it overridden by the file token."""
    token_file, captured = token_env
    monkeypatch.setenv("GH_TOKEN", "stale-spawn-token")
    _write_token(token_file, "fresh-file-token")
    explicit = dict(os.environ)
    explicit["GH_TOKEN"] = "resolve-capable-pat"

    github_api._run(["gh", "api", "graphql"], env=explicit)

    assert captured[-1]["GH_TOKEN"] == "resolve-capable-pat"


def test_non_gh_commands_inherit_env(token_env, monkeypatch):
    token_file, captured = token_env
    monkeypatch.setenv("GH_TOKEN", "stale-spawn-token")
    _write_token(token_file, "fresh-file-token")

    github_api._run(["git", "status"])

    assert captured[-1] is None


def test_file_token_equal_to_env_inherits(token_env, monkeypatch):
    """No pointless env copy when the spawn-time env is already current."""
    token_file, captured = token_env
    monkeypatch.setenv("GH_TOKEN", "current-token")
    _write_token(token_file, "current-token")

    github_api._run(["gh", "pr", "list"])

    assert captured[-1] is None
