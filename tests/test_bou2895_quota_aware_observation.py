"""BOU-2895 regression coverage for quota-aware PR observation."""

from __future__ import annotations

import pytest

from agentic_pr_dash import github_api, orchestrator


@pytest.mark.asyncio
async def test_unchanged_terminal_pr_reuses_batch_observation(monkeypatch):
    anchor_root = "/repos/anchor"
    raw_pr = {
        "number": 2895,
        "title": "Quota-aware observation",
        "headRefName": "feature/quota-aware-observation",
        "headRefOid": "stable-head-sha",
        "baseRefName": "main",
        "url": "https://github.com/org/anchor-repo/pull/2895",
        "isDraft": False,
        "reviewDecision": "",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "labels": [],
        "createdAt": "2026-06-11T12:00:00Z",
    }
    observation_calls = {
        "batch": 0,
        "latest_commit": 0,
        "ci_checks": 0,
        "review_thread_scan": 0,
    }

    monkeypatch.setattr(
        orchestrator, "_resolve_maintenance_roots", lambda cwd: [anchor_root]
    )
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [raw_pr])
    monkeypatch.setattr(
        github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None
    )
    monkeypatch.setattr(
        github_api, "get_repo_info", lambda cwd=None: ("org", "anchor-repo")
    )
    monkeypatch.setattr(
        github_api, "get_mergeability", lambda num, cwd=None: ("CLEAN", "MERGEABLE")
    )

    def fake_latest_commit(num, cwd=None):
        observation_calls["latest_commit"] += 1
        return "stable-head-sha", "2026-06-11T12:00:00Z"

    def fake_ci_checks(num, cwd=None):
        observation_calls["ci_checks"] += 1
        return []

    def fake_scan_review_threads(num, latest_commit_date, cwd=None):
        observation_calls["review_thread_scan"] += 1
        return [], []

    monkeypatch.setattr(github_api, "get_latest_commit", fake_latest_commit)
    monkeypatch.setattr(github_api, "get_ci_checks", fake_ci_checks)
    monkeypatch.setattr(github_api, "scan_review_threads", fake_scan_review_threads)
    monkeypatch.setattr(
        orchestrator, "find_worktree_for_branch", lambda branch, root=None: None
    )

    def fake_batch(owner, repo, numbers, cwd=None):
        observation_calls["batch"] += 1
        return {
            number: {
                "latest_commit": ("stable-head-sha", "2026-06-11T12:00:00Z"),
                "ci_checks": [],
                "threads": [],
                "required_pending": False,
                "head_sha": "stable-head-sha",
                "merge_state": "CLEAN",
                "mergeable": "MERGEABLE",
                "review_decision": "none",
            }
            for number in numbers
        }

    monkeypatch.setattr(github_api, "batch_fetch_pr_review_and_ci", fake_batch)

    orch = orchestrator.Orchestrator(repo_cwd=anchor_root)
    await orch.refresh_prs()
    await orch.refresh_prs()

    assert observation_calls == {
        "batch": 1,
        "latest_commit": 1,
        "ci_checks": 1,
        "review_thread_scan": 1,
    }
