"""Multi-repo dashboard coverage (BOU-1598).

The PR dashboard must cover ``[anchor] + maintenance_repo_roots`` — the same
root list the maintenance loop honors — aggregating PRs from every repo, tagged
by repo so same-number PRs across repos don't collide, with per-repo poll-failure
isolation. Zero extra roots reproduces today's single-repo behavior exactly.

These stub the ``github_api.*`` / ``list_open_prs`` boundary like the existing
orchestrator tests and drive ``Orchestrator.refresh_prs`` directly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentic_pr_dash import github_api, orchestrator
from agentic_pr_dash.models import PRStatus


ANCHOR_ROOT = "/repos/anchor"
SIBLING_ROOT = "/repos/sibling"


def _raw_pr(number: int, repo: str, *, title: str = "PR", branch: str | None = None) -> dict:
    return {
        "number": number,
        "title": title,
        "headRefName": branch or f"feature/{number}",
        "baseRefName": "main",
        "url": f"https://github.com/{repo}/pull/{number}",
        "isDraft": False,
        "reviewDecision": "",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "labels": [],
        "createdAt": "2026-06-11T12:00:00Z",
    }


@pytest.fixture(autouse=True)
def _stub_enrichment(monkeypatch):
    """Neutralize per-PR enrichment + worktree discovery so tests focus on the
    multi-repo aggregation/pruning behavior, not the per-PR REST calls."""
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(github_api, "get_mergeability", lambda num, cwd=None: ("CLEAN", "MERGEABLE"))
    monkeypatch.setattr(
        github_api, "get_latest_commit", lambda num, cwd=None: ("sha", "2026-06-11T12:00:00Z")
    )
    monkeypatch.setattr(github_api, "get_ci_checks", lambda num, cwd=None: [])
    monkeypatch.setattr(
        github_api,
        "get_unaddressed_comments",
        lambda num, latest_commit_date, cwd=None: [],
    )
    monkeypatch.setattr(
        github_api,
        "scan_review_threads_observation",
        lambda num, latest_commit_date, cwd=None: (
            github_api.ObservationReadResult.observed(([], []))
        ),
    )
    # No worktree → no auto-dispatch; keeps the poll a pure read.
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda branch, root=None: None)


def test_polls_anchor_and_sibling_roots_aggregated_and_tagged(monkeypatch):
    """Anchor + one sibling root → BOTH polled; aggregated set tagged by repo."""
    monkeypatch.setattr(
        orchestrator, "_resolve_maintenance_roots", lambda cwd: [ANCHOR_ROOT, SIBLING_ROOT]
    )

    polled_cwds: list[str | None] = []

    def fake_list_open_prs(cwd=None):
        polled_cwds.append(cwd)
        if cwd == ANCHOR_ROOT:
            return [_raw_pr(10, "org/anchor-repo")]
        if cwd == SIBLING_ROOT:
            return [_raw_pr(20, "org/sibling-repo")]
        return []

    monkeypatch.setattr(github_api, "list_open_prs", fake_list_open_prs)

    orch = orchestrator.Orchestrator(repo_cwd=ANCHOR_ROOT)
    prs = asyncio.run(orch.refresh_prs())

    # Both roots polled.
    assert ANCHOR_ROOT in polled_cwds
    assert SIBLING_ROOT in polled_cwds

    by_key = {(pr.repo, pr.number) for pr in prs}
    assert ("org/anchor-repo", 10) in by_key
    assert ("org/sibling-repo", 20) in by_key
    # Each PR carries its repo tag.
    assert {pr.repo for pr in prs} == {"org/anchor-repo", "org/sibling-repo"}


def test_same_pr_number_in_two_repos_is_distinct(monkeypatch):
    """PR #5 in two repos → two distinct dashboard entries, no collision."""
    monkeypatch.setattr(
        orchestrator, "_resolve_maintenance_roots", lambda cwd: [ANCHOR_ROOT, SIBLING_ROOT]
    )

    def fake_list_open_prs(cwd=None):
        if cwd == ANCHOR_ROOT:
            return [_raw_pr(5, "org/anchor-repo", title="Anchor five")]
        if cwd == SIBLING_ROOT:
            return [_raw_pr(5, "org/sibling-repo", title="Sibling five")]
        return []

    monkeypatch.setattr(github_api, "list_open_prs", fake_list_open_prs)

    orch = orchestrator.Orchestrator(repo_cwd=ANCHOR_ROOT)
    prs = asyncio.run(orch.refresh_prs())

    # Two distinct entries despite the shared PR number.
    assert len(orch.prs) == 2
    titles = {(pr.repo, pr.title) for pr in prs}
    assert ("org/anchor-repo", "Anchor five") in titles
    assert ("org/sibling-repo", "Sibling five") in titles
    # Repo-scoped lookup disambiguates.
    assert orch.get_pr(5, repo="org/anchor-repo").title == "Anchor five"
    assert orch.get_pr(5, repo="org/sibling-repo").title == "Sibling five"


def test_one_repo_poll_failure_does_not_drop_the_other_repos_prs(monkeypatch):
    """A repo whose ``list_open_prs`` returns None (gh error) must not drop or
    prune another repo's already-tracked PRs."""
    monkeypatch.setattr(
        orchestrator, "_resolve_maintenance_roots", lambda cwd: [ANCHOR_ROOT, SIBLING_ROOT]
    )

    sibling_healthy = {"ok": True}

    def fake_list_open_prs(cwd=None):
        if cwd == ANCHOR_ROOT:
            return [_raw_pr(10, "org/anchor-repo")]
        if cwd == SIBLING_ROOT:
            return [_raw_pr(20, "org/sibling-repo")] if sibling_healthy["ok"] else None
        return []

    monkeypatch.setattr(github_api, "list_open_prs", fake_list_open_prs)

    orch = orchestrator.Orchestrator(repo_cwd=ANCHOR_ROOT)

    # First poll: both repos healthy.
    asyncio.run(orch.refresh_prs())
    assert ("org/anchor-repo", 10) in orch.prs
    assert ("org/sibling-repo", 20) in orch.prs

    # Sibling now errors (None). Anchor PR must survive; sibling PR must NOT be
    # pruned (None ≠ "no open PRs").
    sibling_healthy["ok"] = False
    prs = asyncio.run(orch.refresh_prs())

    by_key = {(pr.repo, pr.number) for pr in prs}
    assert ("org/anchor-repo", 10) in by_key
    assert ("org/sibling-repo", 20) in by_key


def test_one_repo_raising_does_not_sink_other_repos(monkeypatch):
    """An exception while enriching one repo must be isolated; the other repo's
    PRs still aggregate."""
    monkeypatch.setattr(
        orchestrator, "_resolve_maintenance_roots", lambda cwd: [ANCHOR_ROOT, SIBLING_ROOT]
    )

    def fake_list_open_prs(cwd=None):
        if cwd == SIBLING_ROOT:
            raise RuntimeError("boom")
        if cwd == ANCHOR_ROOT:
            return [_raw_pr(10, "org/anchor-repo")]
        return []

    monkeypatch.setattr(github_api, "list_open_prs", fake_list_open_prs)

    orch = orchestrator.Orchestrator(repo_cwd=ANCHOR_ROOT)
    prs = asyncio.run(orch.refresh_prs())  # must not raise

    by_key = {(pr.repo, pr.number) for pr in prs}
    assert ("org/anchor-repo", 10) in by_key


def test_zero_extra_roots_is_single_repo_behavior(monkeypatch):
    """No configured roots → exactly today's single-repo poll (one cwd, one
    repo's PRs)."""
    # _resolve_maintenance_roots returns just the anchor (no extra roots).
    monkeypatch.setattr(orchestrator, "_resolve_maintenance_roots", lambda cwd: [ANCHOR_ROOT])

    polled_cwds: list[str | None] = []

    def fake_list_open_prs(cwd=None):
        polled_cwds.append(cwd)
        return [_raw_pr(10, "org/anchor-repo")]

    monkeypatch.setattr(github_api, "list_open_prs", fake_list_open_prs)

    orch = orchestrator.Orchestrator(repo_cwd=ANCHOR_ROOT)
    prs = asyncio.run(orch.refresh_prs())

    assert polled_cwds == [ANCHOR_ROOT]
    assert len(prs) == 1
    assert prs[0].number == 10
    # Number-only lookup (the dashboard's human-dispatch path) still resolves.
    assert orch.get_pr(10) is prs[0]


def test_no_repo_cwd_preserves_legacy_none_cwd_poll(monkeypatch):
    """Orchestrator with no repo_cwd polls a single None cwd (legacy default)."""
    called = {"resolve": 0}

    def _resolve(cwd):  # should NOT be called when repo_cwd is None
        called["resolve"] += 1
        return [cwd]

    monkeypatch.setattr(orchestrator, "_resolve_maintenance_roots", _resolve)

    polled_cwds: list[str | None] = []

    def fake_list_open_prs(cwd=None):
        polled_cwds.append(cwd)
        return [_raw_pr(10, "org/anchor-repo")]

    monkeypatch.setattr(github_api, "list_open_prs", fake_list_open_prs)

    orch = orchestrator.Orchestrator(repo_cwd=None)
    asyncio.run(orch.refresh_prs())

    assert polled_cwds == [None]
    assert called["resolve"] == 0
