"""BOU-1980 — get_ci_checks REST fallback for GitHub App tokens.

Under a GitHub App installation token without Actions:read, gh's
``pr checks``/``statusCheckRollup`` GraphQL (which requests
``checkSuite.workflowRun``) fails with "Resource not accessible by
integration" and EMPTY stdout. The primary path cannot distinguish that from
"no checks", so every PR with running/failing CI silently read as Clean.
The REST endpoints need only Checks:read; fall back to them whenever the
primary path yields unparseable stdout.

Codex PR #71 review hardening:
- head SHA comes from REST ``pulls/{n}`` (not ``pulls/{n}/commits`` page 1,
  which truncates at 30 commits and returns a stale SHA on big PRs);
- the fallback reuses get_check_runs_for_commit → paginated check-runs PLUS
  legacy commit StatusContexts;
- non-completed REST statuses (waiting/requested/pending) normalize into the
  queued/in_progress set downstream pending predicates match;
- ``stale`` joins the fail-normalization set (terminal, not success).
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


def _jsonl(*objs):
    return "\n".join(json.dumps(o) for o in objs)


_HEAD = {"pulls/7 --jq .head.sha": _result(0, "abc123\n")}


def test_fallback_surfaces_pending_check_when_primary_unparseable(monkeypatch):
    """The App-token failure shape: `gh pr checks` exits 1 with empty stdout.
    The REST fallback must surface the in-progress check so the PR reads
    CI_PENDING, not CLEAN."""
    monkeypatch.setattr(github_api, "_run", _dispatching_run({
        "pr checks": _result(1, "", "GraphQL: Resource not accessible by integration"),
        **_HEAD,
        "/check-runs": _result(0, _jsonl(
            {"name": "Unit Tests", "status": "in_progress", "conclusion": None},
            {"name": "Setup", "status": "completed", "conclusion": "success"},
        )),
        "abc123/status": _result(0, ""),
    }))
    checks = {c.name: c for c in github_api.get_ci_checks(7)}
    assert checks["Unit Tests"].status == "in_progress"
    assert checks["Unit Tests"].conclusion is None
    assert checks["Setup"].conclusion == "success"


def test_fallback_normalizes_failish_conclusions_including_stale(monkeypatch):
    """REST conclusions gh buckets as `fail` — plus terminal-but-not-success
    `stale` — must normalize to "failure" so orchestrator failing_checks
    (conclusion == "failure") keeps firing."""
    monkeypatch.setattr(github_api, "_run", _dispatching_run({
        "pr checks": _result(1, ""),
        **_HEAD,
        "/check-runs": _result(0, _jsonl(
            {"name": "t1", "status": "completed", "conclusion": "failure"},
            {"name": "t2", "status": "completed", "conclusion": "timed_out"},
            {"name": "t3", "status": "completed", "conclusion": "startup_failure"},
            {"name": "t4", "status": "completed", "conclusion": "action_required"},
            {"name": "t5", "status": "completed", "conclusion": "stale"},
            {"name": "t6", "status": "completed", "conclusion": "cancelled"},
            {"name": "t7", "status": "completed", "conclusion": "skipped"},
        )),
        "abc123/status": _result(0, ""),
    }))
    concl = {c.name: c.conclusion for c in github_api.get_ci_checks(7)}
    assert concl == {
        "t1": "failure", "t2": "failure", "t3": "failure", "t4": "failure",
        "t5": "failure", "t6": "cancelled", "t7": "skipped",
    }


def test_fallback_normalizes_noncompleted_statuses_to_pending(monkeypatch):
    """REST also emits waiting/requested/pending — downstream pending
    predicates match only queued/in_progress, so anything non-completed must
    land in that set or a pending PR reads as clean."""
    monkeypatch.setattr(github_api, "_run", _dispatching_run({
        "pr checks": _result(1, ""),
        **_HEAD,
        "/check-runs": _result(0, _jsonl(
            {"name": "w", "status": "waiting", "conclusion": None},
            {"name": "r", "status": "requested", "conclusion": None},
            {"name": "p", "status": "pending", "conclusion": None},
            {"name": "q", "status": "queued", "conclusion": None},
        )),
        "abc123/status": _result(0, ""),
    }))
    statuses = {c.name: c.status for c in github_api.get_ci_checks(7)}
    assert set(statuses.values()) <= {"queued", "in_progress"}
    assert statuses["q"] == "queued"


def test_fallback_includes_legacy_commit_statuses(monkeypatch):
    """Repos reporting CI via legacy commit StatusContexts must not read as
    check-less in the fallback (the primary rollup path includes them)."""
    monkeypatch.setattr(github_api, "_run", _dispatching_run({
        "pr checks": _result(1, ""),
        **_HEAD,
        "/check-runs": _result(0, ""),
        "abc123/status": _result(0, _jsonl(
            {"context": "external-ci/build", "state": "pending"},
            {"context": "external-ci/lint", "state": "failure"},
        )),
    }))
    checks = {c.name: c for c in github_api.get_ci_checks(7)}
    assert checks["external-ci/build"].status == "in_progress"
    assert checks["external-ci/lint"].conclusion == "failure"


def test_fallback_uses_pr_head_not_commit_list(monkeypatch):
    """The head SHA must come from REST pulls/{n} (.head.sha) — the unpaginated
    pulls/{n}/commits read truncates at 30 commits and yields a stale SHA."""

    def fake_run(cmd, **kw):
        joined = " ".join(cmd)
        assert f"pulls/7/commits" not in joined, "must not read the commit list"
        if "pr checks" in joined:
            return _result(1, "")
        if "pulls/7 --jq .head.sha" in joined:
            return _result(0, "headsha42\n")
        if "headsha42/check-runs" in joined:
            return _result(0, _jsonl({"name": "b", "status": "in_progress", "conclusion": None}))
        if "headsha42/status" in joined:
            return _result(0, "")
        raise AssertionError(f"unexpected gh command: {joined}")

    monkeypatch.setattr(github_api, "_run", fake_run)
    result = github_api.get_ci_checks(7)
    assert [(c.name, c.status) for c in result] == [("b", "in_progress")]


def test_fallback_returns_empty_when_no_head_sha(monkeypatch):
    monkeypatch.setattr(github_api, "_run", _dispatching_run({
        "pr checks": _result(1, ""),
        "pulls/7 --jq .head.sha": _result(1, ""),
    }))
    assert github_api.get_ci_checks(7) == []


def test_fallback_returns_empty_when_rest_also_fails(monkeypatch):
    monkeypatch.setattr(github_api, "_run", _dispatching_run({
        "pr checks": _result(1, ""),
        **_HEAD,
        "/check-runs": _result(1, "not json"),
        "abc123/status": _result(1, ""),
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
