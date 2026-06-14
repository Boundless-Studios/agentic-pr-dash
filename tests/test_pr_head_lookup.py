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
    # open lookups fetch a wide page (not --limit 1) so a prefix match can't
    # occupy the single slot and drop the exact-branch PR.
    assert "--limit" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--limit") + 1] == "30"


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
        {"number": 1, "headRefOid": "aaa", "headRefName": "b", "url": "u1"},
        {"number": 2, "headRefOid": "bbb", "headRefName": "b", "url": "u2"},
    ]
    monkeypatch.setattr(github_api, "_run", lambda *a, **k: _cp(json.dumps(payload)))
    pr = github_api.find_pr_by_head("b", "merged", ".", head_oid="bbb")
    assert pr is not None and pr["number"] == 2


def test_find_pr_by_head_oid_filter_no_match(monkeypatch):
    payload = [{"number": 1, "headRefOid": "aaa", "headRefName": "b"}]
    monkeypatch.setattr(github_api, "_run", lambda *a, **k: _cp(json.dumps(payload)))
    assert github_api.find_pr_by_head("b", "merged", ".", head_oid="zzz") is None


def test_find_pr_by_head_requires_exact_head_match(monkeypatch):
    # `--head fix` is a prefix filter and can return `fix-123`; we must reject it.
    payload = [
        {"number": 9, "headRefOid": "x", "headRefName": "fix-123", "url": "u"},
    ]
    monkeypatch.setattr(github_api, "_run", lambda *a, **k: _cp(json.dumps(payload)))
    assert github_api.find_pr_by_head("fix", "open", ".") is None


def test_find_pr_by_head_strips_owner_qualifier(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, timeout_s=20, cwd=None):
        captured["cmd"] = cmd
        return _cp(json.dumps([
            {"number": 4, "headRefName": "feature", "headRefOid": "y",
             "headRepositoryOwner": {"login": "alice"}, "url": "u"},
        ]))

    monkeypatch.setattr(github_api, "_run", fake_run)
    pr = github_api.find_pr_by_head("alice:feature", "open", ".")
    assert pr is not None and pr["number"] == 4
    # the owner qualifier is stripped before --head (owner is post-filtered)
    assert captured["cmd"][captured["cmd"].index("--head") + 1] == "feature"


def test_find_pr_by_head_merged_uses_wide_limit(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, timeout_s=20, cwd=None):
        captured["cmd"] = cmd
        return _cp("[]")

    monkeypatch.setattr(github_api, "_run", fake_run)
    github_api.find_pr_by_head("b", "merged", ".")
    assert captured["cmd"][captured["cmd"].index("--limit") + 1] == "30"


def test_find_pr_by_head_prefix_match_not_dropped(monkeypatch):
    # `--head fix` is a prefix filter and a wide page returns both `fix-123`
    # (prefix) and the exact `fix`. With the old `--limit 1` the prefix match
    # could occupy the single slot and the exact PR would be dropped → None.
    # A wide page + exact-filter must still find the exact-branch PR.
    payload = [
        {"number": 11, "headRefName": "fix-123", "headRefOid": "a", "url": "u1"},
        {"number": 22, "headRefName": "fix", "headRefOid": "b", "url": "u2"},
    ]
    monkeypatch.setattr(github_api, "_run", lambda *a, **k: _cp(json.dumps(payload)))
    pr = github_api.find_pr_by_head("fix", "open", ".")
    assert pr is not None and pr["number"] == 22


def test_find_pr_by_head_fork_owner_match(monkeypatch):
    # Two fork PRs share branch name `feature`. An owner-qualified head
    # (`alice:feature`) must return alice's PR, not bob's.
    payload = [
        {"number": 1, "headRefName": "feature", "headRefOid": "a",
         "headRepositoryOwner": {"login": "bob"}, "url": "u1"},
        {"number": 2, "headRefName": "feature", "headRefOid": "b",
         "headRepositoryOwner": {"login": "alice"}, "url": "u2"},
    ]
    captured: dict = {}

    def fake_run(cmd, timeout_s=20, cwd=None):
        captured["cmd"] = cmd
        return _cp(json.dumps(payload))

    monkeypatch.setattr(github_api, "_run", fake_run)
    pr = github_api.find_pr_by_head("alice:feature", "open", ".")
    assert pr is not None and pr["number"] == 2
    # the owner qualifier is stripped before --head, but headRepositoryOwner is
    # requested so the owner can be post-filtered.
    assert captured["cmd"][captured["cmd"].index("--head") + 1] == "feature"
    fields = captured["cmd"][captured["cmd"].index("--json") + 1]
    assert "headRepositoryOwner" in fields


def test_find_pr_by_head_fork_owner_mismatch_returns_none(monkeypatch):
    # Only bob's same-named branch exists; alice:feature must NOT match it.
    payload = [
        {"number": 1, "headRefName": "feature", "headRefOid": "a",
         "headRepositoryOwner": {"login": "bob"}, "url": "u1"},
    ]
    monkeypatch.setattr(github_api, "_run", lambda *a, **k: _cp(json.dumps(payload)))
    assert github_api.find_pr_by_head("alice:feature", "open", ".") is None


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


def test_get_review_threads_first_page_failure_returns_empty(monkeypatch):
    # Total unavailability on page 1 → [] (fail open, like find_pr_by_head).
    monkeypatch.setattr(github_api, "get_repo_info", lambda cwd=None: ("o", "r"))
    monkeypatch.setattr(github_api, "_run", lambda *a, **k: _cp("", returncode=1))
    assert github_api.get_review_threads(5, ".") == []


def test_get_review_threads_later_page_failure_raises(monkeypatch):
    # Page 1 advertises hasNextPage; page 2 fails → must NOT return a partial
    # list (silent truncation hazard) — raise instead.
    import pytest

    monkeypatch.setattr(github_api, "get_repo_info", lambda cwd=None: ("o", "r"))

    def fake_run(cmd, timeout_s=20, cwd=None):
        cursor = None
        for part in cmd:
            if isinstance(part, str) and part.startswith("cursor="):
                cursor = part.split("=", 1)[1]
        if cursor == "PAGE2":
            return _cp("", returncode=1)  # transient failure on page 2
        page = {
            "pageInfo": {"hasNextPage": True, "endCursor": "PAGE2"},
            "nodes": [_thread_node(101, "old.py", 10)],
        }
        return _cp(json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": page}}}}))

    monkeypatch.setattr(github_api, "_run", fake_run)
    with pytest.raises(RuntimeError, match="partial thread list"):
        github_api.get_review_threads(5, ".")


def test_get_review_threads_next_page_without_cursor_raises(monkeypatch):
    # GitHub reports hasNextPage=true but omits/empties endCursor: we can't
    # advance, so threads on the unreachable page would be silently dropped.
    # Must raise rather than return a truncated list.
    import pytest

    monkeypatch.setattr(github_api, "get_repo_info", lambda cwd=None: ("o", "r"))

    def fake_run(cmd, timeout_s=20, cwd=None):
        page = {
            "pageInfo": {"hasNextPage": True, "endCursor": None},
            "nodes": [_thread_node(101, "old.py", 10)],
        }
        return _cp(json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": page}}}}))

    monkeypatch.setattr(github_api, "_run", fake_run)
    with pytest.raises(RuntimeError, match="partial thread list"):
        github_api.get_review_threads(5, ".")


def test_get_review_threads_later_page_malformed_raises(monkeypatch):
    import pytest

    monkeypatch.setattr(github_api, "get_repo_info", lambda cwd=None: ("o", "r"))

    def fake_run(cmd, timeout_s=20, cwd=None):
        cursor = None
        for part in cmd:
            if isinstance(part, str) and part.startswith("cursor="):
                cursor = part.split("=", 1)[1]
        if cursor == "PAGE2":
            return _cp("not json")  # malformed page 2
        page = {
            "pageInfo": {"hasNextPage": True, "endCursor": "PAGE2"},
            "nodes": [_thread_node(101, "old.py", 10)],
        }
        return _cp(json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": page}}}}))

    monkeypatch.setattr(github_api, "_run", fake_run)
    with pytest.raises(RuntimeError, match="partial thread list"):
        github_api.get_review_threads(5, ".")
