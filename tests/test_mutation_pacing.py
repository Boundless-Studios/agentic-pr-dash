"""Mutation pacing + Retry-After (BOU-1923 Bucket 4).

Content mutations (review-thread replies / resolves) fired back-to-back from
the completion path trip GitHub's velocity-triggered secondary/abuse rate
limit even when the primary quota has headroom. ``github_api._pace_mutation``
enforces a small minimum spacing between consecutive mutation calls, and
``github_api._run_mutation`` additionally honors a parseable ``Retry-After``
with one bounded extra sleep+retry when a mutation is still rate-limited after
``_run``'s own internal retries are exhausted.
"""
from __future__ import annotations

import subprocess

import pytest

from agentic_pr_dash import github_api


def _cp(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# --------------------------------------------------------------------------- #
# _pace_mutation spaces consecutive calls by >= the configured interval
# --------------------------------------------------------------------------- #

def test_pace_mutation_first_call_never_sleeps(monkeypatch):
    github_api.reset_mutation_pacing()
    slept: list[float] = []
    monkeypatch.setattr(github_api.time, "sleep", lambda s: slept.append(s))

    github_api._pace_mutation()

    assert slept == []


def test_pace_mutation_spaces_two_back_to_back_calls(monkeypatch):
    github_api.reset_mutation_pacing()
    monkeypatch.setattr(github_api, "_MUTATION_MIN_INTERVAL_S", 1.0)

    clock = {"t": 1_000.0}

    def _fake_monotonic():
        return clock["t"]

    slept: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(github_api.time, "monotonic", _fake_monotonic)
    monkeypatch.setattr(github_api.time, "sleep", _fake_sleep)

    github_api._pace_mutation()
    assert slept == []  # first call: nothing to pace against yet

    clock["t"] += 0.2  # only 200ms elapsed before the next mutation attempt
    github_api._pace_mutation()

    assert slept == pytest.approx([1.0 - 0.2])


def test_pace_mutation_no_sleep_once_interval_already_elapsed(monkeypatch):
    github_api.reset_mutation_pacing()
    monkeypatch.setattr(github_api, "_MUTATION_MIN_INTERVAL_S", 1.0)

    clock = {"t": 1_000.0}
    monkeypatch.setattr(github_api.time, "monotonic", lambda: clock["t"])
    slept: list[float] = []
    monkeypatch.setattr(github_api.time, "sleep", lambda s: slept.append(s))

    github_api._pace_mutation()
    clock["t"] += 5.0  # plenty of time has already passed
    github_api._pace_mutation()

    assert slept == []


# --------------------------------------------------------------------------- #
# _run_mutation: pacing gate + Retry-After-aware single extra retry
# --------------------------------------------------------------------------- #

def test_run_mutation_paces_two_back_to_back_mutations(monkeypatch):
    github_api.reset_mutation_pacing()
    monkeypatch.setattr(github_api, "_MUTATION_MIN_INTERVAL_S", 1.0)
    monkeypatch.setattr(github_api, "_run", lambda *a, **k: _cp(stdout="{}", returncode=0))

    clock = {"t": 2_000.0}
    monkeypatch.setattr(github_api.time, "monotonic", lambda: clock["t"])
    slept: list[float] = []

    def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(github_api.time, "sleep", _fake_sleep)

    github_api._run_mutation(["gh", "api", "graphql"])
    clock["t"] += 0.1
    github_api._run_mutation(["gh", "api", "graphql"])

    assert slept, "second back-to-back mutation must have paced"
    assert slept[0] >= 1.0 - 0.1 - 1e-9


def test_run_mutation_honors_retry_after_and_retries_once(monkeypatch):
    github_api.reset_mutation_pacing()
    monkeypatch.setattr(github_api, "_MUTATION_MIN_INTERVAL_S", 0.0)
    slept: list[float] = []
    monkeypatch.setattr(github_api.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(
        github_api, "_run",
        lambda *a, **k: _cp(returncode=1, stderr="secondary rate limit\nRetry-After: 3"),
    )
    monkeypatch.setattr(
        github_api, "_run_once",
        lambda *a, **k: _cp(stdout='{"ok": true}', returncode=0),
    )

    result = github_api._run_mutation(["gh", "api", "graphql"])

    assert result.returncode == 0
    assert slept == [3]


def test_run_mutation_restamps_pacing_after_retry_after_retry(monkeypatch):
    """After a Retry-After sleep+retry succeeds, the pacing timestamp must
    reflect the RETRY time — not the pre-sleep attempt — so the next mutation
    still waits a full interval instead of firing immediately and reintroducing
    the secondary-limit burst (BOU-1923 review #3)."""
    github_api.reset_mutation_pacing()
    monkeypatch.setattr(github_api, "_MUTATION_MIN_INTERVAL_S", 1.0)

    clock = {"t": 100.0}
    monkeypatch.setattr(github_api.time, "monotonic", lambda: clock["t"])

    def _fake_sleep(seconds: float) -> None:
        clock["t"] += seconds

    monkeypatch.setattr(github_api.time, "sleep", _fake_sleep)

    # First mutation: rate-limited with Retry-After: 3, retry succeeds.
    monkeypatch.setattr(
        github_api, "_run",
        lambda *a, **k: _cp(returncode=1, stderr="secondary rate limit\nRetry-After: 3"),
    )
    monkeypatch.setattr(github_api, "_run_once", lambda *a, **k: _cp(stdout="{}", returncode=0))

    github_api._run_mutation(["gh", "api", "graphql"])

    # The Retry-After sleep advanced monotonic from 100 -> 103; the stamp must
    # be 103 (the retry time), NOT the 100 set before the sleep.
    assert github_api._LAST_MUTATION_MONOTONIC == 103.0

    # A subsequent, non-rate-limited mutation ~0.4s later must still pace: only
    # 0.4s of the 1.0s interval has elapsed since the retry, so it sleeps ~0.6s.
    monkeypatch.setattr(github_api, "_run", lambda *a, **k: _cp(returncode=0))
    clock["t"] += 0.4  # -> 103.4
    slept2: list[float] = []

    def _sleep2(seconds: float) -> None:
        slept2.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(github_api.time, "sleep", _sleep2)

    github_api._run_mutation(["gh", "api", "graphql"])

    assert slept2 == pytest.approx([1.0 - 0.4])


def test_run_mutation_caps_large_retry_after(monkeypatch):
    github_api.reset_mutation_pacing()
    monkeypatch.setattr(github_api, "_MUTATION_MIN_INTERVAL_S", 0.0)
    slept: list[float] = []
    monkeypatch.setattr(github_api.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(
        github_api, "_run",
        lambda *a, **k: _cp(returncode=1, stderr="secondary rate limit\nRetry-After: 3600"),
    )
    monkeypatch.setattr(github_api, "_run_once", lambda *a, **k: _cp(returncode=0))

    github_api._run_mutation(["gh", "api", "graphql"])

    assert slept == [github_api._GH_RATELIMIT_MAX_SLEEP_S]


def test_run_mutation_without_retry_after_does_not_extra_retry(monkeypatch):
    """No parseable Retry-After -> no extra sleep/retry beyond `_run`'s own
    (already-exhausted) attempts; the rate-limited result is returned as-is."""
    github_api.reset_mutation_pacing()
    monkeypatch.setattr(github_api, "_MUTATION_MIN_INTERVAL_S", 0.0)
    slept: list[float] = []
    monkeypatch.setattr(github_api.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(
        github_api, "_run",
        lambda *a, **k: _cp(returncode=1, stderr="secondary rate limit"),
    )
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("must not call _run_once without a Retry-After hint")

    monkeypatch.setattr(github_api, "_run_once", _boom)

    result = github_api._run_mutation(["gh", "api", "graphql"])

    assert result.returncode == 1
    assert calls["n"] == 0
    assert slept == []


# --------------------------------------------------------------------------- #
# the public mutation functions route through _run_mutation (pacing + retry)
# --------------------------------------------------------------------------- #

def test_resolve_review_thread_uses_run_mutation(monkeypatch):
    calls = []
    monkeypatch.setattr(
        github_api, "_run_mutation",
        lambda *a, **k: calls.append((a, k)) or _cp(returncode=0),
    )
    assert github_api.resolve_review_thread("THREAD_ID") is True
    assert len(calls) == 1


def test_edit_review_comment_uses_run_mutation(monkeypatch):
    calls = []
    monkeypatch.setattr(
        github_api, "_run_mutation",
        lambda *a, **k: calls.append((a, k)) or _cp(returncode=0),
    )
    assert github_api.edit_review_comment(123, "body") is True
    assert len(calls) == 1


def test_reply_to_review_comment_inline_uses_run_mutation(monkeypatch):
    from agentic_pr_dash.models import ReviewComment

    calls = []
    monkeypatch.setattr(
        github_api, "_run_mutation",
        lambda *a, **k: calls.append((a, k)) or _cp(stdout='{"id": 42}', returncode=0),
    )
    comment = ReviewComment(
        id=1, author="alice", body="hi", created_at="2026-01-01T00:00:00Z", is_inline=True,
    )
    result = github_api.reply_to_review_comment(7, comment, "reply body")
    assert result == 42
    assert len(calls) == 1
