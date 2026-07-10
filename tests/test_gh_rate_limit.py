"""gh rate-limit resilience (BOU-1921).

The `await` waiter is a long-lived poller; dying (or misfiring) the moment a
shared GitHub API budget is exhausted defeats its purpose. `_run` already
retries transient *connectivity* failures — this extends the same bounded,
backoff-with-retry treatment to rate-limit failures (primary GraphQL exhaustion
and the velocity-triggered *secondary*/abuse limit), honoring a parseable
``Retry-After`` but CAPPING the sleep so latency-sensitive callers (the stop
gate) never wedge on a primary reset that can be up to an hour away.

`list_open_prs` additionally tags its failure diagnostic ``reason="rate-limit"``
so the poll path can distinguish "quota, back off" from a hard failure.
"""
from __future__ import annotations

import subprocess

from agentic_pr_dash import github_api
from agentic_pr_dash._maintenance import pr_state


def _cp(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _sequence_run(results):
    calls = {"n": 0}

    def _stub(*a, **k):
        i = calls["n"]
        calls["n"] += 1
        return results[min(i, len(results) - 1)]

    return _stub, calls


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #

def test_is_rate_limit_failure_detects_primary_and_secondary():
    assert github_api._is_rate_limit_failure(
        _cp(returncode=1, stderr="API rate limit exceeded for user ID 1"))
    assert github_api._is_rate_limit_failure(
        _cp(returncode=1, stderr="You have exceeded a secondary rate limit. Please wait a few minutes"))
    assert github_api._is_rate_limit_failure(
        _cp(returncode=1, stderr="was submitted too quickly"))


def test_is_rate_limit_failure_ignores_success_and_auth():
    assert not github_api._is_rate_limit_failure(_cp(stdout="[]", returncode=0))
    assert not github_api._is_rate_limit_failure(
        _cp(returncode=1, stderr="not logged into any GitHub hosts"))


# --------------------------------------------------------------------------- #
# _run retries rate-limit failures (bounded) with capped backoff
# --------------------------------------------------------------------------- #

def test_run_retries_secondary_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setattr(github_api.time, "sleep", lambda *_: None)
    stub, calls = _sequence_run([
        _cp(returncode=1, stderr="You have exceeded a secondary rate limit"),
        _cp(stdout="[]", returncode=0),
    ])
    monkeypatch.setattr(subprocess, "run", stub)

    r = github_api._run(["gh", "pr", "list"])
    assert r.returncode == 0
    assert calls["n"] == 2  # rate-limited once, then recovered


def test_run_rate_limit_backoff_is_bounded(monkeypatch):
    monkeypatch.setattr(github_api.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def _always(*a, **k):
        calls["n"] += 1
        return _cp(returncode=1, stderr="API rate limit exceeded")

    monkeypatch.setattr(subprocess, "run", _always)
    r = github_api._run(["gh", "pr", "list"])
    assert r.returncode == 1
    assert calls["n"] == github_api._GH_RETRY_ATTEMPTS  # bounded, not infinite


def test_run_rate_limit_sleep_capped(monkeypatch):
    """A large Retry-After must be clamped so the stop gate never wedges on a
    primary-reset (up to an hour) wait."""
    slept: list[float] = []
    monkeypatch.setattr(github_api.time, "sleep", lambda s: slept.append(s))
    stub, _ = _sequence_run([
        _cp(returncode=1, stderr="API rate limit exceeded\nRetry-After: 3600"),
        _cp(stdout="[]", returncode=0),
    ])
    monkeypatch.setattr(subprocess, "run", stub)

    github_api._run(["gh", "pr", "list"])
    assert slept, "expected a backoff sleep"
    assert all(s <= github_api._GH_RATELIMIT_MAX_SLEEP_S for s in slept)


# --------------------------------------------------------------------------- #
# list_open_prs tags the rate-limit reason
# --------------------------------------------------------------------------- #

def test_list_open_prs_tags_rate_limit_reason(monkeypatch):
    # _run itself is stubbed (persistent rate-limit failure) so no real backoff.
    monkeypatch.setattr(
        github_api, "_run",
        lambda *a, **k: _cp(returncode=1, stderr="You have exceeded a secondary rate limit"),
    )
    assert github_api.list_open_prs(".") is None  # failure → None, never []
    failure = github_api.last_list_open_prs_failure()
    assert failure is not None
    assert failure.reason == "rate-limit"


# --------------------------------------------------------------------------- #
# per-tick rate-limit-seen flag (BOU-1921 #62) — set by _run for ANY gh call,
# reset/read by the tick-based waiter
# --------------------------------------------------------------------------- #

def test_run_sets_rate_limit_seen_on_persistent_rate_limit(monkeypatch):
    monkeypatch.setattr(github_api.time, "sleep", lambda *_: None)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _cp(returncode=1, stderr="API rate limit exceeded"))
    github_api.reset_rate_limit_seen()
    assert github_api.rate_limit_seen() is False
    github_api._run(["gh", "pr", "list"])
    assert github_api.rate_limit_seen() is True


def test_run_does_not_set_rate_limit_seen_on_success(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _cp(stdout="[]", returncode=0))
    github_api.reset_rate_limit_seen()
    github_api._run(["gh", "pr", "list"])
    assert github_api.rate_limit_seen() is False


def test_run_does_not_set_rate_limit_seen_on_hard_failure(monkeypatch):
    monkeypatch.setattr(github_api.time, "sleep", lambda *_: None)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _cp(returncode=1, stderr="not logged into any GitHub hosts"))
    github_api.reset_rate_limit_seen()
    github_api._run(["gh", "pr", "list"])
    assert github_api.rate_limit_seen() is False  # hard failure is not a quota wall


def test_pr_open_state_rate_limit_sets_flag(monkeypatch):
    """_pr_open_state's `gh pr view` must flow through github_api._run so a
    rate-limit there sets the per-tick flag — a detached-only waiter relies on it
    (BOU-1949, #62 follow-up)."""
    monkeypatch.setattr(github_api.time, "sleep", lambda *_: None)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: _cp(returncode=1, stderr="API rate limit exceeded"))
    github_api.reset_rate_limit_seen()
    state, *_ = pr_state._pr_open_state(7, ".")
    assert state == "unknown"  # unobservable
    assert github_api.rate_limit_seen() is True  # ...but the flag IS set now


def test_run_rate_limit_recovers_on_retry_leaves_flag_clear(monkeypatch):
    """A rate-limit that clears on retry means GitHub WAS observable — flag stays clear."""
    monkeypatch.setattr(github_api.time, "sleep", lambda *_: None)
    stub, _ = _sequence_run([
        _cp(returncode=1, stderr="You have exceeded a secondary rate limit"),
        _cp(stdout="[]", returncode=0),
    ])
    monkeypatch.setattr(subprocess, "run", stub)
    github_api.reset_rate_limit_seen()
    github_api._run(["gh", "pr", "list"])
    assert github_api.rate_limit_seen() is False
