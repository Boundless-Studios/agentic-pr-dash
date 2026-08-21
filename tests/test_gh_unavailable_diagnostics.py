"""Regression tests for surfacing real ``gh`` diagnostics (BOU-1638 / BOU-1694).

Before this change, ANY failure of ``list_open_prs`` (timeout, gh-not-on-PATH,
auth lapse, rate-limit, malformed JSON) collapsed to an opaque
"could not list PRs (gh unavailable)" — the completion path aborted with zero
actionable detail even when the exact ``gh pr list`` ran fine from the operator's
shell. These tests pin that:

* ``_run`` no longer blanks stderr on ``TimeoutExpired`` / ``OSError``.
* ``list_open_prs`` records structured diagnostics (and preserves the load-bearing
  ``None`` (=failure) vs ``[]`` (=no PRs) distinction so a transient outage never
  prunes tracked PRs).
* the operator-facing ``complete`` / ``check`` output now contains the underlying
  stderr + a remediation hint, NOT just "gh unavailable".
"""

from __future__ import annotations

import argparse
import subprocess

from agentic_pr_dash import github_api, maintenance_check
from agentic_pr_dash._maintenance import markers as _markers_mod
from agentic_pr_dash._maintenance import pr_state as _pr_state_mod


def _cp(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# --------------------------------------------------------------------------- #
# _run preserves the failure detail
# --------------------------------------------------------------------------- #

def test_run_captures_oserror_into_stderr(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "gh")

    monkeypatch.setattr(subprocess, "run", boom)
    r = github_api._run(["gh", "pr", "list"])
    assert r.returncode != 0
    # The exception text is preserved instead of a blank "" stderr.
    assert r.stderr
    assert "FileNotFoundError" in r.stderr
    assert "No such file or directory" in r.stderr


def test_run_captures_timeout_into_stderr(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=["gh", "pr", "list"], timeout=30)

    monkeypatch.setattr(subprocess, "run", boom)
    r = github_api._run(["gh", "pr", "list"], timeout_s=30)
    assert r.returncode != 0
    assert "timed out" in r.stderr.lower()
    assert "30s" in r.stderr


# --------------------------------------------------------------------------- #
# list_open_prs records diagnostics and preserves None-vs-[] invariant
# --------------------------------------------------------------------------- #

def test_list_open_prs_records_failure_diagnostics(monkeypatch):
    monkeypatch.setattr(
        github_api, "_run",
        lambda *a, **k: _cp(returncode=1, stderr="gh: command not found"),
    )
    assert github_api.list_open_prs(".") is None  # failure → None (NOT [])
    failure = github_api.last_list_open_prs_failure()
    assert failure is not None
    assert failure.returncode == 1
    assert "gh: command not found" in failure.stderr
    assert "gh" in failure.command_str and "pr" in failure.command_str
    # describe() carries stderr + a re-runnable self-check command.
    described = failure.describe()
    assert "gh: command not found" in described
    assert "remediation" in described
    assert "gh pr list" in described


def test_list_open_prs_clears_diagnostics_on_success(monkeypatch):
    # Prime a stale failure, then succeed — the stale diagnostic must not bleed.
    monkeypatch.setattr(
        github_api, "_run", lambda *a, **k: _cp(returncode=1, stderr="boom"))
    assert github_api.list_open_prs(".") is None
    assert github_api.last_list_open_prs_failure() is not None

    monkeypatch.setattr(github_api, "_run", lambda *a, **k: _cp(stdout="[]"))
    assert github_api.list_open_prs(".") == []  # genuinely no PRs → [] (NOT None)
    assert github_api.last_list_open_prs_failure() is None


def test_none_vs_empty_invariant_distinguishes_outage_from_no_prs(monkeypatch):
    # [] = genuinely no open PRs.
    monkeypatch.setattr(github_api, "_run", lambda *a, **k: _cp(stdout="[]"))
    assert github_api.list_open_prs(".") == []
    # None = the gh call failed; must never degrade to [] (which would prune
    # every tracked PR on a transient outage).
    monkeypatch.setattr(
        github_api, "_run", lambda *a, **k: _cp(returncode=1, stderr="rate limited"))
    assert github_api.list_open_prs(".") is None


# --------------------------------------------------------------------------- #
# complete / check surface the real diagnostics, not "gh unavailable" alone
# --------------------------------------------------------------------------- #

def _simulate_gh_connectivity_failure(monkeypatch):
    """Make every gh shell-out fail like the BOU-1638 subprocess (OSError path)."""
    def boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "gh")

    monkeypatch.setattr(subprocess, "run", boom)


def test_cmd_complete_surfaces_underlying_stderr(monkeypatch, capsys):
    _simulate_gh_connectivity_failure(monkeypatch)
    args = argparse.Namespace(cwd=".", pr="123", baseline="")
    code = maintenance_check._cmd_complete(args)
    out = capsys.readouterr().out

    assert code == 2
    # Still flags the gh-unavailable condition for back-compat scanners…
    assert "gh unavailable" in out
    # …but now ALSO carries the real evidence and a fix hint, not just the bare
    # opaque string.
    assert "FileNotFoundError" in out or "No such file or directory" in out
    assert "remediation" in out
    assert "gh pr list" in out
    # The opaque-only line must no longer be the entire message.
    assert out.strip() != "could not list PRs (gh unavailable)"


def test_check_worktree_surfaces_underlying_stderr(monkeypatch):
    _simulate_gh_connectivity_failure(monkeypatch)
    # Force a non-empty branch so resolution proceeds to list_open_prs.
    # _resolve_pr_for_branch lives in pr_state and uses _current_branch from pr_state's namespace.
    monkeypatch.setattr(_pr_state_mod, "_current_branch", lambda cwd: "feature/x")
    # ``_check_worktree`` calls ``markers._live_foreign_owner`` module-qualified
    # (not the maintenance_check re-export), so patch the marker module — else a
    # REAL armed marker at cwd="." (e.g. this dev worktree's own PR-watch marker)
    # makes the marker-owner gate fire and the gh-unavailable path is never
    # reached. This is the seam the subject actually reads (hermetic in CI).
    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda *a, **k: None)

    code, text = maintenance_check._check_worktree(".", "self-session")
    assert code == 2
    assert "gh unavailable" in text
    assert "FileNotFoundError" in text or "No such file or directory" in text
    assert "remediation" in text
    assert text.strip() != "could not list PRs (gh unavailable)"


# --------------------------------------------------------------------------- #
# _run RETRIES transient connectivity failures (BOU-1694: robust reach, not
# just diagnosable) — the actual fix for "the gh subprocess often can't reach
# GitHub while the shell can."
# --------------------------------------------------------------------------- #

def _sequence_run(results):
    """subprocess.run stub yielding the given CompletedProcess results in order
    (repeating the last), recording how many times it was called."""
    calls = {"n": 0}

    def _stub(*a, **k):
        i = calls["n"]
        calls["n"] += 1
        return results[min(i, len(results) - 1)]

    return _stub, calls


def test_run_retries_transient_connectivity_then_succeeds(monkeypatch):
    monkeypatch.setattr(github_api.time, "sleep", lambda *_: None)
    stub, calls = _sequence_run([
        _cp(returncode=1, stderr="error connecting to api.github.com"),
        _cp(returncode=1, stderr="dial tcp: connection reset by peer"),
        _cp(stdout="[]", returncode=0),
    ])
    monkeypatch.setattr(subprocess, "run", stub)

    r = github_api._run(["gh", "pr", "list"])
    assert r.returncode == 0
    assert calls["n"] == 3  # two transient failures, then success


def test_run_does_not_retry_non_connectivity_failure(monkeypatch):
    monkeypatch.setattr(github_api.time, "sleep", lambda *_: None)
    stub, calls = _sequence_run([
        _cp(returncode=1, stderr="gh auth status: not logged into any GitHub hosts"),
    ])
    monkeypatch.setattr(subprocess, "run", stub)

    r = github_api._run(["gh", "pr", "list"])
    assert r.returncode == 1
    assert calls["n"] == 1  # auth failure surfaces immediately, never retried


def test_run_does_not_retry_full_timeout_hang(monkeypatch):
    # A full-duration gh hang must NOT be retried — that would multiply the
    # worst-case stop-gate latency by the attempt count.
    monkeypatch.setattr(github_api.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def _hang(*a, **k):
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd=["gh", "pr", "list"], timeout=20)

    monkeypatch.setattr(subprocess, "run", _hang)
    r = github_api._run(["gh", "pr", "list"], timeout_s=20)
    assert r.returncode != 0
    assert "timed out" in r.stderr.lower()
    assert calls["n"] == 1


def test_run_gives_up_after_bounded_attempts(monkeypatch):
    monkeypatch.setattr(github_api.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def _always_fail(*a, **k):
        calls["n"] += 1
        return _cp(returncode=1, stderr="error connecting to api.github.com")

    monkeypatch.setattr(subprocess, "run", _always_fail)
    r = github_api._run(["gh", "pr", "list"])
    assert r.returncode == 1
    assert calls["n"] == github_api._GH_RETRY_ATTEMPTS  # bounded, not infinite


def test_list_open_prs_recovers_via_retry(monkeypatch):
    """BOU-1694 AC: the completion path retries successfully when gh works
    moments later from the same cwd."""
    monkeypatch.setattr(github_api.time, "sleep", lambda *_: None)
    stub, calls = _sequence_run([
        _cp(returncode=1, stderr="error connecting to api.github.com"),
        _cp(stdout='[{"number": 7, "author": {"login": "viewer"}}]', returncode=0),
        _cp(stdout="viewer\n", returncode=0),
    ])
    monkeypatch.setattr(subprocess, "run", stub)

    prs = github_api.list_open_prs(".")
    assert prs == [{"number": 7, "author": {"login": "viewer"}}]
    assert github_api.last_list_open_prs_failure() is None  # cleared on success
    assert calls["n"] == 3


# --------------------------------------------------------------------------- #
# BOU-1966: quota-safe REST fallback for PR resolution.
#
# A rate-limited `gh pr list --author @me` (GraphQL) used to fail the
# maintenance check closed even when the target PR was concretely verifiable
# via per-PR REST endpoints. The resolvers now fall back to REST when — and
# only when — the list failure is quota/rate-limit classified. Auth/missing-gh
# failures, REST-also-failing, and a rate-limit DURING the detail fetch all
# still fail closed.
# --------------------------------------------------------------------------- #

_QUOTA_STDERR = "GraphQL: API rate limit already exceeded for installation ID 1"

_REST_PR_123 = {
    "number": 123,
    "title": "Quota-safe fallback",
    "body": "body",
    "html_url": "https://github.com/o/r/pull/123",
    "draft": False,
    "mergeable": True,
    "mergeable_state": "clean",
    "merged_at": None,
    "user": {"login": "octo"},
    "head": {"ref": "feature/x", "sha": "abc123", "repo": {"owner": {"login": "o"}}},
    "base": {"ref": "main"},
}


def _quota_dispatch_run(
    calls, *, rest_ok=True, head_numbers='[{"number": 123}]',
    pr_payload=None, viewer_ok=True,
):
    """``github_api._run`` stub: the author list is GraphQL-quota-blocked while
    the REST endpoints answer (or 403 when ``rest_ok=False``). Records every
    command so tests can assert which paths were (not) taken. The viewer
    lookup (``GET /user``, resolving the ``@me`` author) answers ``octo`` —
    matching ``_REST_PR_123``'s author — unless ``viewer_ok=False``."""
    import json as _json

    payload = _REST_PR_123 if pr_payload is None else pr_payload

    def run(cmd, timeout_s=20, cwd=None, env=None):
        calls.append(list(cmd))
        joined = " ".join(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return _cp(returncode=1, stderr=_QUOTA_STDERR)
        if not rest_ok:
            return _cp(returncode=1, stderr="HTTP 403: API rate limit exceeded")
        if joined.endswith("user --jq .login"):
            if not viewer_ok:
                return _cp(returncode=1, stderr="HTTP 403: Resource not accessible")
            return _cp(stdout="octo\n")
        if ".owner.login" in joined:
            return _cp(stdout="o\n")
        if "pulls?head=" in joined:
            return _cp(stdout=head_numbers)
        if joined.endswith("pulls/123"):
            return _cp(stdout=_json.dumps(payload))
        raise AssertionError(f"unexpected gh call: {joined}")

    return run


def _patch_detail_getters(monkeypatch):
    monkeypatch.setattr(
        github_api, "get_latest_commit",
        lambda n, cwd=None: ("abc123", "2026-07-17T00:00:00Z"))
    monkeypatch.setattr(github_api, "get_ci_checks", lambda n, cwd=None: [])
    monkeypatch.setattr(
        github_api, "get_unaddressed_comments", lambda n, d, cwd=None: [])


def test_ghfailure_rate_limit_classifier():
    def failure(stderr, reason="exit"):
        return github_api.GhFailure(
            command=["gh"], returncode=1, stderr=stderr, reason=reason)

    assert failure("", reason="rate-limit").is_rate_limited
    # The GraphQL primary-quota phrasing ("already exceeded") — the exact
    # BOU-1966 symptom — must classify as a rate-limit.
    assert failure(_QUOTA_STDERR).is_rate_limited
    assert failure("You have exceeded a secondary rate limit").is_rate_limited
    assert not failure("To get started with GitHub CLI: gh auth login").is_rate_limited
    assert not failure("FileNotFoundError: No such file or directory: 'gh'").is_rate_limited


def test_quota_blocked_list_falls_back_to_rest_by_number(monkeypatch):
    calls = []
    monkeypatch.setattr(github_api, "_run", _quota_dispatch_run(calls))
    _patch_detail_getters(monkeypatch)
    monkeypatch.setattr(
        _pr_state_mod,
        "_gh_pr_view_field",
        lambda cwd, number, field: ("APPROVED", ""),
    )

    pr = _pr_state_mod._resolve_pr_by_number(123, ".", force=True)
    assert pr is not _pr_state_mod._GH_UNAVAILABLE and pr is not None
    assert pr.number == 123
    assert pr.branch == "feature/x"
    assert pr.base_branch == "main"
    assert pr.is_draft is False
    assert pr.merge_state == "CLEAN"  # REST mergeable_state, GraphQL-normalized
    assert pr.mergeable == "MERGEABLE"
    failure = github_api.last_list_open_prs_failure()
    assert failure is not None and failure.is_rate_limited


def test_quota_blocked_list_falls_back_to_rest_for_branch(monkeypatch):
    calls = []
    monkeypatch.setattr(github_api, "_run", _quota_dispatch_run(calls))
    monkeypatch.setattr(_pr_state_mod, "_current_branch", lambda cwd: "feature/x")
    _patch_detail_getters(monkeypatch)

    pr = _pr_state_mod._resolve_pr_for_branch(".", force=True)
    assert pr is not _pr_state_mod._GH_UNAVAILABLE and pr is not None
    assert pr.number == 123
    assert pr.branch == "feature/x"
    # The head lookup must use the owner-qualified REST endpoint, never the
    # GraphQL `gh pr list --head` path (which is exactly what's quota-blocked).
    assert not any("--head" in c for c in calls)
    assert any("pulls?head=" in " ".join(c) for c in calls)


def test_auth_failure_does_not_fall_back_to_rest(monkeypatch):
    calls = []

    def run(cmd, timeout_s=20, cwd=None, env=None):
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "pr", "list"]:
            return _cp(
                returncode=1,
                stderr="To get started with GitHub CLI, please run: gh auth login")
        raise AssertionError(
            f"REST fallback must not run on an auth failure: {' '.join(cmd)}")

    monkeypatch.setattr(github_api, "_run", run)
    monkeypatch.setattr(_pr_state_mod, "_current_branch", lambda cwd: "feature/x")

    assert _pr_state_mod._resolve_pr_by_number(
        123, ".", force=True) is _pr_state_mod._GH_UNAVAILABLE
    assert _pr_state_mod._resolve_pr_for_branch(
        ".", force=True) is _pr_state_mod._GH_UNAVAILABLE
    # Only the (failing) list calls ran; no REST endpoint was touched.
    assert all(c[:3] == ["gh", "pr", "list"] for c in calls)


def test_quota_blocked_and_rest_also_failing_stays_fail_closed(monkeypatch):
    calls = []
    monkeypatch.setattr(github_api, "_run", _quota_dispatch_run(calls, rest_ok=False))
    monkeypatch.setattr(_pr_state_mod, "_current_branch", lambda cwd: "feature/x")

    assert _pr_state_mod._resolve_pr_by_number(
        123, ".", force=True) is _pr_state_mod._GH_UNAVAILABLE
    assert _pr_state_mod._resolve_pr_for_branch(
        ".", force=True) is _pr_state_mod._GH_UNAVAILABLE


def test_quota_fallback_branch_without_rest_match_stays_fail_closed(monkeypatch):
    # REST healthy but no owner-qualified PR for this head → still fail closed:
    # the owner-qualified query cannot see a fork-backed head, so an empty
    # result under quota exhaustion must not read as a verified "no PR".
    calls = []
    monkeypatch.setattr(
        github_api, "_run", _quota_dispatch_run(calls, head_numbers="[]"))
    monkeypatch.setattr(_pr_state_mod, "_current_branch", lambda cwd: "feature/x")

    assert _pr_state_mod._resolve_pr_for_branch(
        ".", force=True) is _pr_state_mod._GH_UNAVAILABLE


def test_quota_fallback_branch_rejects_foreign_author(monkeypatch):
    # The normal path is author-scoped (`gh pr list --author <pr_author>`), so
    # under rate limiting a shared-repo branch carrying ANOTHER maintainer's
    # open PR must not be adopted as this session's PR (PR #77 review).
    calls = []
    foreign = dict(_REST_PR_123, user={"login": "mallory"})
    monkeypatch.setattr(
        github_api, "_run", _quota_dispatch_run(calls, pr_payload=foreign))
    monkeypatch.setattr(_pr_state_mod, "_current_branch", lambda cwd: "feature/x")

    assert _pr_state_mod._resolve_pr_for_branch(
        ".", force=True) is _pr_state_mod._GH_UNAVAILABLE
    # The candidate WAS fetched and rejected on authorship, not on resolution.
    assert any(" ".join(c).endswith("pulls/123") for c in calls)


def test_quota_fallback_branch_fails_closed_when_viewer_unresolvable(monkeypatch):
    # `pr_author = "@me"` needs the REST viewer login to verify authorship; if
    # GET /user fails (e.g. an App installation token), authorship is
    # unverifiable and the fallback must fail closed, not adopt the PR.
    calls = []
    monkeypatch.setattr(
        github_api, "_run", _quota_dispatch_run(calls, viewer_ok=False))
    monkeypatch.setattr(_pr_state_mod, "_current_branch", lambda cwd: "feature/x")

    assert _pr_state_mod._resolve_pr_for_branch(
        ".", force=True) is _pr_state_mod._GH_UNAVAILABLE


def test_quota_fallback_branch_explicit_author_skips_viewer_lookup(monkeypatch):
    # A configured non-@me pr_author compares directly against the payload
    # author — no GET /user call is spent (and none may be relied on: under an
    # automation identity /user is exactly what's unavailable).
    import types

    from agentic_pr_dash import config as config_mod

    calls = []
    monkeypatch.setattr(
        github_api, "_run", _quota_dispatch_run(calls, viewer_ok=False))
    monkeypatch.setattr(_pr_state_mod, "_current_branch", lambda cwd: "feature/x")
    fake_load = lambda cwd=None: types.SimpleNamespace(pr_author="octo")  # noqa: E731
    # conftest's _isolate_config teardown calls config.load.cache_clear() while
    # this patch may still be in place — give the fake a no-op.
    fake_load.cache_clear = lambda: None
    monkeypatch.setattr(config_mod, "load", fake_load)
    _patch_detail_getters(monkeypatch)

    pr = _pr_state_mod._resolve_pr_for_branch(".", force=True)
    assert pr is not _pr_state_mod._GH_UNAVAILABLE and pr is not None
    assert pr.number == 123
    assert not any(" ".join(c).endswith("user --jq .login") for c in calls)


def test_login_key_normalizes_app_and_bot_forms():
    # gh's `--author` accepts `app/<name>` for Apps while REST serializes the
    # same identity as `<name>[bot]`; logins are case-insensitive.
    assert _pr_state_mod._login_key("app/gaia-bot") == "gaia-bot"
    assert _pr_state_mod._login_key("gaia-bot[bot]") == "gaia-bot"
    assert _pr_state_mod._login_key("Octo") == _pr_state_mod._login_key("octo")
    assert _pr_state_mod._login_key("octo") != _pr_state_mod._login_key("mallory")


def test_quota_fallback_detail_rate_limit_stays_fail_closed(monkeypatch):
    # REST resolves the PR, but a detail fetch ULTIMATELY rate-limits — the
    # empty detail results are unreliable, so the resolver must fail closed.
    # Uses the monotonic event counter: the tick-scoped rate_limit_seen() flag
    # is already set by the failed list, so a boolean guard could not see this.
    calls = []
    monkeypatch.setattr(github_api, "_run", _quota_dispatch_run(calls))
    monkeypatch.setattr(
        github_api, "get_latest_commit",
        lambda n, cwd=None: ("abc123", "2026-07-17T00:00:00Z"))
    monkeypatch.setattr(github_api, "get_ci_checks", lambda n, cwd=None: [])

    def _comments_rate_limited(n, d, cwd=None):
        github_api._RATE_LIMIT_EVENTS = github_api._RATE_LIMIT_EVENTS + 1
        return []

    monkeypatch.setattr(
        github_api, "get_unaddressed_comments", _comments_rate_limited)

    assert _pr_state_mod._resolve_pr_by_number(
        123, ".", force=True) is _pr_state_mod._GH_UNAVAILABLE


def test_quota_fallback_preserves_formal_review_decision(monkeypatch):
    calls = []
    monkeypatch.setattr(github_api, "_run", _quota_dispatch_run(calls))
    _patch_detail_getters(monkeypatch)
    monkeypatch.setattr(
        _pr_state_mod,
        "_gh_pr_view_field",
        lambda cwd, number, field: ("CHANGES_REQUESTED", ""),
    )

    pr = _pr_state_mod._resolve_pr_by_number(123, ".", force=True)

    assert pr is not _pr_state_mod._GH_UNAVAILABLE and pr is not None
    assert pr.review_decision == "CHANGES_REQUESTED"


def test_rest_pr_payload_normalizes_to_graphql_shape(monkeypatch):
    import json as _json

    monkeypatch.setattr(
        github_api, "_run", lambda *a, **k: _cp(stdout=_json.dumps(_REST_PR_123)))
    payload = github_api._rest_pr_payload(123, cwd=".")
    assert payload == {
        "number": 123,
        "state": "",
        "author": {"login": "octo"},
        "title": "Quota-safe fallback",
        "body": "body",
        "url": "https://github.com/o/r/pull/123",
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "",
        "headRefOid": "abc123",
        "headRefName": "feature/x",
        "headRepositoryOwner": {"login": "o"},
        "baseRefName": "main",
        "mergedAt": None,
        "mergeable": "MERGEABLE",
    }


def test_gh_unavailable_message_mentions_rest_fallback_on_quota(monkeypatch):
    monkeypatch.setattr(
        github_api, "_run", lambda *a, **k: _cp(returncode=1, stderr=_QUOTA_STDERR))
    assert github_api.list_open_prs(".") is None
    msg = _pr_state_mod._gh_unavailable_message(".")
    assert "gh unavailable" in msg
    assert "REST" in msg and "BOU-1966" in msg
