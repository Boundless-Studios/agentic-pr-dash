"""BOU-2406: an indeterminate `gh` result must not read as a successful arm.

`gh pr view` issued seconds after `gh pr create` can fail transiently. The arm
path used to collapse every failure into `None`, print "gh unavailable" and
return 0 -- so the caller believed arming succeeded while no arm marker and no
session-ledger entry were written. With no ledger entry `_await_anchors` cannot
discover the worktree, so the feedback waiter then exits `unbound` having watched
nothing. One transient call silently removed CI coverage for a whole session,
behind two successful-looking steps.
"""

from __future__ import annotations

import subprocess

import pytest

from agentic_pr_dash._maintenance import pr_state


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Keep retry tests instant."""
    monkeypatch.setattr(pr_state, "_PR_VIEW_BACKOFF_SECONDS", 0)


class TestRetryOnTransientFailure:
    def test_succeeds_on_a_later_attempt(self, monkeypatch):
        """The exact shape observed: first probe fails, a later one works."""
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                return _Result(returncode=1, stderr="could not resolve to a PullRequest")
            return _Result(returncode=0, stdout='{"isDraft": false}')

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert pr_state._pr_draft_status("/repo", 2821) is False
        assert len(calls) == 3, "must retry rather than give up on the first failure"

    def test_gives_up_after_the_attempt_budget(self, monkeypatch):
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(1)
            return _Result(returncode=1, stderr="boom")

        monkeypatch.setattr(subprocess, "run", fake_run)
        status, why = pr_state._pr_draft_status_detailed("/repo", 2821)
        assert status is None
        assert len(calls) == pr_state._PR_VIEW_ATTEMPTS
        assert "boom" in why, "the captured stderr must reach the operator"

    def test_missing_gh_executable_is_not_retried(self, monkeypatch):
        """The one case that really is 'gh unavailable' -- retrying is pointless."""
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(1)
            raise FileNotFoundError("gh")

        monkeypatch.setattr(subprocess, "run", fake_run)
        status, why = pr_state._pr_draft_status_detailed("/repo", 2821)
        assert status is None
        assert len(calls) == 1, "a missing executable will not appear on a retry"
        assert "not found on PATH" in why


class TestDiagnosticsSurvive:
    def test_stderr_is_reported_not_discarded(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: _Result(returncode=1, stderr="HTTP 403: rate limit exceeded"),
        )
        _, why = pr_state._pr_draft_status_detailed("/repo", 2821)
        assert "rate limit exceeded" in why
        assert "gh unavailable" not in why, (
            "must report what actually happened, not assert gh is missing"
        )

    def test_non_json_output_is_distinguished(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _Result(returncode=0, stdout="not json")
        )
        _, why = pr_state._pr_draft_status_detailed("/repo", 2821)
        assert "non-JSON" in why

    def test_head_branch_reports_its_own_cause(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _Result(returncode=1, stderr="network is down")
        )
        branch, why = pr_state._pr_head_branch_detailed("/repo", 2821)
        assert branch is None
        assert "network is down" in why


class TestBackCompatWrappers:
    """The thin wrappers keep their original Optional contract for existing callers."""

    def test_draft_status_wrapper_returns_plain_bool(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _Result(returncode=0, stdout='{"isDraft": true}')
        )
        assert pr_state._pr_draft_status("/repo", 1) is True

    def test_head_branch_wrapper_returns_plain_str(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: _Result(returncode=0, stdout='{"headRefName": "feature/x"}'),
        )
        assert pr_state._pr_head_branch("/repo", 1) == "feature/x"

    def test_empty_head_branch_is_none(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: _Result(returncode=0, stdout='{"headRefName": ""}'),
        )
        assert pr_state._pr_head_branch("/repo", 1) is None
