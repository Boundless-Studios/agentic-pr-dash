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


# --------------------------------------------------------------------------- #
# process-level rate-limit-backoff suppression (BOU-1953) — the stop-gate is a
# Stop-hook subprocess with a hard ~108s deadline and must fail fast on a
# rate-limit instead of accumulating backoff; the waiter keeps backing off.
# --------------------------------------------------------------------------- #

def test_run_with_backoff_disabled_returns_immediately_on_persistent_rate_limit(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(github_api.time, "sleep", lambda s: slept.append(s))
    calls = {"n": 0}

    def _always(*a, **k):
        calls["n"] += 1
        return _cp(returncode=1, stderr="API rate limit exceeded")

    monkeypatch.setattr(subprocess, "run", _always)
    github_api.set_rate_limit_backoff(False)
    github_api.reset_rate_limit_seen()

    r = github_api._run(["gh", "pr", "list"])

    assert r.returncode == 1
    assert calls["n"] == 1  # no rate-limit retries — fails fast on the first call
    assert not slept  # no backoff sleep occurred
    assert github_api.rate_limit_seen() is True  # flag semantics preserved


def test_run_with_backoff_disabled_still_retries_connectivity_failures(monkeypatch):
    """Disabling rate-limit backoff must not touch the short, unrelated
    connectivity retry (transient DNS/connect/5xx failures)."""
    monkeypatch.setattr(github_api.time, "sleep", lambda *_: None)
    stub, calls = _sequence_run([
        _cp(returncode=1, stderr="error connecting to api.github.com"),
        _cp(stdout="[]", returncode=0),
    ])
    monkeypatch.setattr(subprocess, "run", stub)
    github_api.set_rate_limit_backoff(False)

    r = github_api._run(["gh", "pr", "list"])

    assert r.returncode == 0
    assert calls["n"] == 2  # connectivity retry still happened


def test_run_with_backoff_enabled_default_still_backs_off(monkeypatch):
    """Sanity check: the default (enabled) mode is unchanged by the toggle's
    existence — a persistent rate-limit still retries up to the bound."""
    monkeypatch.setattr(github_api.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def _always(*a, **k):
        calls["n"] += 1
        return _cp(returncode=1, stderr="API rate limit exceeded")

    monkeypatch.setattr(subprocess, "run", _always)
    assert github_api.rate_limit_backoff_enabled() is True  # default

    r = github_api._run(["gh", "pr", "list"])

    assert r.returncode == 1
    assert calls["n"] == github_api._GH_RETRY_ATTEMPTS  # unchanged, bounded retry


def test_stop_gate_disables_rate_limit_backoff(tmp_path, monkeypatch):
    """The stop-gate entrypoint must disable backoff at the START of its run so
    a rate-limited gh call inside it fails fast instead of wedging toward the
    Stop-hook's hard deadline (BOU-1953)."""
    from types import SimpleNamespace

    import agentic_pr_dash._maintenance.worktree_check as worktree_check_mod
    import agentic_pr_dash._maintenance.worktrees as worktrees_mod
    from agentic_pr_dash._maintenance import stop_gate

    assert github_api.rate_limit_backoff_enabled() is True  # precondition

    # Stub out everything _stop_gate_impl touches past the toggle so this stays
    # a focused unit test of the toggle, not a full stop-gate integration test.
    # These are imported *locally* inside `_stop_gate_impl`, so patch the
    # defining modules (read at call time) rather than the `stop_gate` module.
    monkeypatch.setattr(
        worktrees_mod, "_owned_worktrees_across_roots", lambda *a, **k: [str(tmp_path)]
    )
    monkeypatch.setattr(
        worktrees_mod, "_reconcile_owned_across_roots", lambda *a, **k: ([], [])
    )
    monkeypatch.setattr(
        worktrees_mod, "_detached_records_across_roots", lambda *a, **k: []
    )

    seen_enabled_during_call: list[bool] = []

    def _fake_check_worktree(worktree, session_id, claim=False):
        seen_enabled_during_call.append(github_api.rate_limit_backoff_enabled())
        return 0, ""

    monkeypatch.setattr(worktree_check_mod, "_check_worktree", _fake_check_worktree)

    args = SimpleNamespace(cwd=str(tmp_path), session_id="", pid=None, no_waiter=True)
    stop_gate._stop_gate_impl(args)

    assert github_api.rate_limit_backoff_enabled() is False
    assert seen_enabled_during_call == [False]
