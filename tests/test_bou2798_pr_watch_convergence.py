"""Regression coverage for BOU-2798 PR-watch ownership convergence."""

from __future__ import annotations

from pathlib import Path

from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash import github_api
from agentic_pr_dash._maintenance import _common, ownership_resolution, pr_state
from agentic_pr_dash._maintenance.stop_gate import _effective_pr_pairs


def test_waiter_pairs_rebind_to_current_branch_pr_after_stale_marker(
    monkeypatch, tmp_path: Path
):
    """A closed PR A must not keep the waiter bound after the worktree moves to B."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    monkeypatch.setattr(
        mc, "_owned_open_pr_pairs", lambda owned: [(str(worktree), 3017)]
    )
    monkeypatch.setattr(_common, "_current_branch", lambda cwd: "feature-b")
    monkeypatch.setattr(ownership_resolution, "_current_head", lambda cwd: "head-b")
    monkeypatch.setattr(
        pr_state,
        "_resolve_pr_entry_for_branch",
        lambda cwd, branch, **kwargs: {
            "number": 3062,
            "headRefName": branch,
            "headRefOid": "head-b",
            "isDraft": False,
        },
    )

    assert mc._owned_pr_pairs_for_await([str(worktree)]) == [
        (str(worktree), 3062)
    ]


def test_current_branch_resolution_rejects_closed_cached_pr_and_rebinds(
    monkeypatch, tmp_path: Path
):
    """A warm GraphQL snapshot must not keep a closed PR A after PR B opens."""
    monkeypatch.setattr(
        github_api,
        "peek_pr_snapshot",
        lambda cwd: [{
            "number": 3017,
            "headRefName": "feature-b",
            "headRefOid": "head-a",
            "isDraft": False,
        }],
    )
    monkeypatch.setattr(
        github_api,
        "_rest_pr_payload",
        lambda number, cwd=None: {
            "number": number,
            "state": "closed",
            "headRefName": "feature-b",
        },
    )
    monkeypatch.setattr(
        pr_state,
        "_rest_fallback_entry_for_branch",
        lambda branch, cwd, *, force=False: {
            "number": 3062,
            "state": "open",
            "headRefName": branch,
            "headRefOid": "head-b",
            "isDraft": False,
        },
    )

    result = pr_state._resolve_pr_entry_for_branch(
        str(tmp_path), "feature-b", validate_snapshot_state=True
    )

    assert result["number"] == 3062
    assert result["headRefOid"] == "head-b"


def test_rate_limit_fallback_refuses_foreign_author(monkeypatch, tmp_path: Path):
    """REST fallback must not turn a shared branch's foreign PR into ownership."""
    monkeypatch.setattr(github_api, "peek_pr_snapshot", lambda cwd: None)
    monkeypatch.setattr(pr_state, "_gh_pr_list_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pr_state, "_list_failure_is_rate_limited", lambda: True
    )
    monkeypatch.setattr(github_api, "_rest_repo_owner", lambda cwd: "owner")
    monkeypatch.setattr(
        github_api, "_exact_head_pr_numbers", lambda *args, **kwargs: [77]
    )
    monkeypatch.setattr(
        github_api,
        "_rest_pr_payload",
        lambda number, cwd=None: {
            "number": number,
            "state": "open",
            "headRefName": "shared-branch",
            "author": {"login": "another-maintainer"},
        },
    )
    monkeypatch.setattr(github_api, "_rest_viewer_login", lambda cwd: "tracked-owner")

    result = pr_state._resolve_pr_entry_for_branch(str(tmp_path), "shared-branch")

    assert result is pr_state._GH_UNAVAILABLE


def test_gate_current_resolution_does_not_fall_back_to_stale_marker(tmp_path: Path):
    worktree = str(tmp_path / "worktree")

    assert _effective_pr_pairs(
        [worktree],
        {worktree: 3062},
        current_resolved={worktree},
    ) == [(worktree, 3062)]
    assert _effective_pr_pairs(
        [worktree],
        {},
        current_resolved={worktree},
    ) == []
