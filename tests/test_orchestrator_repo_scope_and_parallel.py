"""Repo-scoped worktree resolution (BOU-1720) + parallel per-PR enrichment
(BOU-1637 #4).

Item A — ``find_worktree_for_branch(branch, root=...)`` must restrict the
worktree search to the given repo root so a same-named branch in a sibling repo
can't resolve a multi-repo PR to the wrong checkout. The no-scope call must
behave exactly as before.

Item B — the dashboard poll enriches every open PR with several independent
``github_api.*`` calls; those run CONCURRENTLY (``asyncio.gather``) with a modest
concurrency bound and per-PR error isolation: one PR's failed enrichment must not
drop the others.

These stub the ``github_api.*`` / ``discover_worktrees`` boundary like the
existing orchestrator tests and drive ``Orchestrator.refresh_prs`` directly.
"""

from __future__ import annotations

import asyncio

import pytest

from agentic_pr_dash import github_api, orchestrator, worktrees
from agentic_pr_dash.models import PRStatus


ANCHOR_ROOT = "/repos/anchor"
SIBLING_ROOT = "/repos/sibling"


def _raw_pr(number: int, repo: str, *, branch: str = "feature/x") -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "headRefName": branch,
        "baseRefName": "main",
        "url": f"https://github.com/{repo}/pull/{number}",
        "isDraft": False,
        "reviewDecision": "",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "labels": [],
        "createdAt": "2026-06-11T12:00:00Z",
    }


# --------------------------------------------------------------------------- #
# Item A — BOU-1720: repo-scoped worktree resolution
# --------------------------------------------------------------------------- #


def test_find_worktree_for_branch_scopes_to_given_root(monkeypatch):
    """Two repos each have a worktree on ``feature/x``. Scoping to a root resolves
    to THAT root's worktree, never the sibling's same-named branch."""
    pools = {
        ANCHOR_ROOT: [{"branch": "feature/x", "path": "/wt/anchor/feature-x"}],
        SIBLING_ROOT: [{"branch": "feature/x", "path": "/wt/sibling/feature-x"}],
    }

    def fake_discover(root=None):
        # root=None would be the legacy process-cwd pool; scoped calls pick a repo.
        return pools.get(root, [])

    monkeypatch.setattr(worktrees, "discover_worktrees", fake_discover)

    assert (
        worktrees.find_worktree_for_branch("feature/x", root=ANCHOR_ROOT)
        == "/wt/anchor/feature-x"
    )
    assert (
        worktrees.find_worktree_for_branch("feature/x", root=SIBLING_ROOT)
        == "/wt/sibling/feature-x"
    )


def test_find_worktree_for_branch_no_scope_is_legacy_behavior(monkeypatch):
    """The no-scope call passes ``root=None`` through to ``discover_worktrees`` —
    i.e. the legacy process-cwd pool, unchanged."""
    seen_roots: list[str | None] = []

    def fake_discover(root=None):
        seen_roots.append(root)
        return [{"branch": "feature/x", "path": "/wt/legacy/feature-x"}]

    monkeypatch.setattr(worktrees, "discover_worktrees", fake_discover)

    # Positional, single-arg call — exactly how legacy callers invoked it.
    assert worktrees.find_worktree_for_branch("feature/x") == "/wt/legacy/feature-x"
    assert seen_roots == [None]


def test_discover_worktrees_runs_git_in_root_cwd(monkeypatch):
    """``discover_worktrees(root)`` must run ``git worktree list`` with ``root`` as
    the cwd so it enumerates THAT repo's pool, not the process cwd's."""
    captured: dict = {}

    class _Result:
        returncode = 0
        stdout = "worktree /wt/x\nbranch refs/heads/feature/x\n"

    def fake_run(cmd, timeout_s=10, cwd=None):
        captured["cwd"] = cwd
        return _Result()

    monkeypatch.setattr(worktrees, "_run", fake_run)
    # Avoid touching the filesystem for .env enrichment.
    monkeypatch.setattr(worktrees, "_parse_env", lambda p: {})

    worktrees.discover_worktrees(root=ANCHOR_ROOT)
    assert captured["cwd"] == ANCHOR_ROOT


def test_refresh_threads_pr_repo_root_into_worktree_resolution(monkeypatch):
    """A PR from repo A resolves to repo A's worktree even though repo B has a
    worktree on the SAME branch name — the poll threads each PR's root into
    ``find_worktree_for_branch``."""
    monkeypatch.setattr(
        orchestrator, "_resolve_maintenance_roots", lambda cwd: [ANCHOR_ROOT, SIBLING_ROOT]
    )
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(github_api, "get_mergeability", lambda num, cwd=None: ("CLEAN", "MERGEABLE"))
    monkeypatch.setattr(
        github_api, "get_latest_commit", lambda num, cwd=None: ("sha", "2026-06-11T12:00:00Z")
    )
    monkeypatch.setattr(github_api, "get_ci_checks", lambda num, cwd=None: [])
    monkeypatch.setattr(
        github_api, "get_unaddressed_comments", lambda num, latest, cwd=None: []
    )

    def fake_list_open_prs(cwd=None):
        if cwd == ANCHOR_ROOT:
            return [_raw_pr(10, "org/anchor-repo", branch="feature/x")]
        if cwd == SIBLING_ROOT:
            return [_raw_pr(20, "org/sibling-repo", branch="feature/x")]
        return []

    monkeypatch.setattr(github_api, "list_open_prs", fake_list_open_prs)

    # The worktree resolver is repo-scoped: the same branch maps to a DIFFERENT
    # path per root. If the poll failed to thread the root, both PRs would
    # collapse onto one repo's worktree.
    def fake_find(branch, root=None):
        return f"{root}/wt/{branch}"

    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", fake_find)

    orch = orchestrator.Orchestrator(repo_cwd=ANCHOR_ROOT)
    prs = asyncio.run(orch.refresh_prs())

    by_repo = {pr.repo: pr.worktree_path for pr in prs}
    assert by_repo["org/anchor-repo"] == f"{ANCHOR_ROOT}/wt/feature/x"
    assert by_repo["org/sibling-repo"] == f"{SIBLING_ROOT}/wt/feature/x"
    # Crucially: the two PRs do NOT share a worktree path.
    assert by_repo["org/anchor-repo"] != by_repo["org/sibling-repo"]


# --------------------------------------------------------------------------- #
# Item B — BOU-1637 #4: parallel per-PR enrichment
# --------------------------------------------------------------------------- #


@pytest.fixture
def _base_stubs(monkeypatch):
    monkeypatch.setattr(orchestrator, "_resolve_maintenance_roots", lambda cwd: [ANCHOR_ROOT])
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(github_api, "get_mergeability", lambda num, cwd=None: ("CLEAN", "MERGEABLE"))
    monkeypatch.setattr(github_api, "get_ci_checks", lambda num, cwd=None: [])
    monkeypatch.setattr(
        github_api, "get_unaddressed_comments", lambda num, latest, cwd=None: []
    )
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda branch, root=None: None)


def test_enrichment_runs_concurrently(monkeypatch, _base_stubs):
    """N PRs enrich concurrently, not as N serialized awaits. We prove it by
    measuring max simultaneous in-flight ``get_latest_commit`` calls: a sequential
    loop peaks at 1; ``asyncio.gather`` peaks at >1 (bounded by the semaphore)."""
    n_prs = 5
    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: [_raw_pr(i, "org/anchor-repo", branch=f"feature/{i}") for i in range(n_prs)],
    )

    state = {"current": 0, "peak": 0}

    async def fake_to_thread(fn, *args, **kwargs):
        # Only the per-PR commit fetch participates in the in-flight measurement.
        if fn is github_api.get_latest_commit:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
            await asyncio.sleep(0)  # yield so siblings can interleave
            state["current"] -= 1
            return ("sha", "2026-06-11T12:00:00Z")
        return fn(*args, **kwargs)

    monkeypatch.setattr(orchestrator.asyncio, "to_thread", fake_to_thread)

    orch = orchestrator.Orchestrator(repo_cwd=ANCHOR_ROOT)
    prs = asyncio.run(orch.refresh_prs())

    assert len(prs) == n_prs
    # Sequential enrichment would never exceed 1 concurrent commit fetch.
    assert state["peak"] > 1
    # ...and never exceeds the configured concurrency bound.
    assert state["peak"] <= orchestrator.ENRICHMENT_CONCURRENCY


def test_one_pr_enrichment_error_does_not_drop_the_others(monkeypatch, _base_stubs):
    """One PR's enrichment raising must be isolated — the others still enrich and
    remain tracked."""
    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: [
            _raw_pr(1, "org/anchor-repo", branch="feature/1"),
            _raw_pr(2, "org/anchor-repo", branch="feature/2"),
            _raw_pr(3, "org/anchor-repo", branch="feature/3"),
        ],
    )

    def fake_latest_commit(num, cwd=None):
        if num == 2:
            raise RuntimeError("boom on PR 2")
        return ("sha", "2026-06-11T12:00:00Z")

    monkeypatch.setattr(github_api, "get_latest_commit", fake_latest_commit)

    orch = orchestrator.Orchestrator(repo_cwd=ANCHOR_ROOT)
    prs = asyncio.run(orch.refresh_prs())  # must not raise

    nums = {pr.number for pr in prs}
    # PR 2 failed mid-enrichment but is still TRACKED (created up front); the
    # critical guarantee is that PRs 1 and 3 were NOT dropped by its failure.
    assert {1, 2, 3} == nums
    # PRs 1 and 3 finished enrichment (status computed → CLEAN for these stubs).
    by_num = {pr.number: pr for pr in prs}
    assert by_num[1].status == PRStatus.CLEAN
    assert by_num[3].status == PRStatus.CLEAN
    # The failure was surfaced as an error event, not swallowed silently.
    assert any(
        "Enrichment failed for PR #2" in e.message for e in orch.events
    )
