"""Regression coverage for BOU-2798 PR-watch ownership convergence."""

from __future__ import annotations

import time
from pathlib import Path

from agentic_pr_dash import github_api, session_ledger
from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash._maintenance import (
    _common,
    markers,
    ownership_resolution,
    pr_state,
    worktrees,
)
from agentic_pr_dash._maintenance.ownership_resolution import CurrentPRResolution
from agentic_pr_dash._maintenance.stop_gate import (
    _cached_clean_binding_matches,
    _effective_pr_pairs,
    _fence_current_pr_rebindings,
)


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


def test_branch_resolution_rejects_ambiguous_same_branch_without_head_match(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(github_api, "peek_pr_snapshot", lambda cwd: None)
    monkeypatch.setattr(
        pr_state,
        "_gh_pr_list_json",
        lambda *args, **kwargs: [
            {"number": 3017, "headRefName": "shared", "headRefOid": "head-a"},
            {"number": 3062, "headRefName": "shared", "headRefOid": "head-b"},
        ],
    )

    result = pr_state._resolve_pr_entry_for_branch(
        str(tmp_path), "shared", head_oid="unpushed-local-head"
    )

    assert result is pr_state._GH_UNAVAILABLE


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


def test_strict_current_resolution_revalidates_a_warm_snapshot_miss(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(github_api, "peek_pr_snapshot", lambda cwd: [])
    monkeypatch.setattr(
        pr_state,
        "_rest_fallback_entry_for_branch",
        lambda branch, cwd, **kwargs: {
            "number": 3062,
            "state": "open",
            "headRefName": branch,
        },
    )

    result = pr_state._resolve_pr_entry_for_branch(
        str(tmp_path), "feature-b", validate_snapshot_state=True
    )

    assert result["number"] == 3062


def test_strict_replacement_miss_is_unknown_for_fork_backed_heads(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(
        github_api,
        "peek_pr_snapshot",
        lambda cwd: [{"number": 3017, "headRefName": "feature-b"}],
    )
    monkeypatch.setattr(
        github_api,
        "_rest_pr_payload",
        lambda number, cwd=None, **kwargs: {
            "number": number,
            "state": "closed",
            "headRefName": "feature-b",
        },
    )
    monkeypatch.setattr(
        pr_state, "_rest_fallback_entry_for_branch", lambda *args, **kwargs: None
    )

    result = pr_state._resolve_pr_entry_for_branch(
        str(tmp_path), "feature-b", validate_snapshot_state=True
    )

    assert result is pr_state._GH_UNAVAILABLE


def test_cold_current_pr_resolution_shares_the_stop_gate_deadline(
    monkeypatch, tmp_path: Path
):
    deadline = time.monotonic() + 1
    observed: list[tuple[str, float | None]] = []

    monkeypatch.setattr(github_api, "peek_pr_snapshot", lambda cwd: None)

    def _list(cwd, args, fields, *, timeout=15, deadline=None):
        observed.append(("list", deadline))

    def _rest(branch, cwd, *, force=False, deadline=None):
        observed.append(("rest", deadline))

    monkeypatch.setattr(pr_state, "_gh_pr_list_json", _list)
    monkeypatch.setattr(pr_state, "_rest_fallback_entry_for_branch", _rest)

    result = pr_state._resolve_pr_entry_for_branch(
        str(tmp_path), "feature-b", validate_snapshot_state=True, deadline=deadline
    )

    assert result is pr_state._GH_UNAVAILABLE
    assert observed == [("list", deadline), ("rest", deadline)]


def test_failed_fenced_rebind_is_reported_unknown_and_not_acquired(tmp_path: Path):
    worktree = str(tmp_path / "worktree")
    binding = CurrentPRResolution(
        worktree,
        "feature-b",
        3062,
        resolved=True,
        stale_pr_number=3017,
    )

    rebound, conflicts = _fence_current_pr_rebindings(
        {worktree: binding},
        session_id="session-a",
        pid=123,
        provenance_for={worktree: "armed"},
        arm=lambda *args: False,
    )

    assert conflicts == [worktree]
    assert rebound[worktree].unknown is True
    assert rebound[worktree].pr_number is None
    assert rebound[worktree].stale_pr_number == 3017


def test_failed_claim_does_not_append_replacement_to_session_ledger(
    monkeypatch, tmp_path: Path
):
    appended: list[tuple] = []
    monkeypatch.setattr(markers, "marker_writes_enabled", lambda: False)
    monkeypatch.setattr(markers, "_current_branch", lambda cwd: "feature-b")
    monkeypatch.setattr(markers, "_repo_slug", lambda cwd: "owner/repo")
    monkeypatch.setattr(markers, "_dual_write_ownership_claim", lambda *a, **k: False)
    monkeypatch.setattr(
        "agentic_pr_dash.session_ledger.append",
        lambda *args, **kwargs: appended.append((args, kwargs)),
    )

    assert not markers._write_arm_marker(
        str(tmp_path), "session-a", 123, 3062
    )
    assert appended == []


def test_clean_cache_identity_includes_current_pr_and_branch(tmp_path: Path):
    worktree = str(tmp_path / "worktree")
    binding = CurrentPRResolution(
        worktree, "feature-b", 3062, head_sha="same-sha", resolved=True
    )
    cached = {
        "head_sha": "same-sha",
        "branch": "feature-a",
        "pr_number": 3017,
        "checked_at": 100,
        "code": 0,
    }

    assert not _cached_clean_binding_matches(
        cached, "same-sha", binding, now=101, interval=180
    )


def test_waiter_ci_probe_uses_strict_current_binding(monkeypatch, tmp_path: Path):
    worktree = str(tmp_path / "worktree")
    binding = CurrentPRResolution(
        worktree, "feature-b", 3062, resolved=True, is_draft=False
    )
    probed: list[tuple[int, str]] = []
    monkeypatch.setattr(
        github_api,
        "required_checks_pending",
        lambda pr, cwd: probed.append((pr, cwd)) or True,
    )
    monkeypatch.setattr(
        mc,
        "_collect_await_watch_pending",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("strict binding must avoid branch re-resolution")
        ),
    )

    assert mc._await_watch_pending_this_tick(
        [worktree], [], str(tmp_path), "session-a", bindings={worktree: binding}
    )
    assert probed == [(3062, worktree)]


def test_repointed_worktree_no_longer_hides_old_pr_from_detached_records(
    monkeypatch, tmp_path: Path
):
    entry = session_ledger.LedgerEntry(
        pr=3017,
        branch="feature-a",
        worktree=str(tmp_path),
        opened_at="",
        baseline_sha=None,
        repo="owner/repo",
    )
    owner = ownership_resolution.WorktreeOwnership(
        worktree=str(tmp_path),
        session_id="session-a",
        pr_number=3017,
        provenance="armed",
        marker_session_id="session-a",
        source="marker",
    )
    monkeypatch.setattr(ownership_resolution, "resolve_worktree", lambda *a, **k: owner)
    monkeypatch.setattr(worktrees, "_current_branch", lambda path: "feature-b")

    assert not worktrees._worktree_is_for_entry(str(tmp_path), entry)
