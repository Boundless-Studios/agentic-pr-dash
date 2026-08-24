"""Regression coverage for BOU-2798 PR-watch ownership convergence."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

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
    _binding_matches_live_checkout,
    _cached_clean_binding_matches,
    _durable_stop_gate_pid,
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


def test_rate_limit_fallback_rejects_multiple_author_matching_prs(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(pr_state, "_list_failure_is_rate_limited", lambda: True)
    monkeypatch.setattr(github_api, "_rest_repo_owner", lambda cwd: "owner")
    monkeypatch.setattr(
        github_api, "_exact_head_pr_numbers", lambda *args, **kwargs: [77, 78]
    )
    monkeypatch.setattr(
        github_api,
        "_rest_pr_payload",
        lambda number, cwd=None: {
            "number": number,
            "headRefName": "shared-branch",
            "author": {"login": "tracked-owner"},
        },
    )
    monkeypatch.setattr(github_api, "_rest_viewer_login", lambda cwd: "tracked-owner")

    assert (
        pr_state._rest_fallback_entry_for_branch("shared-branch", str(tmp_path))
        is None
    )


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


def test_cold_ambiguous_current_pr_resolution_stays_unknown(
    monkeypatch, tmp_path: Path
):
    """A cold strict-resolution failure must not fall back to stale ownership."""
    owner = SimpleNamespace(pr_number=3017, owned_by=lambda session_id: True)
    monkeypatch.setattr(
        ownership_resolution, "resolve_worktree", lambda *args, **kwargs: owner
    )
    monkeypatch.setattr(_common, "_current_branch", lambda cwd: "shared")
    monkeypatch.setattr(ownership_resolution, "_current_head", lambda cwd: "local")
    monkeypatch.setattr(
        pr_state,
        "_resolve_pr_entry_for_branch",
        lambda *args, **kwargs: pr_state._GH_UNAVAILABLE,
    )

    result = ownership_resolution.resolve_current_pr(
        str(tmp_path), session_id="session-a"
    )

    assert result.branch == "shared"
    assert result.pr_number is None
    assert result.stale_pr_number == 3017
    assert result.resolved is True
    assert result.unknown is True


def test_current_pr_resolution_preserves_local_checkout_sha(
    monkeypatch, tmp_path: Path
):
    owner = SimpleNamespace(pr_number=3017, owned_by=lambda session_id: True)
    monkeypatch.setattr(
        ownership_resolution, "resolve_worktree", lambda *args, **kwargs: owner
    )
    monkeypatch.setattr(_common, "_current_branch", lambda cwd: "feature-b")
    monkeypatch.setattr(
        ownership_resolution, "_current_head", lambda cwd: "local-head"
    )
    monkeypatch.setattr(github_api, "peek_pr_snapshot", lambda cwd: None)
    monkeypatch.setattr(
        pr_state,
        "_resolve_pr_entry_for_branch",
        lambda *args, **kwargs: {
            "number": 3062,
            "headRefName": "feature-b",
            "headRefOid": "remote-head",
            "isDraft": False,
        },
    )

    result = ownership_resolution.resolve_current_pr(
        str(tmp_path), session_id="session-a"
    )

    assert result.pr_number == 3062
    assert result.head_sha == "local-head"


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


def test_fenced_rebind_revalidates_checkout_before_acquiring(
    monkeypatch, tmp_path: Path
):
    worktree = str(tmp_path / "worktree")
    binding = CurrentPRResolution(
        worktree,
        "feature-b",
        3062,
        head_sha="head-b",
        resolved=True,
        stale_pr_number=3017,
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        "agentic_pr_dash._maintenance.stop_gate._current_branch",
        lambda cwd: "feature-c",
    )
    monkeypatch.setattr(
        "agentic_pr_dash._maintenance.stop_gate._local_head_sha",
        lambda cwd: "head-c",
    )

    rebound, conflicts = _fence_current_pr_rebindings(
        {worktree: binding},
        session_id="session-a",
        pid=123,
        provenance_for={worktree: "armed"},
        arm=lambda *args: calls.append(args) or True,
    )

    assert calls == []
    assert conflicts == [worktree]
    assert rebound[worktree].unknown is True
    assert rebound[worktree].pr_number is None


def test_fenced_rebind_passes_checkout_identity_to_arm(
    monkeypatch, tmp_path: Path
):
    worktree = str(tmp_path / "worktree")
    binding = CurrentPRResolution(
        worktree,
        "feature-b",
        3062,
        head_sha="head-b",
        resolved=True,
        stale_pr_number=3017,
    )
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "agentic_pr_dash._maintenance.stop_gate._current_branch",
        lambda cwd: "feature-b",
    )
    monkeypatch.setattr(
        "agentic_pr_dash._maintenance.stop_gate._local_head_sha",
        lambda cwd: "head-b",
    )

    rebound, conflicts = _fence_current_pr_rebindings(
        {worktree: binding},
        session_id="session-a",
        pid=123,
        provenance_for={worktree: "armed"},
        arm=lambda *args, **kwargs: calls.append((args, kwargs)) or True,
    )

    assert conflicts == []
    assert rebound[worktree] == binding
    assert calls == [
        (
            (worktree, "session-a", 123, 3062, "armed"),
            {"expected_branch": "feature-b", "expected_head_sha": "head-b"},
        )
    ]


def test_arm_rejects_mismatched_checkout_before_claim(
    monkeypatch, tmp_path: Path
):
    claimed: list[tuple] = []
    monkeypatch.setattr(markers, "marker_writes_enabled", lambda: False)
    monkeypatch.setattr(markers, "_current_branch", lambda cwd: "feature-c")
    monkeypatch.setattr(markers, "_current_head_sha", lambda cwd: "head-c")
    monkeypatch.setattr(
        markers,
        "_dual_write_ownership_claim",
        lambda *args, **kwargs: claimed.append((args, kwargs)) or True,
    )

    assert not markers._write_arm_marker(
        str(tmp_path),
        "session-a",
        123,
        3062,
        expected_branch="feature-b",
        expected_head_sha="head-b",
    )
    assert claimed == []


def test_fenced_rebind_does_not_acquire_draft_replacement(tmp_path: Path):
    worktree = str(tmp_path / "worktree")
    binding = CurrentPRResolution(
        worktree,
        "feature-b",
        3062,
        is_draft=True,
        resolved=True,
        stale_pr_number=3017,
    )
    calls: list[tuple] = []

    rebound, conflicts = _fence_current_pr_rebindings(
        {worktree: binding},
        session_id="session-a",
        pid=123,
        provenance_for={worktree: "armed"},
        arm=lambda *args: calls.append(args) or True,
    )

    assert conflicts == []
    assert calls == []
    assert rebound[worktree] == binding


def test_stop_gate_uses_durable_pid_when_cli_pid_is_absent(monkeypatch):
    monkeypatch.setattr(worktrees, "_resolve_owner_pid", lambda: 4242)

    assert _durable_stop_gate_pid(None) == 4242
    assert _durable_stop_gate_pid(123) == 123


def test_live_checkout_must_still_match_prefetched_binding():
    binding = CurrentPRResolution(
        "/worktree", "feature-b", 3062, head_sha="head-b", resolved=True
    )

    assert _binding_matches_live_checkout(binding, "feature-b", "head-b")
    assert not _binding_matches_live_checkout(binding, "feature-c", "head-c")
    assert not _binding_matches_live_checkout(binding, "feature-b", "new-head")


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
        worktree,
        "feature-b",
        3062,
        head_sha="head-b",
        resolved=True,
        is_draft=False,
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
    monkeypatch.setattr(
        "agentic_pr_dash._maintenance.stop_gate._current_branch",
        lambda cwd: "feature-b",
    )
    monkeypatch.setattr(
        "agentic_pr_dash._maintenance.stop_gate._local_head_sha",
        lambda cwd: "head-b",
    )

    assert mc._await_watch_pending_this_tick(
        [worktree], [], str(tmp_path), "session-a", bindings={worktree: binding}
    )
    assert probed == [(3062, worktree)]


def test_waiter_marks_tick_unknown_when_binding_changes_before_ci_probe(
    monkeypatch, tmp_path: Path
):
    worktree = str(tmp_path / "worktree")
    binding = CurrentPRResolution(
        worktree,
        "feature-b",
        3062,
        head_sha="head-b",
        resolved=True,
        is_draft=False,
    )
    probed: list[tuple[int, str]] = []
    monkeypatch.setattr(
        mc,
        "_revalidate_waiter_binding",
        lambda cwd, current: CurrentPRResolution(
            cwd,
            "feature-c",
            None,
            head_sha="head-c",
            resolved=True,
            unknown=True,
            stale_pr_number=3062,
        ),
    )
    monkeypatch.setattr(
        github_api,
        "required_checks_pending",
        lambda pr, cwd: probed.append((pr, cwd)) or False,
    )

    assert mc._await_watch_pending_this_tick(
        [worktree], [], str(tmp_path), "session-a", bindings={worktree: binding}
    )
    assert probed == []


def test_waiter_revalidates_checkout_before_using_bound_pr(
    monkeypatch, tmp_path: Path
):
    worktree = str(tmp_path / "worktree")
    binding = CurrentPRResolution(
        worktree, "feature-b", 3062, head_sha="head-b", resolved=True
    )
    monkeypatch.setattr(
        "agentic_pr_dash._maintenance.stop_gate._current_branch",
        lambda cwd: "feature-c",
    )
    monkeypatch.setattr(
        "agentic_pr_dash._maintenance.stop_gate._local_head_sha",
        lambda cwd: "head-c",
    )
    refreshed = mc._revalidate_waiter_binding(worktree, binding)

    assert refreshed.unknown is True
    assert refreshed.pr_number is None
    assert refreshed.branch == "feature-c"
    assert refreshed.head_sha == "head-c"


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
