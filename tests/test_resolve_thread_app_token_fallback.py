"""App-token resolve fallback (BOU-1974).

GitHub App installation tokens (the automation identity introduced by
BOU-1923) CANNOT call the `resolveReviewThread` GraphQL mutation — GitHub
answers FORBIDDEN ("Resource not accessible by integration") even with full
permissions; it's a platform limitation, not a missing scope. Reads and
comments work fine under the App token; only resolving is broken. Left
unhandled, the detached maintenance loop can never actually close a review
thread once it exports the App `GH_TOKEN`, so it re-services already-fixed
PRs forever and never converges.

`github_api.resolve_review_thread` retries a FORBIDDEN resolve once with a
resolve-capable identity: `AGENTIC_PR_DASH_GH_RESOLVE_TOKEN` if configured,
else the same subprocess with `GH_TOKEN` unset (falls back to gh's ambient
identity). The override must be local to that one retry call — every other
gh call in the process still sees the original `GH_TOKEN`.
"""
from __future__ import annotations

import subprocess

import pytest

from agentic_pr_dash import github_api

FORBIDDEN_STDERR = (
    'gh: Resource not accessible by integration (HTTP 403)\n'
    '{"data":null,"errors":[{"type":"FORBIDDEN","path":["resolveReviewThread"],'
    '"message":"Resource not accessible by integration"}]}'
)


def _cp(returncode: int = 0, stdout: str = "{}", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# --------------------------------------------------------------------------- #
# FORBIDDEN under an App token -> retried once with GH_TOKEN unset
# --------------------------------------------------------------------------- #

def test_forbidden_under_app_token_retries_with_gh_token_unset(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "app-installation-token")
    monkeypatch.delenv("AGENTIC_PR_DASH_GH_RESOLVE_TOKEN", raising=False)

    calls: list[dict] = []

    def _fake_run_mutation(cmd, cwd=None, timeout_s=20, env=None):
        calls.append({"cmd": cmd, "cwd": cwd, "env": env})
        if len(calls) == 1:
            return _cp(returncode=1, stderr=FORBIDDEN_STDERR)
        return _cp(returncode=0)

    monkeypatch.setattr(github_api, "_run_mutation", _fake_run_mutation)

    result = github_api.resolve_review_thread("THREAD_ID", cwd="/repo")

    assert result is True
    assert len(calls) == 2
    # First attempt: no env override (inherits the App-token process env).
    assert calls[0]["env"] is None
    # Retry: a full-env copy with GH_TOKEN removed, not the App token.
    retry_env = calls[1]["env"]
    assert retry_env is not None
    assert "GH_TOKEN" not in retry_env
    # Same mutation, not some different request.
    assert calls[1]["cmd"] == calls[0]["cmd"]


def test_forbidden_without_gh_token_in_env_does_not_retry(monkeypatch):
    """No App token in play (human's own gh session) -> FORBIDDEN has some
    other cause; retrying with a different env can't help, so don't bother."""
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("AGENTIC_PR_DASH_GH_RESOLVE_TOKEN", raising=False)

    calls: list[dict] = []

    def _fake_run_mutation(cmd, cwd=None, timeout_s=20, env=None):
        calls.append({"env": env})
        return _cp(returncode=1, stderr=FORBIDDEN_STDERR)

    monkeypatch.setattr(github_api, "_run_mutation", _fake_run_mutation)

    result = github_api.resolve_review_thread("THREAD_ID")

    assert result is False
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# First attempt succeeds -> no fallback, single call
# --------------------------------------------------------------------------- #

def test_success_on_first_attempt_no_fallback_call(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "app-installation-token")

    calls: list[dict] = []

    def _fake_run_mutation(cmd, cwd=None, timeout_s=20, env=None):
        calls.append({"env": env})
        return _cp(returncode=0)

    monkeypatch.setattr(github_api, "_run_mutation", _fake_run_mutation)

    result = github_api.resolve_review_thread("THREAD_ID")

    assert result is True
    assert len(calls) == 1


def test_success_on_first_attempt_with_no_app_token_no_fallback_call(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)

    calls: list[dict] = []

    def _fake_run_mutation(cmd, cwd=None, timeout_s=20, env=None):
        calls.append({"env": env})
        return _cp(returncode=0)

    monkeypatch.setattr(github_api, "_run_mutation", _fake_run_mutation)

    result = github_api.resolve_review_thread("THREAD_ID")

    assert result is True
    assert len(calls) == 1


def test_non_forbidden_failure_does_not_retry(monkeypatch):
    """A different failure (e.g. bad thread id) must not trigger the fallback
    retry — only the FORBIDDEN/integration signature does."""
    monkeypatch.setenv("GH_TOKEN", "app-installation-token")

    calls: list[dict] = []

    def _fake_run_mutation(cmd, cwd=None, timeout_s=20, env=None):
        calls.append({"env": env})
        return _cp(returncode=1, stderr="gh: Could not resolve to a ReviewThread with the id THREAD_ID.")

    monkeypatch.setattr(github_api, "_run_mutation", _fake_run_mutation)

    result = github_api.resolve_review_thread("THREAD_ID")

    assert result is False
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# AGENTIC_PR_DASH_GH_RESOLVE_TOKEN configured -> used for the retry
# --------------------------------------------------------------------------- #

def test_configured_resolve_token_used_for_retry(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "app-installation-token")
    monkeypatch.setenv("AGENTIC_PR_DASH_GH_RESOLVE_TOKEN", "machine-user-pat")

    calls: list[dict] = []

    def _fake_run_mutation(cmd, cwd=None, timeout_s=20, env=None):
        calls.append({"env": env})
        if len(calls) == 1:
            return _cp(returncode=1, stderr=FORBIDDEN_STDERR)
        return _cp(returncode=0)

    monkeypatch.setattr(github_api, "_run_mutation", _fake_run_mutation)

    result = github_api.resolve_review_thread("THREAD_ID")

    assert result is True
    assert len(calls) == 2
    retry_env = calls[1]["env"]
    assert retry_env is not None
    assert retry_env["GH_TOKEN"] == "machine-user-pat"


# --------------------------------------------------------------------------- #
# No global env leakage: subsequent calls still see the original GH_TOKEN
# --------------------------------------------------------------------------- #

def test_fallback_env_override_is_local_no_global_leak(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "app-installation-token")
    monkeypatch.delenv("AGENTIC_PR_DASH_GH_RESOLVE_TOKEN", raising=False)

    calls: list[dict] = []

    def _fake_run_mutation(cmd, cwd=None, timeout_s=20, env=None):
        calls.append({"env": env})
        if len(calls) == 1:
            return _cp(returncode=1, stderr=FORBIDDEN_STDERR)
        return _cp(returncode=0)

    monkeypatch.setattr(github_api, "_run_mutation", _fake_run_mutation)

    github_api.resolve_review_thread("THREAD_ID")

    # The real process environment is untouched by the fallback retry.
    assert __import__("os").environ["GH_TOKEN"] == "app-installation-token"

    # A subsequent, unrelated resolve call starts fresh: first attempt again
    # has no env override, i.e. it still sees the original GH_TOKEN via
    # normal inheritance rather than some leaked stripped-down env.
    calls.clear()

    def _fake_run_mutation_2(cmd, cwd=None, timeout_s=20, env=None):
        calls.append({"env": env})
        return _cp(returncode=0)

    monkeypatch.setattr(github_api, "_run_mutation", _fake_run_mutation_2)

    result = github_api.resolve_review_thread("THREAD_ID_2")

    assert result is True
    assert len(calls) == 1
    assert calls[0]["env"] is None


# --------------------------------------------------------------------------- #
# _resolve_fallback_env unit coverage
# --------------------------------------------------------------------------- #

def test_resolve_fallback_env_none_when_no_gh_token(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert github_api._resolve_fallback_env() is None


def test_resolve_fallback_env_drops_gh_token_by_default(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "app-installation-token")
    monkeypatch.delenv("AGENTIC_PR_DASH_GH_RESOLVE_TOKEN", raising=False)

    env = github_api._resolve_fallback_env()

    assert env is not None
    assert "GH_TOKEN" not in env


def test_resolve_fallback_env_uses_configured_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "app-installation-token")
    monkeypatch.setenv("AGENTIC_PR_DASH_GH_RESOLVE_TOKEN", "machine-user-pat")

    env = github_api._resolve_fallback_env()

    assert env is not None
    assert env["GH_TOKEN"] == "machine-user-pat"


def test_resolve_fallback_env_refreshes_rotated_token_file(monkeypatch, tmp_path):
    config = tmp_path / "agentic-pr-dash"
    config.mkdir()
    token_file = config / "gh-resolve-token"
    token_file.write_text("fresh-machine-user-pat\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("GH_TOKEN", "app-installation-token")
    monkeypatch.setenv("AGENTIC_PR_DASH_GH_RESOLVE_TOKEN", "stale-pat")

    env = github_api._resolve_fallback_env()

    assert env is not None
    assert env["GH_TOKEN"] == "fresh-machine-user-pat"


def test_resolve_fallback_env_drops_github_token_too_ambient(monkeypatch):
    """gh falls back GH_TOKEN -> GITHUB_TOKEN; GITHUB_TOKEN is commonly the SAME
    App token (GitHub Actions / wrapper shells). Dropping only GH_TOKEN would let
    gh silently reuse GITHUB_TOKEN and stay FORBIDDEN. Ambient case: NEITHER."""
    monkeypatch.setenv("GH_TOKEN", "app-installation-token")
    monkeypatch.setenv("GITHUB_TOKEN", "app-installation-token")
    monkeypatch.delenv("AGENTIC_PR_DASH_GH_RESOLVE_TOKEN", raising=False)

    env = github_api._resolve_fallback_env()

    assert env is not None
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env


def test_resolve_fallback_env_drops_github_token_when_resolve_token_set(monkeypatch):
    """Resolve-token case: GH_TOKEN=the PAT and GITHUB_TOKEN removed so it can't
    shadow the PAT we just set."""
    monkeypatch.setenv("GH_TOKEN", "app-installation-token")
    monkeypatch.setenv("GITHUB_TOKEN", "app-installation-token")
    monkeypatch.setenv("AGENTIC_PR_DASH_GH_RESOLVE_TOKEN", "machine-user-pat")

    env = github_api._resolve_fallback_env()

    assert env is not None
    assert env["GH_TOKEN"] == "machine-user-pat"
    assert "GITHUB_TOKEN" not in env


def test_resolve_fallback_env_local_no_global_github_token_leak(monkeypatch):
    """The per-call env override never touches the real process environment."""
    monkeypatch.setenv("GH_TOKEN", "app-installation-token")
    monkeypatch.setenv("GITHUB_TOKEN", "app-installation-token")
    monkeypatch.delenv("AGENTIC_PR_DASH_GH_RESOLVE_TOKEN", raising=False)

    github_api._resolve_fallback_env()

    import os as _os
    assert _os.environ["GH_TOKEN"] == "app-installation-token"
    assert _os.environ["GITHUB_TOKEN"] == "app-installation-token"


# --------------------------------------------------------------------------- #
# Fallback log message: printed once, not spammed
# --------------------------------------------------------------------------- #

def test_fallback_log_message_printed_once(monkeypatch, capsys):
    monkeypatch.setenv("GH_TOKEN", "app-installation-token")
    monkeypatch.delenv("AGENTIC_PR_DASH_GH_RESOLVE_TOKEN", raising=False)

    def _fake_run_mutation(cmd, cwd=None, timeout_s=20, env=None):
        if env is None:
            return _cp(returncode=1, stderr=FORBIDDEN_STDERR)
        return _cp(returncode=0)

    monkeypatch.setattr(github_api, "_run_mutation", _fake_run_mutation)

    github_api.resolve_review_thread("THREAD_A")
    github_api.resolve_review_thread("THREAD_B")

    err = capsys.readouterr().err
    assert err.count("resolveReviewThread FORBIDDEN") == 1
