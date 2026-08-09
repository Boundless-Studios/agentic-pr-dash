from agent_review_coordinator import (
    Disposition,
    ReviewLedger,
    ReviewPolicy,
    ReviewResult,
    ReviewStage,
    Severity,
    __version__,
    evaluate,
)

from agentic_pr_dash._maintenance.review_settlement import (
    finding_from_thread,
    findings_from_review_submission,
    overlay_backstop_evidence,
    overlay_backstop_results,
    overlay_github_findings,
)
from agentic_pr_dash.github_api import (
    ReviewSubmission,
    ReviewThread,
    ReviewThreadComment,
)


def test_review_coordinator_contract_version() -> None:
    ledger = ReviewLedger(
        repository="Boundless-Studios/gaia-free",
        head_sha="a" * 40,
        delivery_id="delivery-pr-24",
        review_charter_version="review-charter-v1",
    )

    assert __version__ == "0.3.0"
    assert ledger.version == 2


REPOSITORY = "Boundless-Studios/gaia-free"
HEAD = "a" * 40
DELIVERY_ID = "delivery-pr-24"
REVIEW_CHARTER_VERSION = "review-charter-v1"


def _thread(
    *,
    node_id: str = "PRRT_one",
    database_id: int = 1,
    body: str = "[P2] Avoid a false green\nRequired CI can be absent.",
    path: str = "scripts/review.py",
    line: int = 10,
    review_id: int | None = None,
) -> ReviewThread:
    return ReviewThread(
        node_id=node_id,
        is_resolved=False,
        is_outdated=False,
        top=ReviewThreadComment(
            database_id=database_id,
            path=path,
            line=line,
            body=body,
            author="review-bot",
            created_at="2026-07-28T00:00:00Z",
            review_id=review_id,
        ),
    )


def _policy() -> ReviewPolicy:
    return ReviewPolicy.model_validate(
        {
            "version": 1,
            "review": {
                "local": {"reviewer_count": 1, "required_results": 1},
                "backstop": {
                    "reviewer_count": 1,
                    "required_results": 1,
                    "trigger": "new_head_sha",
                },
            },
        }
    )


def _exhausted_policy() -> ReviewPolicy:
    policy = _policy()
    policy.review.local.max_generation_rounds = 1
    return policy


def _ledger_with_local_result() -> ReviewLedger:
    ledger = ReviewLedger(
        repository=REPOSITORY,
        head_sha=HEAD,
        delivery_id=DELIVERY_ID,
        review_charter_version=REVIEW_CHARTER_VERSION,
    )
    ledger.submit(
        ReviewResult(
            repository=REPOSITORY,
            head_sha=HEAD,
            stage=ReviewStage.LOCAL,
            round_number=1,
            slot_number=1,
            reviewer_execution_id="local-r1-slot1",
        )
    )
    return ledger


def test_overlay_preserves_delivery_identity_through_json_boundary() -> None:
    loaded = ReviewLedger.model_validate_json(
        _ledger_with_local_result().model_dump_json()
    )

    overlaid = overlay_github_findings(
        loaded,
        threads=[],
        deferrals={},
    )

    assert overlaid.delivery_id == DELIVERY_ID
    assert overlaid.review_charter_version == REVIEW_CHARTER_VERSION


def test_finding_from_thread_normalizes_p1_and_snapshot_identity() -> None:
    finding = finding_from_thread(
        _thread(body="[P1] Do not drop a blocking review"),
        repository=REPOSITORY,
        head_sha=HEAD,
        reviewer_execution_id="github-backstop",
    )

    assert finding.severity is Severity.P1
    assert finding.repository == REPOSITORY
    assert finding.head_sha == HEAD
    assert finding.path == "scripts/review.py"
    assert finding.line == 10
    assert "PRRT_one" in (finding.evidence or "")


def test_finding_from_thread_preserves_declared_p0() -> None:
    finding = finding_from_thread(
        _thread(body="[P0] Prevent irreversible data loss"),
        repository=REPOSITORY,
        head_sha=HEAD,
        reviewer_execution_id="github-backstop",
    )

    assert finding.severity is Severity.P0


def test_top_level_review_preserves_declared_p0() -> None:
    findings = findings_from_review_submission(
        ReviewSubmission(
            review_id=123,
            author="review-bot",
            state="COMMENTED",
            commit_id=HEAD,
            submitted_at="2026-07-28T00:00:00Z",
            body="[P0] Prevent irreversible data loss",
        ),
        repository=REPOSITORY,
        head_sha=HEAD,
        reviewer_execution_id="github-review-123",
    )

    assert [finding.severity for finding in findings] == [Severity.P0]


def test_declared_p3_preserves_nonblocking_typed_severity() -> None:
    finding = finding_from_thread(
        _thread(body="[P3] Optional readability improvement"),
        repository=REPOSITORY,
        head_sha=HEAD,
        reviewer_execution_id="github-backstop",
    )

    assert finding.severity is Severity.P3


def test_declared_p2_stays_p2_when_explanation_mentions_p1() -> None:
    finding = finding_from_thread(
        _thread(
            body=(
                "[P2] Keep this scoped\n"
                "This was evaluated against the P1 policy and is not blocking."
            )
        ),
        repository=REPOSITORY,
        head_sha=HEAD,
        reviewer_execution_id="github-backstop",
    )

    assert finding.severity is Severity.P2


def test_equivalent_threads_share_fingerprint() -> None:
    first = finding_from_thread(
        _thread(node_id="PRRT_one", database_id=1),
        repository=REPOSITORY,
        head_sha=HEAD,
        reviewer_execution_id="github-backstop",
    )
    second = finding_from_thread(
        _thread(node_id="PRRT_two", database_id=2),
        repository=REPOSITORY,
        head_sha=HEAD,
        reviewer_execution_id="github-backstop",
    )

    assert first.fingerprint == second.fingerprint


def test_thread_deferral_clears_finding_without_synthesizing_review() -> None:
    ledger = overlay_github_findings(
        _ledger_with_local_result(),
        threads=[_thread()],
        deferrals={
            "PRRT_one": {
                "reason": "Unsupported provider path with no observed occurrence.",
                "ticket": "",
            }
        },
    )

    finding = ledger.current_findings[0]
    assert finding.disposition is Disposition.DEFER
    assert finding.rationale == "Unsupported provider path with no observed occurrence."
    report = evaluate(policy=_exhausted_policy(), ledger=ledger)
    assert report.required_actions == []
    assert report.missing_slots == ["backstop:1"]


def test_no_threads_does_not_synthesize_backstop_result() -> None:
    ledger = overlay_github_findings(
        _ledger_with_local_result(),
        threads=[],
        deferrals={},
    )

    report = evaluate(policy=_policy(), ledger=ledger)

    assert not report.settled
    assert report.missing_slots == ["backstop:1"]


def test_current_head_review_submission_fills_backstop_slot() -> None:
    ledger = overlay_backstop_results(
        _ledger_with_local_result(),
        reviews=[
            ReviewSubmission(
                review_id=123,
                author="review-bot",
                state="COMMENTED",
                commit_id=HEAD,
                submitted_at="2026-07-28T00:00:00Z",
            )
        ],
        reviewer_count=1,
    )

    report = evaluate(policy=_policy(), ledger=ledger)

    assert report.settled
    backstop = [
        result
        for result in ledger.results
        if result.stage is ReviewStage.BACKSTOP
    ]
    assert backstop[0].reviewer_execution_id == "github-review-123"
    assert backstop[0].reviewer_provider == "review-bot"


def test_thread_and_submission_from_same_review_fill_one_backstop_slot() -> None:
    policy = ReviewPolicy.model_validate(
        {
            "version": 1,
            "review": {
                "local": {"reviewer_count": 1, "required_results": 1},
                "backstop": {
                    "reviewer_count": 2,
                    "required_results": 2,
                    "trigger": "new_head_sha",
                },
            },
        }
    )
    ledger = overlay_backstop_evidence(
        _ledger_with_local_result(),
        threads=[_thread(review_id=123)],
        deferrals={
            "PRRT_one": {
                "reason": "Evaluated and deferred.",
                "ticket": "",
            }
        },
        reviews=[
            ReviewSubmission(
                review_id=123,
                author="review-bot",
                state="COMMENTED",
                commit_id=HEAD,
                submitted_at="2026-07-28T00:00:00Z",
            )
        ],
        reviewer_count=2,
    )

    policy.review.local.max_generation_rounds = 1
    report = evaluate(policy=policy, ledger=ledger)

    assert not report.settled
    assert report.missing_slots == ["backstop:2"]


def test_review_body_finding_blocks_backstop_settlement() -> None:
    ledger = overlay_backstop_results(
        _ledger_with_local_result(),
        reviews=[
            ReviewSubmission(
                review_id=123,
                author="review-bot",
                state="COMMENTED",
                commit_id=HEAD,
                submitted_at="2026-07-28T00:00:00Z",
                body="[P1] Do not discard a review-level blocker.",
            )
        ],
        reviewer_count=1,
    )

    report = evaluate(policy=_policy(), ledger=ledger)

    assert not report.settled
    assert report.required_actions == ["address_p1"]
    assert ledger.current_findings[0].title == (
        "Do not discard a review-level blocker."
    )


def test_all_review_bodies_are_inspected_after_required_slots_fill() -> None:
    ledger = overlay_backstop_evidence(
        _ledger_with_local_result(),
        threads=[],
        deferrals={},
        reviews=[
            ReviewSubmission(
                review_id=123,
                author="first-reviewer",
                state="COMMENTED",
                commit_id=HEAD,
                submitted_at="2026-07-28T00:00:00Z",
            ),
            ReviewSubmission(
                review_id=456,
                author="later-reviewer",
                state="COMMENTED",
                commit_id=HEAD,
                submitted_at="2026-07-28T00:01:00Z",
                body="[P1] Do not discard findings after quorum.",
            ),
        ],
        reviewer_count=1,
    )

    report = evaluate(policy=_policy(), ledger=ledger)

    assert not report.settled
    assert report.required_actions == ["address_p1"]


def test_two_threaded_reviews_fill_two_distinct_backstop_slots() -> None:
    policy = ReviewPolicy.model_validate(
        {
            "version": 1,
            "review": {
                "local": {"reviewer_count": 1, "required_results": 1},
                "backstop": {
                    "reviewer_count": 2,
                    "required_results": 2,
                    "trigger": "new_head_sha",
                },
            },
        }
    )
    ledger = overlay_backstop_evidence(
        _ledger_with_local_result(),
        threads=[
            _thread(node_id="PRRT_one", review_id=123),
            _thread(node_id="PRRT_two", review_id=456),
        ],
        deferrals={
            "PRRT_one": {"reason": "Evaluated first P2."},
            "PRRT_two": {"reason": "Evaluated second P2."},
        },
        reviews=[
            ReviewSubmission(
                review_id=123,
                author="first-reviewer",
                state="COMMENTED",
                commit_id=HEAD,
                submitted_at="2026-07-28T00:00:00Z",
            ),
            ReviewSubmission(
                review_id=456,
                author="second-reviewer",
                state="COMMENTED",
                commit_id=HEAD,
                submitted_at="2026-07-28T00:01:00Z",
            ),
        ],
        reviewer_count=2,
    )

    policy.review.local.max_generation_rounds = 1
    report = evaluate(policy=policy, ledger=ledger)

    assert report.settled
    assert {
        result.reviewer_execution_id
        for result in ledger.results
        if result.stage is ReviewStage.BACKSTOP and result.slot_number <= 2
    } == {"github-review-123", "github-review-456"}


def test_review_body_p2_accepts_review_id_deferral() -> None:
    ledger = overlay_backstop_evidence(
        _ledger_with_local_result(),
        threads=[],
        deferrals={
            "review:123": {
                "reason": "No supported-path reproduction.",
                "ticket": "",
            }
        },
        reviews=[
            ReviewSubmission(
                review_id=123,
                author="review-bot",
                state="COMMENTED",
                commit_id=HEAD,
                submitted_at="2026-07-28T00:00:00Z",
                body="[P2] Defend an unobserved edge case.",
            )
        ],
        reviewer_count=1,
    )

    report = evaluate(policy=_exhausted_policy(), ledger=ledger)

    assert report.settled
    assert ledger.current_findings[0].disposition is Disposition.DEFER


def test_multiple_review_body_p2s_require_individual_dispositions() -> None:
    reviews = [
        ReviewSubmission(
            review_id=123,
            author="review-bot",
            state="COMMENTED",
            commit_id=HEAD,
            submitted_at="2026-07-28T00:00:00Z",
            body="[P2] First edge case.\n\n[P2] Second edge case.",
        )
    ]
    ledger = overlay_backstop_evidence(
        _ledger_with_local_result(),
        threads=[],
        deferrals={
            "review:123:1": {
                "reason": "First has no supported-path reproduction.",
            }
        },
        reviews=reviews,
        reviewer_count=1,
    )

    report = evaluate(policy=_policy(), ledger=ledger)

    assert not report.settled
    assert len(ledger.current_findings) == 2
    assert {
        finding.disposition for finding in ledger.current_findings
    } == {Disposition.DEFER, None}
