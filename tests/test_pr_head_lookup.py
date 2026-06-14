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
#
# find_pr_by_head now resolves heads via an EXACT REST query
# (`gh api repos/{owner}/{repo}/pulls?head=owner:branch`) and then fetches the
# full Stop/QA-gate contract fields per match via `gh pr view <n> --json`.
# The helper below builds a `_run` fake that dispatches on the gh subcommand so
# tests can describe both stages declaratively.
# --------------------------------------------------------------------------- #

def _install_gh_fake(
    monkeypatch,
    *,
    rest_pages,
    pr_views,
    repo_owner="acme",
    repo_name="repo",
):
    """Patch ``github_api._run`` with a gh dispatcher.

    ``rest_pages`` is a list of REST `pulls` page payloads (each a list of dicts
    with at least ``number``), returned in order for successive `gh api ...pulls`
    calls. ``pr_views`` maps PR number -> the full ``gh pr view --json`` dict (or
    a (returncode, stdout) failure tuple). ``gh repo view`` returns owner/name.
    """
    state = {"rest_idx": 0, "calls": []}
    rest_iter = list(rest_pages)

    def fake_run(cmd, timeout_s=20, cwd=None):
        state["calls"].append(cmd)
        # gh repo view --json owner,name  → repo resolution
        if cmd[:3] == ["gh", "repo", "view"]:
            return _cp(json.dumps({"owner": {"login": repo_owner}, "name": repo_name}))
        # gh api ...repos/{owner}/{repo}/pulls?... → exact-head REST query
        if cmd[:2] == ["gh", "api"] and any("pulls?head=" in p for p in cmd):
            idx = state["rest_idx"]
            state["rest_idx"] += 1
            page = rest_iter[idx] if idx < len(rest_iter) else []
            return _cp(json.dumps(page))
        # gh pr view <n> --json ... → full contract fields
        if cmd[:3] == ["gh", "pr", "view"]:
            number = int(cmd[3])
            entry = pr_views.get(number)
            if isinstance(entry, tuple):
                rc, out = entry
                return _cp(out, returncode=rc)
            return _cp(json.dumps(entry))
        raise AssertionError(f"unexpected gh invocation: {cmd}")

    monkeypatch.setattr(github_api, "_run", fake_run)
    return state


def _full_pr(number, head="fix/thing", oid="deadbeef", owner=None, **extra):
    pr = {
        "number": number,
        "title": f"PR {number}",
        "body": "## Root cause\nx",
        "url": f"https://x/pull/{number}",
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
        "headRefOid": oid,
        "headRefName": head,
        "baseRefName": "main",
    }
    if owner is not None:
        pr["headRepositoryOwner"] = {"login": owner}
    pr.update(extra)
    return pr


def test_find_pr_by_head_returns_full_open_pr(monkeypatch):
    state = _install_gh_fake(
        monkeypatch,
        rest_pages=[[{"number": 7}]],
        pr_views={7: _full_pr(7, head="fix/thing", oid="deadbeef")},
    )
    pr = github_api.find_pr_by_head("fix/thing", "open", ".")
    assert pr is not None
    assert pr["number"] == 7
    assert pr["body"] == "## Root cause\nx"
    assert pr["baseRefName"] == "main"
    # the head is resolved via an EXACT REST query, not a prefix `gh pr list`.
    rest_calls = [c for c in state["calls"] if c[:2] == ["gh", "api"]]
    assert rest_calls, "expected an exact-head REST query"
    # owner:branch separator colon is preserved; the branch's `/` is percent-encoded.
    assert any("head=acme:fix%2Fthing" in p for c in rest_calls for p in c)


def test_find_pr_by_head_no_match_returns_none(monkeypatch):
    _install_gh_fake(monkeypatch, rest_pages=[[]], pr_views={})
    assert github_api.find_pr_by_head("fix/none", "open", ".") is None


def test_find_pr_by_head_gh_failure_returns_none(monkeypatch):
    # REST query itself fails → fail open.
    monkeypatch.setattr(github_api, "_run", lambda *a, **k: _cp("", returncode=1))
    assert github_api.find_pr_by_head("fix/x", "open", ".") is None


def test_find_pr_by_head_pr_view_failure_returns_none(monkeypatch):
    # REST resolves the number but the full-fields fetch fails → fail open
    # (distinct from a genuine no-match, which returns None via [] pages).
    _install_gh_fake(
        monkeypatch,
        rest_pages=[[{"number": 7}]],
        pr_views={7: (1, "")},
    )
    assert github_api.find_pr_by_head("fix/thing", "open", ".") is None


def test_find_pr_by_head_empty_branch_returns_none(monkeypatch):
    called = {"n": 0}

    def fake_run(*a, **k):
        called["n"] += 1
        return _cp("[]")

    monkeypatch.setattr(github_api, "_run", fake_run)
    assert github_api.find_pr_by_head("", "open", ".") is None
    assert called["n"] == 0


def test_find_pr_by_head_oid_filter_matches(monkeypatch):
    _install_gh_fake(
        monkeypatch,
        rest_pages=[[{"number": 1}, {"number": 2}]],
        pr_views={
            1: _full_pr(1, head="b", oid="aaa"),
            2: _full_pr(2, head="b", oid="bbb"),
        },
    )
    pr = github_api.find_pr_by_head("b", "merged", ".", head_oid="bbb")
    assert pr is not None and pr["number"] == 2


def test_find_pr_by_head_oid_filter_no_match(monkeypatch):
    _install_gh_fake(
        monkeypatch,
        rest_pages=[[{"number": 1}]],
        pr_views={1: _full_pr(1, head="b", oid="aaa")},
    )
    assert github_api.find_pr_by_head("b", "merged", ".", head_oid="zzz") is None


def test_find_pr_by_head_strips_owner_qualifier(monkeypatch):
    state = _install_gh_fake(
        monkeypatch,
        rest_pages=[[{"number": 4}]],
        pr_views={4: _full_pr(4, head="feature", oid="y", owner="alice")},
    )
    pr = github_api.find_pr_by_head("alice:feature", "open", ".")
    assert pr is not None and pr["number"] == 4
    # the owner-qualified head is sent to the EXACT REST query as `alice:feature`,
    # and no repo-owner resolution is needed (the owner came from the caller).
    rest_calls = [c for c in state["calls"] if c[:2] == ["gh", "api"]]
    assert any("head=alice%3Afeature" in p or "head=alice:feature" in p
               for c in rest_calls for p in c)
    assert not any(c[:3] == ["gh", "repo", "view"] for c in state["calls"])


def test_find_pr_by_head_merged_maps_to_closed_state(monkeypatch):
    # REST has no "merged" state — a merged PR is closed. The query must request
    # state=closed (not the literal "merged") so the merged PR is found.
    state = _install_gh_fake(
        monkeypatch,
        rest_pages=[[]],
        pr_views={},
    )
    github_api.find_pr_by_head("b", "merged", ".")
    rest_calls = [c for c in state["calls"] if c[:2] == ["gh", "api"]]
    assert rest_calls
    assert any("state=closed" in p for c in rest_calls for p in c)
    assert not any("state=merged" in p for c in rest_calls for p in c)


def test_find_pr_by_head_exact_match_independent_of_prefix_volume(monkeypatch):
    # REGRESSION: previously `gh pr list --head fix` was a *prefix* filter capped
    # at 30 results; if >30 PRs began with `fix` and the exact `fix` PR sorted
    # past the first page, it was silently dropped → None → the gate falsely
    # blocked "No open PR". The exact REST query returns ONLY the exact-head PR
    # regardless of how many prefix-matches exist, so a single page with just the
    # exact PR is what the server returns.
    state = _install_gh_fake(
        monkeypatch,
        # The server-side exact-head filter returns ONLY #22 (the exact `fix`),
        # never the 50 `fix-NNN` prefix siblings that drowned it before.
        rest_pages=[[{"number": 22}]],
        pr_views={22: _full_pr(22, head="fix", oid="b")},
    )
    pr = github_api.find_pr_by_head("fix", "open", ".")
    assert pr is not None and pr["number"] == 22
    # And the query was indeed the exact-head form, not a prefix `gh pr list`.
    assert not any(c[:3] == ["gh", "pr", "list"] for c in state["calls"])


def test_find_pr_by_head_paginates_rest(monkeypatch):
    # Defensive pagination: if a full page (100) of matches comes back, a second
    # page is fetched. The exact PR can live on the second page and must be found.
    full_page = [{"number": n} for n in range(1, 101)]  # exactly per_page=100
    second = [{"number": 777}]
    state = _install_gh_fake(
        monkeypatch,
        rest_pages=[full_page, second],
        pr_views={
            **{n: _full_pr(n, head="other", oid=str(n)) for n in range(1, 101)},
            777: _full_pr(777, head="fix", oid="z"),
        },
    )
    pr = github_api.find_pr_by_head("fix", "open", ".")
    assert pr is not None and pr["number"] == 777
    page_params = [p for c in state["calls"] if c[:2] == ["gh", "api"]
                   for p in c if "page=" in p]
    assert any("page=2" in p for p in page_params)


def test_find_pr_by_head_fork_owner_match(monkeypatch):
    # An owner-qualified head (`alice:feature`) resolves alice's PR via the exact
    # REST head spec `alice:feature`; the headRepositoryOwner is re-verified.
    state = _install_gh_fake(
        monkeypatch,
        rest_pages=[[{"number": 2}]],
        pr_views={2: _full_pr(2, head="feature", oid="b", owner="alice")},
    )
    pr = github_api.find_pr_by_head("alice:feature", "open", ".")
    assert pr is not None and pr["number"] == 2
    rest_calls = [c for c in state["calls"] if c[:2] == ["gh", "api"]]
    assert any("head=alice%3Afeature" in p or "head=alice:feature" in p
               for c in rest_calls for p in c)


def test_find_pr_by_head_fork_owner_mismatch_returns_none(monkeypatch):
    # The REST exact query is for `alice:feature`; if the only PR returned is
    # actually bob's (defensive re-verify), it is rejected → None.
    _install_gh_fake(
        monkeypatch,
        rest_pages=[[{"number": 1}]],
        pr_views={1: _full_pr(1, head="feature", oid="a", owner="bob")},
    )
    assert github_api.find_pr_by_head("alice:feature", "open", ".") is None


def test_find_pr_by_head_repo_resolution_failure_returns_none(monkeypatch):
    # Unqualified head needs the repo owner for the REST `head=owner:branch`
    # spec; if `gh repo view` fails, fail open rather than guessing.
    monkeypatch.setattr(github_api, "get_repo_info", lambda cwd=None: ("", ""))
    monkeypatch.setattr(
        github_api, "_run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not reach gh api")),
    )
    assert github_api.find_pr_by_head("fix/thing", "open", ".") is None


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
