from __future__ import annotations

from datetime import UTC, datetime

from agentic_pr_dash import github_api
from agentic_pr_dash._maintenance import pr_state, stop_gate, worktree_check
from agentic_pr_dash.models import PRData, PRStatus, ReviewComment


def _raw_pr(head: str = "sha-b") -> dict:
    return {
        "number": 77,
        "author": {"login": "reviewer"},
        "title": "Fresh maintenance",
        "headRefName": "feature",
        "baseRefName": "main",
        "headRefOid": head,
        "url": "https://github.com/acme/widgets/pull/77",
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "reviewDecision": "",
    }


def _stub_detail_reads(monkeypatch) -> None:
    monkeypatch.setattr(pr_state, "_current_branch", lambda _cwd: "feature")
    monkeypatch.setattr(github_api, "get_latest_commit", lambda *_a: ("sha-b", "2026-09-01T00:00:00Z"))
    monkeypatch.setattr(github_api, "get_ci_checks", lambda *_a: [])
    monkeypatch.setattr(github_api, "get_review_threads", lambda *_a, **_k: [])
    monkeypatch.setattr(
        github_api,
        "get_ci_checks_observation",
        lambda *_a: github_api.ObservationReadResult.observed([]),
    )


def test_check_forces_a_fresh_pr_list_and_records_exact_observation(monkeypatch) -> None:
    _stub_detail_reads(monkeypatch)
    forced: list[bool] = []

    def list_prs(_cwd: str, *, force: bool = False):
        forced.append(force)
        return [_raw_pr("sha-b" if force else "sha-a")]

    monkeypatch.setattr(github_api, "list_open_prs_cached", list_prs)
    monkeypatch.setattr(
        github_api,
        "scan_review_threads_observation",
        lambda *_a: github_api.ObservationReadResult.observed(([], [])),
    )

    pr, blockers = worktree_check._resolve_and_blockers("/worktree")

    assert forced == [True, True]
    assert blockers == []
    assert pr.latest_commit_sha == "sha-b"
    assert pr.maintenance_observed_head_sha == "sha-b"
    assert pr.maintenance_observed_base_branch == "main"
    assert pr.maintenance_observed_at.tzinfo is UTC
    assert "OBSERVED_HEAD_SHA=sha-b" in worktree_check._observation_evidence(pr)
    assert "OBSERVED_AT=" in worktree_check._observation_evidence(pr)


def test_new_submitted_p1_review_body_invalidates_prior_clean(monkeypatch) -> None:
    _stub_detail_reads(monkeypatch)
    monkeypatch.setattr(github_api, "list_open_prs_cached", lambda *_a, **_k: [_raw_pr()])
    observations = iter(
        [
            github_api.ObservationReadResult.observed(([], [])),
            github_api.ObservationReadResult.observed(
                ([ReviewComment(id=9, author="reviewer", body="[P1] still broken", created_at="2026-09-01T00:01:00Z", is_inline=False)], [])
            ),
        ]
    )
    monkeypatch.setattr(github_api, "scan_review_threads_observation", lambda *_a: next(observations))

    first, first_blockers = worktree_check._resolve_and_blockers("/worktree")
    second, second_blockers = worktree_check._resolve_and_blockers("/worktree")

    assert first_blockers == []
    assert second_blockers == ["review_comments"]
    assert first.maintenance_observed_at <= second.maintenance_observed_at


def test_missing_authoritative_review_read_fails_closed(monkeypatch) -> None:
    _stub_detail_reads(monkeypatch)
    monkeypatch.setattr(github_api, "list_open_prs_cached", lambda *_a, **_k: [_raw_pr()])
    monkeypatch.setattr(
        github_api,
        "scan_review_threads_observation",
        lambda *_a: github_api.ObservationReadResult.unavailable("reviews unavailable"),
    )

    pr, blockers = worktree_check._resolve_and_blockers("/worktree")

    assert pr is pr_state._GH_UNAVAILABLE
    assert blockers == []


def test_missing_authoritative_ci_read_fails_closed(monkeypatch) -> None:
    _stub_detail_reads(monkeypatch)
    monkeypatch.setattr(github_api, "list_open_prs_cached", lambda *_a, **_k: [_raw_pr()])
    monkeypatch.setattr(
        github_api,
        "get_ci_checks_observation",
        lambda *_a: github_api.ObservationReadResult.unavailable("CI unavailable"),
    )

    pr, blockers = worktree_check._resolve_and_blockers("/worktree")

    assert pr is pr_state._GH_UNAVAILABLE
    assert blockers == []


def test_head_change_during_authoritative_read_fails_closed(monkeypatch) -> None:
    _stub_detail_reads(monkeypatch)
    snapshots = iter([[_raw_pr("sha-a")], [_raw_pr("sha-b")]])
    monkeypatch.setattr(github_api, "list_open_prs_cached", lambda *_a, **_k: next(snapshots))
    monkeypatch.setattr(
        github_api,
        "scan_review_threads_observation",
        lambda *_a: github_api.ObservationReadResult.observed(([], [])),
    )

    pr, blockers = worktree_check._resolve_and_blockers("/worktree")

    assert pr is pr_state._GH_UNAVAILABLE
    assert blockers == []


def test_mutable_review_state_is_taken_from_final_identity_read(monkeypatch) -> None:
    _stub_detail_reads(monkeypatch)
    initial = _raw_pr()
    final = {**_raw_pr(), "reviewDecision": "CHANGES_REQUESTED"}
    snapshots = iter([[initial], [final]])
    monkeypatch.setattr(github_api, "list_open_prs_cached", lambda *_a, **_k: next(snapshots))
    monkeypatch.setattr(
        github_api,
        "scan_review_threads_observation",
        lambda *_a: github_api.ObservationReadResult.observed(([], [])),
    )

    pr, blockers = worktree_check._resolve_and_blockers("/worktree")

    assert pr.review_decision == "CHANGES_REQUESTED"
    assert blockers == ["changes_requested"]


def test_explicit_pr_retains_mutable_state_from_post_probe_snapshot(monkeypatch) -> None:
    _stub_detail_reads(monkeypatch)
    initial = _raw_pr()
    final = {**_raw_pr(), "isDraft": True, "mergeStateStatus": "DRAFT"}
    snapshots = iter([[], [initial], [final]])
    monkeypatch.setattr(
        github_api, "list_open_prs_cached", lambda *_a, **_k: next(snapshots)
    )
    monkeypatch.setattr(
        github_api,
        "_rest_pr_payload",
        lambda *_a, **_k: {**initial, "state": "open"},
    )
    monkeypatch.setattr(pr_state, "_gh_pr_view_field", lambda *_a: ("APPROVED", ""))
    monkeypatch.setattr(
        github_api,
        "scan_review_threads_observation",
        lambda *_a: github_api.ObservationReadResult.observed(([], [])),
    )

    with pr_state.authoritative_maintenance_read():
        pr = pr_state._resolve_pr_by_number(77, "/worktree")

    assert pr.is_draft is True
    assert pr.merge_state == "DRAFT"
    assert pr.review_decision == "APPROVED"


def test_unresolved_thread_fallback_failure_is_unavailable(monkeypatch) -> None:
    _stub_detail_reads(monkeypatch)
    monkeypatch.setattr(github_api, "list_open_prs_cached", lambda *_a, **_k: [_raw_pr()])
    monkeypatch.setattr(
        github_api,
        "scan_review_threads_observation",
        lambda *_a: github_api.ObservationReadResult.observed(([], [])),
    )
    monkeypatch.setattr(
        github_api,
        "get_review_threads",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("threads unavailable")),
    )

    pr, blockers = worktree_check._resolve_and_blockers("/worktree")

    assert pr is pr_state._GH_UNAVAILABLE
    assert blockers == []


def test_observation_validator_rejects_head_or_base_mismatch() -> None:
    pr = PRData(
        number=77,
        title="Fresh maintenance",
        branch="feature",
        base_branch="main",
        url="https://github.com/acme/widgets/pull/77",
        latest_commit_sha="sha-b",
        status=PRStatus.CLEAN,
        maintenance_observed_head_sha="sha-a",
        maintenance_observed_base_branch="release",
        maintenance_observed_at=datetime.now(UTC),
    )

    assert pr_state.authoritative_observation_matches(pr) is False


def test_observation_validator_rejects_missing_evidence() -> None:
    pr = PRData(
        number=77,
        title="Fresh maintenance",
        branch="feature",
        base_branch="main",
        url="https://github.com/acme/widgets/pull/77",
        latest_commit_sha="sha-b",
        status=PRStatus.CLEAN,
    )

    assert pr_state.authoritative_observation_matches(pr) is False


def test_authoritative_scope_discards_primed_detail_cache() -> None:
    github_api.prime_pr_batch_cache(
        "acme/widgets",
        {77: {"merge_state": "CLEAN", "mergeable": "MERGEABLE", "threads": []}},
        "/worktree",
    )

    with pr_state.authoritative_maintenance_read():
        assert github_api.get_primed_mergeability(77, "/worktree") is None


def test_authoritative_scope_discards_preloop_checkpoint_batch() -> None:
    github_api.clear_pr_batch_cache()
    github_api.prime_pr_batch_cache(
        "acme/widgets",
        {77: {"merge_state": "CLEAN", "mergeable": "MERGEABLE"}},
        "/worktree",
    )
    github_api.mark_pr_batch_cache_authoritative()

    with pr_state.authoritative_maintenance_read():
        assert github_api.get_primed_mergeability(77, "/worktree") is None


def test_stop_fingerprint_ignores_only_volatile_observation_time() -> None:
    first = [("/worktree", "blocked\nOBSERVED_HEAD_SHA=sha-a\nOBSERVED_AT=2026-09-01T00:00:00Z")]
    later = [("/worktree", "blocked\nOBSERVED_HEAD_SHA=sha-a\nOBSERVED_AT=2026-09-01T00:01:00Z")]
    moved = [("/worktree", "blocked\nOBSERVED_HEAD_SHA=sha-b\nOBSERVED_AT=2026-09-01T00:01:00Z")]

    assert stop_gate._stop_fingerprint(first) == stop_gate._stop_fingerprint(later)
    assert stop_gate._stop_fingerprint(first) != stop_gate._stop_fingerprint(moved)
