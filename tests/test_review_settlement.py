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
    overlay_backstop_results,
    overlay_github_findings,
)
from agentic_pr_dash.github_api import (
    ReviewSubmission,
    ReviewThread,
    ReviewThreadComment,
)


def test_review_coordinator_contract_version() -> None:
    assert __version__ == "0.1.0"


REPOSITORY = "Boundless-Studios/gaia-free"
HEAD = "a" * 40


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


def _ledger_with_local_result() -> ReviewLedger:
    ledger = ReviewLedger(repository=REPOSITORY, head_sha=HEAD)
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


def test_overlay_applies_explicit_p2_deferral_without_tracker_write() -> None:
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
    assert evaluate(policy=_policy(), ledger=ledger).settled


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
    ledger = overlay_github_findings(
        _ledger_with_local_result(),
        threads=[_thread(review_id=123)],
        deferrals={
            "PRRT_one": {
                "reason": "Evaluated and deferred.",
                "ticket": "",
            }
        },
    )
    ledger = overlay_backstop_results(
        ledger,
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
        thread_review_ids={123},
    )

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
