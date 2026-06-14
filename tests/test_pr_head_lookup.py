"""Tests for find_pr_by_head() and paginated get_review_threads().

These primitives back the gaia pre-completion Stop/QA gate, which resolves a PR
by *head branch* (not author) and needs the PR body + headRefOid for its
bugfix-RCA / merged-HEAD policy checks, plus full review-thread enumeration
across pagination.
"""

from __future__ import annotations

import json
import subprocess

from agentic_pr_dash import github_api


def _cp(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, "")


# --------------------------------------------------------------------------- #
# find_pr_by_head
# --------------------------------------------------------------------------- #

def test_find_pr_by_head_returns_full_open_pr(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, timeout_s=20, cwd=None):
        captured["cmd"] = cmd
        return _cp(json.dumps([
            {
                "number": 7,
                "title": "Fix the thing",
                "body": "## Root cause\nx",
                "url": "https://x/pull/7",
                "isDraft": False,
                "mergeStateStatus": "CLEAN",
                "reviewDecision": "APPROVED",
                "headRefOid": "deadbeef",
                "headRefName": "fix/thing",
                "baseRefName": "main",
            }
        ]))

    monkeypatch.setattr(github_api, "_run", fake_run)
    pr = github_api.find_pr_by_head("fix/thing", "open", ".")
    assert pr is not None
    assert pr["number"] == 7
    assert pr["body"] == "## Root cause\nx"
    assert pr["baseRefName"] == "main"
    # open lookups cap at limit 1
    assert "--limit" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--limit") + 1] == "1"


def test_find_pr_by_head_no_match_returns_none(monkeypatch):
    monkeypatch.setattr(github_api, "_run", lambda *a, **k: _cp("[]"))
    assert github_api.find_pr_by_head("fix/none", "open", ".") is None


def test_find_pr_by_head_gh_failure_returns_none(monkeypatch):
    monkeypatch.setattr(github_api, "_run", lambda *a, **k: _cp("", returncode=1))
    assert github_api.find_pr_by_head("fix/x", "open", ".") is None


def test_find_pr_by_head_empty_branch_returns_none(monkeypatch):
    called = {"n": 0}

    def fake_run(*a, **k):
        called["n"] += 1
        return _cp("[]")

    monkeypatch.setattr(github_api, "_run", fake_run)
    assert github_api.find_pr_by_head("", "open", ".") is None
    assert called["n"] == 0


def test_find_pr_by_head_oid_filter_matches(monkeypatch):
    payload = [
        {"number": 1, "headRefOid": "aaa", "url": "u1"},
        {"number": 2, "headRefOid": "bbb", "url": "u2"},
    ]
    monkeypatch.setattr(github_api, "_run", lambda *a, **k: _cp(json.dumps(payload)))
    pr = github_api.find_pr_by_head("b", "merged", ".", head_oid="bbb")
    assert pr is not None and pr["number"] == 2


def test_find_pr_by_head_oid_filter_no_match(monkeypatch):
    payload = [{"number": 1, "headRefOid": "aaa"}]
    monkeypatch.setattr(github_api, "_run", lambda *a, **k: _cp(json.dumps(payload)))
    assert github_api.find_pr_by_head("b", "merged", ".", head_oid="zzz") is None


def test_find_pr_by_head_merged_uses_wide_limit(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, timeout_s=20, cwd=None):
        captured["cmd"] = cmd
        return _cp("[]")

    monkeypatch.setattr(github_api, "_run", fake_run)
    github_api.find_pr_by_head("b", "merged", ".")
    assert captured["cmd"][captured["cmd"].index("--limit") + 1] == "20"


# --------------------------------------------------------------------------- #
# get_review_threads pagination
# --------------------------------------------------------------------------- #

def _thread_node(db_id: int, path: str, line: int, resolved=False, outdated=False):
    return {
        "id": f"node-{db_id}",
        "isResolved": resolved,
        "isOutdated": outdated,
        "comments": {
            "nodes": [
                {
                    "databaseId": db_id,
                    "path": path,
                    "line": line,
                    "body": "comment",
                    "author": {"login": "rev"},
                    "createdAt": "2026-01-01T00:00:00Z",
                }
            ]
        },
    }


def test_get_review_threads_paginates(monkeypatch):
    monkeypatch.setattr(github_api, "get_repo_info", lambda cwd=None: ("o", "r"))
    cursors: list[str | None] = []

    def fake_run(cmd, timeout_s=20, cwd=None):
        cursor = None
        for part in cmd:
            if isinstance(part, str) and part.startswith("cursor="):
                cursor = part.split("=", 1)[1]
        cursors.append(cursor)
        if cursor == "PAGE2":
            page = {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [_thread_node(999, "later.py", 42)],
            }
        else:
            page = {
                "pageInfo": {"hasNextPage": True, "endCursor": "PAGE2"},
                "nodes": [_thread_node(101, "old.py", 10, resolved=True)],
            }
        return _cp(json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": page}}}}))

    monkeypatch.setattr(github_api, "_run", fake_run)
    threads = github_api.get_review_threads(2111, ".")
    db_ids = {t.top.database_id for t in threads}
    assert db_ids == {101, 999}
    assert cursors == [None, "PAGE2"]


def test_get_review_threads_single_page(monkeypatch):
    monkeypatch.setattr(github_api, "get_repo_info", lambda cwd=None: ("o", "r"))

    def fake_run(cmd, timeout_s=20, cwd=None):
        page = {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [_thread_node(202, "new.py", 20)],
        }
        return _cp(json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": page}}}}))

    monkeypatch.setattr(github_api, "_run", fake_run)
    threads = github_api.get_review_threads(5, ".")
    assert len(threads) == 1
    assert threads[0].top.database_id == 202
