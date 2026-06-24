"""Tests for github_api.required_checks_pending (BOU-1789 Task 1).

Uses the GraphQL statusCheckRollup so it sees required checks that are merely
EXPECTED (not yet reported — the cli/cli#8855 gap that `gh pr checks` misses),
in addition to queued/in_progress ones.
"""
from __future__ import annotations

import json
import types

import pytest

from agentic_pr_dash import config, github_api


@pytest.fixture(autouse=True)
def _repo(monkeypatch):
    # Pin the repo so resolved_repo() doesn't shell out to gh/git.
    monkeypatch.setenv("AGENTIC_PR_DASH_REPO", "owner/name")
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _rollup(*contexts) -> str:
    """Build a gh-api-graphql response envelope from context dicts."""
    return json.dumps({
        "data": {"repository": {"pullRequest": {"commits": {"nodes": [
            {"commit": {"statusCheckRollup": {"contexts": {"nodes": list(contexts)}}}}
        ]}}}}
    })


def _result(returncode: int, stdout: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def _check_run(status, *, required, conclusion=None):
    return {"__typename": "CheckRun", "status": status,
            "conclusion": conclusion, "isRequired": required}


def _status_context(state, *, required):
    return {"__typename": "StatusContext", "state": state, "isRequired": required}


def test_true_when_required_check_in_progress(monkeypatch):
    monkeypatch.setattr(github_api, "_run",
                        lambda cmd, **kw: _result(0, _rollup(_check_run("IN_PROGRESS", required=True))))
    assert github_api.required_checks_pending(42) is True


def test_true_when_required_check_expected_not_yet_reported(monkeypatch):
    """The cli/cli#8855 case: required check configured but not started — gh pr
    checks omits it; the rollup reports it as an EXPECTED StatusContext."""
    monkeypatch.setattr(github_api, "_run",
                        lambda cmd, **kw: _result(0, _rollup(_status_context("EXPECTED", required=True))))
    assert github_api.required_checks_pending(42) is True


def test_false_when_required_checks_terminal(monkeypatch):
    monkeypatch.setattr(github_api, "_run",
                        lambda cmd, **kw: _result(0, _rollup(
                            _check_run("COMPLETED", required=True, conclusion="SUCCESS"),
                            _status_context("SUCCESS", required=True),
                        )))
    assert github_api.required_checks_pending(42) is False


def test_false_when_only_non_required_pending(monkeypatch):
    """An in-progress NON-required check must not hold coverage open."""
    monkeypatch.setattr(github_api, "_run",
                        lambda cmd, **kw: _result(0, _rollup(
                            _check_run("IN_PROGRESS", required=False),
                            _check_run("COMPLETED", required=True, conclusion="SUCCESS"),
                        )))
    assert github_api.required_checks_pending(42) is False


def test_false_when_required_check_failed_terminal(monkeypatch):
    """A failed (terminal) required check is NOT watch-pending — the failure is
    handled by the ci_failure blocker path."""
    monkeypatch.setattr(github_api, "_run",
                        lambda cmd, **kw: _result(0, _rollup(
                            _check_run("COMPLETED", required=True, conclusion="FAILURE"))))
    assert github_api.required_checks_pending(42) is False


def test_false_on_gh_error(monkeypatch):
    monkeypatch.setattr(github_api, "_run", lambda cmd, **kw: _result(1, ""))
    assert github_api.required_checks_pending(42) is False


def test_false_on_unparseable_json(monkeypatch):
    monkeypatch.setattr(github_api, "_run", lambda cmd, **kw: _result(0, "not-json"))
    assert github_api.required_checks_pending(42) is False


def test_false_on_null_rollup(monkeypatch):
    """statusCheckRollup can be null (no checks yet) — fail-safe to False."""
    envelope = json.dumps({"data": {"repository": {"pullRequest": {"commits": {"nodes": [
        {"commit": {"statusCheckRollup": None}}]}}}}})
    monkeypatch.setattr(github_api, "_run", lambda cmd, **kw: _result(0, envelope))
    assert github_api.required_checks_pending(42) is False


def test_uses_graphql_with_owner_name_number(monkeypatch):
    captured = {}
    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _result(0, _rollup())
    monkeypatch.setattr(github_api, "_run", fake_run)
    github_api.required_checks_pending(42, cwd="/some/path")
    assert "graphql" in captured["cmd"]
    assert "owner=owner" in captured["cmd"]
    assert "name=name" in captured["cmd"]
    assert "number=42" in captured["cmd"]
