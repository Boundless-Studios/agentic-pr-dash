"""BOU-1980 — get_ci_checks REST fallback for GitHub App tokens.

Under a GitHub App installation token without Actions:read, gh's
``pr checks``/``statusCheckRollup`` GraphQL (which requests
``checkSuite.workflowRun``) fails with "Resource not accessible by
integration" and EMPTY stdout. The primary path cannot distinguish that from
"no checks", so every PR with running/failing CI silently read as Clean.
The REST check-runs endpoint needs only Checks:read; fall back to it whenever
the primary path yields unparseable stdout.
"""
from __future__ import annotations

import json
import types

from agentic_pr_dash import github_api


def _result(rc, stdout, stderr=""):
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


def _dispatching_run(responses):
    """Return a fake ``_run`` that routes on a substring of the gh command."""

    def fake_run(cmd, **kw):
        joined = " ".join(cmd)
        for needle, result in responses.items():
            if needle in joined:
                return result
        raise AssertionError(f"unexpected gh command: {joined}")

    return fake_run


def _rest_lines(*runs):
    return "\n".join(json.dumps(r) for r in runs)


def test_fallback_surfaces_pending_check_when_primary_unparseable(monkeypatch):
    """The App-token failure shape: `gh pr checks` exits 1 with empty stdout.
    The REST fallback must surface the in-progress check so the PR reads
    CI_PENDING, not CLEAN."""
    monkeypatch.setattr(github_api, "_run", _dispatching_run({
        "pr checks": _result(1, "", "GraphQL: Resource not accessible by integration"),
        "/commits --jq": _result(0, "abc123\t2026-07-10T23:43:20Z\n"),
        "/check-runs": _result(0, _rest_lines(
            {"name": "Unit Tests", "status": "in_progress", "conclusion": None},
            {"name": "Setup", "status": "completed", "conclusion": "success"},
        )),
    }))
    checks = {c.name: c for c in github_api.get_ci_checks(7)}
    assert checks["Unit Tests"].status == "in_progress"
    assert checks["Unit Tests"].conclusion is None
    assert checks["Setup"].conclusion == "success"


def test_fallback_normalizes_failish_conclusions(monkeypatch):
    """REST conclusions gh buckets as `fail` must normalize to "failure" so
    orchestrator failing_checks (conclusion == "failure") keeps firing."""
    monkeypatch.setattr(github_api, "_run", _dispatching_run({
        "pr checks": _result(1, ""),
        "/commits --jq": _result(0, "abc123\t2026-07-10T23:43:20Z\n"),
        "/check-runs": _result(0, _rest_lines(
            {"name": "t1", "status": "completed", "conclusion": "failure"},
            {"name": "t2", "status": "completed", "conclusion": "timed_out"},
            {"name": "t3", "status": "completed", "conclusion": "startup_failure"},
            {"name": "t4", "status": "completed", "conclusion": "action_required"},
            {"name": "t5", "status": "completed", "conclusion": "cancelled"},
            {"name": "t6", "status": "completed", "conclusion": "skipped"},
        )),
    }))
    concl = {c.name: c.conclusion for c in github_api.get_ci_checks(7)}
    assert concl == {
        "t1": "failure", "t2": "failure", "t3": "failure", "t4": "failure",
        "t5": "cancelled", "t6": "skipped",
    }


def test_fallback_returns_empty_when_no_head_sha(monkeypatch):
    monkeypatch.setattr(github_api, "_run", _dispatching_run({
        "pr checks": _result(1, ""),
        "/commits --jq": _result(1, ""),
    }))
    assert github_api.get_ci_checks(7) == []


def test_fallback_returns_empty_when_rest_also_fails(monkeypatch):
    monkeypatch.setattr(github_api, "_run", _dispatching_run({
        "pr checks": _result(1, ""),
        "/commits --jq": _result(0, "abc123\t2026-07-10T23:43:20Z\n"),
        "/check-runs": _result(1, "not json"),
    }))
    assert github_api.get_ci_checks(7) == []


def test_primary_path_still_wins_when_parseable(monkeypatch):
    """A parseable primary response must never trigger the REST fallback."""
    checks = [{"name": "build", "bucket": "pending", "state": "in_progress"}]

    def fake_run(cmd, **kw):
        joined = " ".join(cmd)
        assert "check-runs" not in joined, "REST fallback must not run"
        return _result(8, json.dumps(checks))

    monkeypatch.setattr(github_api, "_run", fake_run)
    result = github_api.get_ci_checks(7)
    assert [(c.name, c.status) for c in result] == [("build", "in_progress")]
