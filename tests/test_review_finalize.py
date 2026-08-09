import pytest
from agent_review_coordinator import (
    FindingSettlementState,
    HeadAttestation,
    HeadAttestationKind,
    ReviewLedger,
    ReviewPolicy,
    ReviewResult,
    ReviewStage,
)

from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash._maintenance.review_settlement import (
    combine_clean_observations,
    evaluate_pr_snapshot,
)
from agentic_pr_dash.github_api import ReviewSubmission
from agentic_pr_dash.models import CICheck, PRData

REPOSITORY = "Boundless-Studios/agentic-pr-dash"
HEAD = "a" * 40
DELIVERY_ID = "delivery-pr-24"
REVIEW_CHARTER_VERSION = "review-charter-v1"


def _policy() -> ReviewPolicy:
    return ReviewPolicy.model_validate(
        {
            "version": 1,
            "review": {
                "local": {"reviewer_count": 1},
                "backstop": {
                    "reviewer_count": 1,
                    "trigger": "new_head_sha",
                },
            },
        }
    )


def _ledger() -> ReviewLedger:
    ledger = ReviewLedger(
        repository=REPOSITORY,
        head_sha=HEAD,
        delivery_id=DELIVERY_ID,
        review_charter_version=REVIEW_CHARTER_VERSION,
    )
    for stage in (ReviewStage.LOCAL, ReviewStage.BACKSTOP):
        ledger.submit(
            ReviewResult(
                repository=REPOSITORY,
                head_sha=HEAD,
                stage=stage,
                round_number=1,
                slot_number=1,
                reviewer_execution_id=f"{stage.value}-review",
            )
        )
    return ledger


def _pr(**updates) -> PRData:
    values = {
        "number": 24,
        "repo": REPOSITORY,
        "title": "Review settlement",
        "branch": "review-settlement",
        "url": (
            "https://github.com/"
            f"{REPOSITORY}/pull/24"
        ),
        "merge_state": "CLEAN",
        "mergeable": "MERGEABLE",
        "review_decision": "APPROVED",
        "latest_commit_sha": HEAD,
        "ci_checks": [
            CICheck(
                name="tests",
                status="completed",
                conclusion="success",
            )
        ],
    }
    values.update(updates)
    return PRData(**values)


def _observation(*, pr: PRData | None = None, ledger: ReviewLedger | None = None):
    return evaluate_pr_snapshot(
        pr=pr or _pr(),
        policy=_policy(),
        ledger=ledger or _ledger(),
        threads=[],
        deferrals={},
    )


def test_clean_snapshot_is_ready_but_not_final_until_reobserved() -> None:
    first = _observation()

    assert first.clean
    assert not combine_clean_observations(first, None).settled
    assert combine_clean_observations(first, first).settled


def test_exhausted_descendant_attestation_does_not_report_local_slot() -> None:
    first_head = "c" * 40
    reviewed_head = "b" * 40
    ledger = ReviewLedger(
        repository=REPOSITORY,
        head_sha=first_head,
        delivery_id=DELIVERY_ID,
        review_charter_version=REVIEW_CHARTER_VERSION,
    )
    policy = _policy().model_copy(
        update={
            "review": _policy().review.model_copy(
                update={
                    "local": _policy().review.local.model_copy(
                        update={"max_generation_rounds": 2}
                    )
                }
            )
        }
    )
    ledger.submit(
        ReviewResult(
            repository=REPOSITORY,
            head_sha=first_head,
            stage=ReviewStage.LOCAL,
            round_number=1,
            slot_number=1,
            reviewer_execution_id="local-r1",
        )
    )
    ledger.advance_head(reviewed_head)
    ledger.submit(
        ReviewResult(
            repository=REPOSITORY,
            head_sha=reviewed_head,
            stage=ReviewStage.LOCAL,
            round_number=2,
            slot_number=1,
            reviewer_execution_id="local-r2",
        )
    )
    ledger.advance_head(HEAD)
    ledger.record_head_attestation(
        HeadAttestation(
            repository=REPOSITORY,
            delivery_id=DELIVERY_ID,
            review_charter_version=REVIEW_CHARTER_VERSION,
            reviewed_head_sha=reviewed_head,
            head_sha=HEAD,
            kind=HeadAttestationKind.EXHAUSTED_DELIVERY_CONTINUITY,
            delta_sha256="a" * 64,
            evidence=["ci:tests-success"],
            attested_by="gaia-review-local",
        ),
        stage_policy=policy.review.local,
    )
    ledger.submit(
        ReviewResult(
            repository=REPOSITORY,
            head_sha=HEAD,
            stage=ReviewStage.BACKSTOP,
            round_number=1,
            slot_number=1,
            reviewer_execution_id="backstop-current-head",
        )
    )

    observation = evaluate_pr_snapshot(
        pr=_pr(),
        policy=policy,
        ledger=ledger,
        threads=[],
        deferrals={},
    )

    assert observation.review.missing_slots == []
    assert observation.review.settled


def test_required_ci_pending_blocks() -> None:
    pr = _pr(ci_watch_pending=True)

    assert "ci_pending" in _observation(pr=pr).blockers


def test_absent_ci_evidence_blocks() -> None:
    assert "ci_unavailable" in _observation(pr=_pr(ci_checks=[])).blockers


def test_cancelled_ci_check_blocks() -> None:
    pr = _pr(
        ci_checks=[
            CICheck(
                name="tests",
                status="completed",
                conclusion="cancelled",
            )
        ]
    )

    assert "ci_not_successful" in _observation(pr=pr).blockers


def test_merge_conflict_and_change_request_block() -> None:
    observation = _observation(
        pr=_pr(
            merge_state="DIRTY",
            mergeable="CONFLICTING",
            review_decision="CHANGES_REQUESTED",
        )
    )

    assert "merge_conflict" in observation.blockers
    assert "changes_requested" in observation.blockers


def test_ledger_head_drift_blocks() -> None:
    assert "head_drift" in _observation(
        pr=_pr(latest_commit_sha="b" * 40)
    ).blockers


def test_missing_reviewer_result_blocks() -> None:
    ledger = _ledger()
    ledger.results = [
        result for result in ledger.results if result.stage is ReviewStage.LOCAL
    ]

    observation = _observation(ledger=ledger)

    assert not observation.clean
    assert observation.review.missing_slots == ["backstop:1"]


def test_draft_pr_does_not_require_backstop_settlement() -> None:
    ledger = _ledger()
    ledger.results = [
        result for result in ledger.results if result.stage is ReviewStage.LOCAL
    ]

    observation = _observation(pr=_pr(is_draft=True), ledger=ledger)

    assert observation.clean
    assert observation.review.settled
    assert observation.review.missing_slots == []


def test_draft_pr_ignores_ship_readiness_blockers() -> None:
    observation = _observation(
        pr=_pr(
            is_draft=True,
            merge_state="DIRTY",
            mergeable="CONFLICTING",
            review_decision="CHANGES_REQUESTED",
            ci_checks=[
                CICheck(
                    name="tests",
                    status="completed",
                    conclusion="failure",
                )
            ],
        )
    )

    assert observation.clean


def test_draft_pr_still_requires_local_review_result() -> None:
    ledger = _ledger()
    ledger.results = []

    observation = _observation(pr=_pr(is_draft=True), ledger=ledger)

    assert not observation.clean
    assert observation.review.missing_slots == ["local:1"]


def test_live_current_head_review_satisfies_missing_backstop_result() -> None:
    ledger = _ledger()
    ledger.results = [
        result for result in ledger.results if result.stage is ReviewStage.LOCAL
    ]

    observation = evaluate_pr_snapshot(
        pr=_pr(),
        policy=_policy(),
        ledger=ledger,
        threads=[],
        deferrals={},
        review_submissions=[
            ReviewSubmission(
                review_id=321,
                author="review-bot",
                state="COMMENTED",
                commit_id=HEAD,
                submitted_at="2026-07-28T00:00:00Z",
            )
        ],
    )

    assert observation.clean


def test_pr_author_review_does_not_satisfy_backstop_result() -> None:
    ledger = _ledger()
    ledger.results = [
        result for result in ledger.results if result.stage is ReviewStage.LOCAL
    ]

    observation = evaluate_pr_snapshot(
        pr=_pr(author="pr-author"),
        policy=_policy(),
        ledger=ledger,
        threads=[],
        deferrals={},
        review_submissions=[],
    )

    assert not observation.clean
    assert observation.review.missing_slots == ["backstop:1"]


def test_unevaluated_p2_remains_blocking_before_budget_exhaustion() -> None:
    from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment

    thread = ReviewThread(
        node_id="PRRT_p2",
        is_resolved=False,
        is_outdated=False,
        top=ReviewThreadComment(
            database_id=1,
            path="src/review.py",
            line=12,
            body="[P2] Speculative provider edge",
            author="reviewer",
            created_at="2026-07-28T00:00:00Z",
        ),
    )

    blocked = evaluate_pr_snapshot(
        pr=_pr(),
        policy=_policy(),
        ledger=_ledger(),
        threads=[thread],
        deferrals={},
    )
    deferred = evaluate_pr_snapshot(
        pr=_pr(),
        policy=_policy(),
        ledger=_ledger(),
        threads=[thread],
        deferrals={
            "PRRT_p2": {
                "reason": "No supported-path reproduction or production evidence.",
            }
        },
    )

    assert blocked.review.required_actions == ["evaluate_p2"]
    assert not deferred.clean
    assert deferred.review.required_actions == ["evaluate_p2"]


def test_exhausted_p2_is_recorded_without_blocking_finalization() -> None:
    from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment

    ledger = _ledger()
    ledger.submit(
        ReviewResult(
            repository=REPOSITORY,
            head_sha=HEAD,
            stage=ReviewStage.LOCAL,
            round_number=2,
            slot_number=1,
            reviewer_execution_id="local-review-round-2",
            reviewer_provider="round-two-provider",
        )
    )
    thread = ReviewThread(
        node_id="PRRT_exhausted_p2",
        is_resolved=False,
        is_outdated=False,
        top=ReviewThreadComment(
            database_id=3,
            path="src/review.py",
            line=24,
            body="[P2] Speculative edge after the review budget is exhausted",
            author="reviewer",
            created_at="2026-07-28T00:00:00Z",
        ),
    )

    observation = evaluate_pr_snapshot(
        pr=_pr(),
        policy=_policy(),
        ledger=ledger,
        threads=[thread],
        deferrals={},
    )

    assert observation.clean
    assert observation.review.required_actions == []
    assert list(observation.review.finding_states.values()) == [
        FindingSettlementState.DECLINED_WITH_RATIONALE
    ]


def test_exhausted_review_budget_does_not_suppress_p1() -> None:
    from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment

    ledger = _ledger()
    ledger.submit(
        ReviewResult(
            repository=REPOSITORY,
            head_sha=HEAD,
            stage=ReviewStage.LOCAL,
            round_number=2,
            slot_number=1,
            reviewer_execution_id="local-review-round-2",
            reviewer_provider="round-two-provider",
        )
    )
    thread = ReviewThread(
        node_id="PRRT_exhausted_p1",
        is_resolved=False,
        is_outdated=False,
        top=ReviewThreadComment(
            database_id=4,
            path="src/review.py",
            line=25,
            body="[P1] Required status is dropped",
            author="reviewer",
            created_at="2026-07-28T00:00:00Z",
        ),
    )

    observation = evaluate_pr_snapshot(
        pr=_pr(),
        policy=_policy(),
        ledger=ledger,
        threads=[thread],
        deferrals={},
    )

    assert not observation.clean
    assert observation.review.required_actions == ["address_p1"]


def test_exhausted_review_budget_does_not_suppress_p0() -> None:
    from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment

    ledger = _ledger()
    ledger.submit(
        ReviewResult(
            repository=REPOSITORY,
            head_sha=HEAD,
            stage=ReviewStage.LOCAL,
            round_number=2,
            slot_number=1,
            reviewer_execution_id="local-review-round-2",
            reviewer_provider="round-two-provider",
        )
    )
    thread = ReviewThread(
        node_id="PRRT_exhausted_p0",
        is_resolved=False,
        is_outdated=False,
        top=ReviewThreadComment(
            database_id=5,
            path="src/review.py",
            line=26,
            body="[P0] Irreversible data loss",
            author="reviewer",
            created_at="2026-07-28T00:00:00Z",
        ),
    )

    observation = evaluate_pr_snapshot(
        pr=_pr(),
        policy=_policy(),
        ledger=ledger,
        threads=[thread],
        deferrals={},
    )

    assert not observation.clean
    assert observation.review.required_actions == ["address_p0"]


def test_p1_deferral_never_settles() -> None:
    from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment

    thread = ReviewThread(
        node_id="PRRT_p1",
        is_resolved=False,
        is_outdated=False,
        top=ReviewThreadComment(
            database_id=2,
            path="src/review.py",
            line=20,
            body="[P1] Drops a required status",
            author="reviewer",
            created_at="2026-07-28T00:00:00Z",
        ),
    )

    observation = evaluate_pr_snapshot(
        pr=_pr(),
        policy=_policy(),
        ledger=_ledger(),
        threads=[thread],
        deferrals={"PRRT_p1": {"reason": "Out of scope."}},
    )

    assert not observation.clean
    assert observation.review.required_actions == ["address_p1"]


def test_two_observations_must_match() -> None:
    first = _observation()
    second = _observation(pr=_pr(latest_commit_sha="b" * 40))

    report = combine_clean_observations(first, second)

    assert not report.settled
    assert not report.stable
    assert "head_drift" in report.blockers


def test_finalize_cli_requires_two_clean_observations(
    tmp_path, monkeypatch, capsys
) -> None:
    policy_path = tmp_path / "review-policy.yaml"
    policy_path.write_text(
        """
version: 1
review:
  local:
    reviewer_count: 1
  backstop:
    reviewer_count: 1
    trigger: new_head_sha
""".lstrip(),
        encoding="utf-8",
    )
    ledger_path = tmp_path / "review-ledger.json"
    ledger_path.write_text(_ledger().model_dump_json(), encoding="utf-8")
    observations = iter([_observation(), _observation()])
    monkeypatch.setattr(
        mc,
        "_observe_finalization",
        lambda *args, **kwargs: next(observations),
    )
    slept: list[float] = []
    monkeypatch.setattr(mc.time, "sleep", slept.append)

    rc = mc.main(
        [
            "finalize",
            "--cwd",
            str(tmp_path),
            "--policy",
            str(policy_path),
            "--ledger",
            str(ledger_path),
            "--stabilization-seconds",
            "0",
            "--json",
        ]
    )

    assert rc == 0
    assert slept == [0.0]
    assert '"settled": true' in capsys.readouterr().out


def test_finalize_cli_returns_work_remaining_without_waiting(
    tmp_path, monkeypatch
) -> None:
    policy_path = tmp_path / "review-policy.yaml"
    policy_path.write_text(
        """
version: 1
review:
  local:
    reviewer_count: 1
  backstop:
    reviewer_count: 1
""".lstrip(),
        encoding="utf-8",
    )
    ledger = _ledger()
    ledger.results = [
        result for result in ledger.results if result.stage is ReviewStage.LOCAL
    ]
    ledger_path = tmp_path / "review-ledger.json"
    ledger_path.write_text(ledger.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(
        mc,
        "_observe_finalization",
        lambda *args, **kwargs: _observation(ledger=ledger),
    )
    monkeypatch.setattr(
        mc.time,
        "sleep",
        lambda seconds: (_ for _ in ()).throw(AssertionError("must not sleep")),
    )

    rc = mc.main(
        [
            "finalize",
            "--cwd",
            str(tmp_path),
            "--policy",
            str(policy_path),
            "--ledger",
            str(ledger_path),
            "--stabilization-seconds",
            "0",
            "--json",
        ]
    )

    assert rc == 10


def test_stop_gate_uses_canonical_finalization_when_configured(
    monkeypatch,
) -> None:
    import argparse

    calls: list[str] = []
    monkeypatch.setattr(
        mc,
        "_cmd_finalize",
        lambda args: calls.append(args.policy) or 10,
    )
    legacy_calls: list[bool] = []
    monkeypatch.setattr(
        mc,
        "_stop_gate_impl",
        lambda args: legacy_calls.append(True) or 0,
    )
    args = argparse.Namespace(
        policy="review-policy.yaml",
        ledger=None,
    )

    assert mc._cmd_stop_gate(args) == 2
    assert legacy_calls == [True]
    assert calls == ["review-policy.yaml"]


def test_stop_gate_refuses_ledger_without_policy(capsys) -> None:
    import argparse

    args = argparse.Namespace(policy=None, ledger="review-ledger.json")

    assert mc._cmd_stop_gate(args) == 2
    assert "--ledger requires --policy" in capsys.readouterr().err


def test_finalize_without_ledger_blocks_an_open_pr(tmp_path, monkeypatch, capsys) -> None:
    policy_path = tmp_path / "review-policy.yaml"
    policy_path.write_text(
        """
version: 1
review:
  local:
    reviewer_count: 1
  backstop:
    reviewer_count: 1
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        mc,
        "_resolve_pr_for_branch",
        lambda cwd, force=False: _pr(),
    )

    rc = mc.main(
        [
            "finalize",
            "--cwd",
            str(tmp_path),
            "--policy",
            str(policy_path),
            "--stabilization-seconds",
            "0",
            "--json",
        ]
    )

    assert rc == 10
    assert "review_ledger_missing" in capsys.readouterr().out


def test_finalize_without_ledger_is_noop_when_there_is_no_pr(
    tmp_path, monkeypatch, capsys
) -> None:
    policy_path = tmp_path / "review-policy.yaml"
    policy_path.write_text(
        """
version: 1
review:
  local:
    reviewer_count: 1
  backstop:
    reviewer_count: 1
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        mc,
        "_resolve_pr_for_branch",
        lambda cwd, force=False: None,
    )

    rc = mc.main(
        [
            "finalize",
            "--cwd",
            str(tmp_path),
            "--policy",
            str(policy_path),
            "--json",
        ]
    )

    assert rc == 0
    assert "no_open_pr" in capsys.readouterr().out


def test_finalize_with_existing_ledger_is_noop_when_there_is_no_pr(
    tmp_path, monkeypatch, capsys
) -> None:
    policy_path = tmp_path / "review-policy.yaml"
    policy_path.write_text(
        """
version: 1
review:
  local:
    reviewer_count: 1
  backstop:
    reviewer_count: 1
""".lstrip(),
        encoding="utf-8",
    )
    ledger_path = tmp_path / "review-ledger.json"
    ledger_path.write_text(_ledger().model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(
        mc,
        "_resolve_pr_for_branch",
        lambda cwd, force=False: None,
    )

    rc = mc.main(
        [
            "finalize",
            "--cwd",
            str(tmp_path),
            "--policy",
            str(policy_path),
            "--ledger",
            str(ledger_path),
            "--json",
        ]
    )

    assert rc == 0
    assert "no_open_pr" in capsys.readouterr().out


def test_finalization_observation_fails_when_required_ci_is_unobservable(
    tmp_path, monkeypatch
) -> None:
    import argparse

    from agentic_pr_dash import github_api

    monkeypatch.setattr(
        mc,
        "_resolve_pr_for_branch",
        lambda cwd, force=False: _pr(),
    )
    monkeypatch.setattr(mc, "_repo_slug", lambda cwd: REPOSITORY)
    monkeypatch.setattr(
        github_api,
        "reset_checks_probe_failure_seen",
        lambda: None,
    )
    monkeypatch.setattr(
        github_api,
        "required_checks_pending",
        lambda pr, cwd: False,
    )
    monkeypatch.setattr(
        github_api,
        "checks_probe_failure_seen",
        lambda: True,
    )
    args = argparse.Namespace(cwd=str(tmp_path), pr=None)

    with pytest.raises(RuntimeError, match="required CI status is unobservable"):
        mc._observe_finalization(args, _policy(), _ledger())


def test_finalization_reads_submissions_before_review_threads(
    tmp_path,
    monkeypatch,
) -> None:
    import argparse

    from agentic_pr_dash import github_api
    from agentic_pr_dash._maintenance import deferred_review

    calls: list[str] = []
    monkeypatch.setattr(
        mc,
        "_resolve_pr_for_branch",
        lambda cwd, force=False: _pr(),
    )
    monkeypatch.setattr(mc, "_repo_slug", lambda cwd: REPOSITORY)
    monkeypatch.setattr(
        github_api,
        "reset_checks_probe_failure_seen",
        lambda: None,
    )
    monkeypatch.setattr(
        github_api,
        "required_checks_pending",
        lambda pr, cwd: False,
    )
    monkeypatch.setattr(
        github_api,
        "checks_probe_failure_seen",
        lambda: False,
    )
    monkeypatch.setattr(
        github_api,
        "clear_pr_batch_cache",
        lambda: calls.append("clear"),
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions",
        lambda *args, **kwargs: calls.append("submissions") or [],
    )
    monkeypatch.setattr(
        github_api,
        "get_review_threads",
        lambda *args, **kwargs: calls.append("threads") or [],
    )
    monkeypatch.setattr(
        deferred_review,
        "deferred_threads_for_pr",
        lambda *args, **kwargs: {},
    )

    mc._observe_finalization(
        argparse.Namespace(cwd=str(tmp_path), pr=None),
        _policy(),
        _ledger(),
    )

    assert calls == ["clear", "submissions", "threads"]
