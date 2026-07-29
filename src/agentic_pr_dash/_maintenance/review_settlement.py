"""GitHub adapter for provider-neutral review settlement."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence

from agent_review_coordinator import (
    Disposition,
    Finding,
    ReviewLedger,
    ReviewPolicy,
    ReviewResult,
    ReviewStage,
    SettlementReport,
    Severity,
    evaluate,
)
from pydantic import BaseModel, ConfigDict, Field

from agentic_pr_dash.maintenance import terminal_clean_blockers
from agentic_pr_dash.models import PRData

_SEVERITY_PREFIX = re.compile(
    r"^\s*(?:\*\*)?\[(P[12])\]\s*",
    re.IGNORECASE,
)


class FinalizationObservation(BaseModel):
    """One immutable reading of PR, CI, and review-settlement state."""

    model_config = ConfigDict(extra="forbid")

    repository: str
    head_sha: str
    clean: bool
    blockers: list[str] = Field(default_factory=list)
    review: SettlementReport


class FinalizationReport(BaseModel):
    """Stable completion result returned to agents and hooks."""

    model_config = ConfigDict(extra="forbid")

    settled: bool
    stable: bool
    repository: str
    head_sha: str
    observations: int
    blockers: list[str] = Field(default_factory=list)
    review: SettlementReport


def _thread_title(body: str) -> str:
    first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
    title = _SEVERITY_PREFIX.sub("", first_line).strip("* ")
    return title or "Unresolved GitHub review finding"


def finding_from_thread(
    thread,
    *,
    repository: str,
    head_sha: str,
    reviewer_execution_id: str,
) -> Finding:
    """Translate one unresolved GitHub thread onto the current snapshot."""

    body = thread.top.body or "Unresolved GitHub review finding"
    first_line = next(
        (line.strip() for line in body.splitlines() if line.strip()),
        "",
    )
    declared = _SEVERITY_PREFIX.match(first_line)
    line = (
        thread.top.line
        if thread.top.line is not None
        else thread.top.original_line
    )
    return Finding(
        repository=repository,
        head_sha=head_sha,
        reviewer_execution_id=reviewer_execution_id,
        reviewer_provider=thread.top.author or None,
        severity=(
            Severity.P1
            if declared and declared.group(1).upper() == "P1"
            else Severity.P2
        ),
        title=_thread_title(body),
        explanation=body,
        path=thread.top.path or ".github/pull-request",
        line=line,
        invariant=body,
        evidence=(
            f"GitHub review thread {thread.node_id}; "
            f"top-level comment {thread.top.database_id}"
        ),
    )


def finding_from_review_submission(
    review,
    *,
    repository: str,
    head_sha: str,
    reviewer_execution_id: str,
) -> Finding | None:
    """Translate a declared P1/P2 in a top-level GitHub review body."""

    declared_lines = [
        (line.strip(), match)
        for line in review.body.splitlines()
        if (match := _SEVERITY_PREFIX.match(line.strip()))
    ]
    if not declared_lines:
        return None
    first_line, _ = declared_lines[0]
    severity = (
        Severity.P1
        if any(match.group(1).upper() == "P1" for _, match in declared_lines)
        else Severity.P2
    )
    return Finding(
        repository=repository,
        head_sha=head_sha,
        reviewer_execution_id=reviewer_execution_id,
        reviewer_provider=review.author or None,
        severity=severity,
        title=_thread_title(first_line),
        explanation=review.body,
        path=".github/pull-request",
        invariant=review.body,
        evidence=f"GitHub top-level review {review.review_id}",
    )


def _github_execution_id(head_sha: str, threads: Sequence) -> str:
    identities = "\n".join(sorted(thread.node_id for thread in threads))
    digest = hashlib.sha256(identities.encode("utf-8")).hexdigest()[:16]
    return f"github-backstop:{head_sha}:{digest}"


def overlay_github_findings(
    ledger: ReviewLedger,
    *,
    threads: Sequence,
    deferrals: Mapping[str, Mapping[str, object]],
) -> ReviewLedger:
    """Overlay live GitHub findings without synthesizing an empty review run."""

    overlaid = ledger.model_copy(deep=True)
    if not threads:
        return overlaid

    execution_id = _github_execution_id(overlaid.head_sha, threads)
    findings = [
        finding_from_thread(
            thread,
            repository=overlaid.repository,
            head_sha=overlaid.head_sha,
            reviewer_execution_id=execution_id,
        )
        for thread in threads
    ]
    overlaid.submit(
        ReviewResult(
            repository=overlaid.repository,
            head_sha=overlaid.head_sha,
            stage=ReviewStage.BACKSTOP,
            round_number=1,
            slot_number=1,
            reviewer_execution_id=execution_id,
            reviewer_provider="github",
            findings=findings,
        )
    )
    for thread, submitted in zip(threads, findings, strict=True):
        record = deferrals.get(thread.node_id)
        if record is None:
            continue
        reason = str(record.get("reason") or "").strip()
        if not reason:
            continue
        overlaid.record_disposition(
            fingerprint=submitted.fingerprint,
            disposition=Disposition.DEFER,
            rationale=reason,
        )
    return overlaid


def overlay_backstop_results(
    ledger: ReviewLedger,
    *,
    reviews: Sequence,
    reviewer_count: int,
    thread_review_ids: set[int] | None = None,
) -> ReviewLedger:
    """Project current-head GitHub review submissions into backstop slots."""

    overlaid = ledger.model_copy(deep=True)
    existing_results = [
        result
        for result in overlaid.results
        if not result.stale and result.stage is ReviewStage.BACKSTOP
    ]
    occupied_slots = {result.slot_number for result in existing_results}
    execution_ids = {
        result.reviewer_execution_id for result in existing_results
    }
    available_slots = [
        slot_number
        for slot_number in range(1, reviewer_count + 1)
        if slot_number not in occupied_slots
    ]
    eligible_reviews = [
        review
        for review in reviews
        if review.review_id not in (thread_review_ids or set())
    ]
    for review, slot_number in zip(
        eligible_reviews,
        available_slots,
        strict=False,
    ):
        execution_id = f"github-review-{review.review_id}"
        if execution_id in execution_ids:
            continue
        body_finding = finding_from_review_submission(
            review,
            repository=overlaid.repository,
            head_sha=overlaid.head_sha,
            reviewer_execution_id=execution_id,
        )
        overlaid.submit(
            ReviewResult(
                repository=overlaid.repository,
                head_sha=overlaid.head_sha,
                stage=ReviewStage.BACKSTOP,
                round_number=1,
                slot_number=slot_number,
                reviewer_execution_id=execution_id,
                reviewer_provider=review.author,
                findings=[body_finding] if body_finding is not None else [],
            )
        )
        execution_ids.add(execution_id)
    return overlaid


def _append_once(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


def evaluate_pr_snapshot(
    *,
    pr: PRData,
    policy: ReviewPolicy,
    ledger: ReviewLedger,
    threads: Sequence,
    deferrals: Mapping[str, Mapping[str, object]],
    review_submissions: Sequence = (),
) -> FinalizationObservation:
    """Combine one live PR snapshot with provider-neutral review settlement."""

    repository = pr.repo.strip()
    blockers: list[str] = []
    if not repository:
        _append_once(blockers, "repository_unknown")
    elif repository != ledger.repository:
        _append_once(blockers, "ledger_repository_mismatch")

    head_sha = pr.latest_commit_sha.strip()
    if not head_sha:
        _append_once(blockers, "head_unknown")

    # Review findings below replace PRData.review_comments as the authoritative
    # thread gate. Keeping both would make an explicitly evaluated P2 block via
    # the legacy generic "review_comments" signal.
    readiness_pr = pr.model_copy(update={"review_comments": []}, deep=True)
    for blocker in terminal_clean_blockers(
        readiness_pr,
        validated_head=ledger.head_sha,
    ):
        _append_once(blockers, blocker)

    if pr.review_decision.upper() == "CHANGES_REQUESTED":
        _append_once(blockers, "changes_requested")
    if pr.is_draft:
        _append_once(blockers, "draft")
    if pr.mergeable.upper() != "MERGEABLE":
        _append_once(blockers, "not_mergeable")
    if not pr.ci_checks:
        _append_once(blockers, "ci_unavailable")
    elif any(
        check.status == "completed"
        and (check.conclusion or "").lower()
        not in {"success", "neutral", "skipped"}
        for check in pr.ci_checks
    ):
        _append_once(blockers, "ci_not_successful")

    current_threads = [thread for thread in threads if not thread.is_resolved]
    settled_ledger = overlay_github_findings(
        ledger,
        threads=current_threads,
        deferrals=deferrals,
    )
    settled_ledger = overlay_backstop_results(
        settled_ledger,
        reviews=review_submissions,
        reviewer_count=policy.review.backstop.reviewer_count,
        thread_review_ids={
            thread.top.review_id
            for thread in current_threads
            if thread.top.review_id is not None
        },
    )
    review = evaluate(policy=policy, ledger=settled_ledger)
    return FinalizationObservation(
        repository=repository or ledger.repository,
        head_sha=head_sha,
        clean=not blockers and review.settled,
        blockers=blockers,
        review=review,
    )


def _observation_key(observation: FinalizationObservation) -> str:
    payload = observation.model_dump_json(exclude={"clean"})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def combine_clean_observations(
    first: FinalizationObservation,
    second: FinalizationObservation | None,
) -> FinalizationReport:
    """Require two identical clean observations before declaring settlement."""

    if second is None:
        blockers = list(first.blockers)
        if first.clean:
            _append_once(blockers, "stabilization_pending")
        return FinalizationReport(
            settled=False,
            stable=False,
            repository=first.repository,
            head_sha=first.head_sha,
            observations=1,
            blockers=blockers,
            review=first.review,
        )

    stable = _observation_key(first) == _observation_key(second)
    blockers = list(second.blockers)
    if not stable:
        _append_once(blockers, "unstable_observation")
    return FinalizationReport(
        settled=first.clean and second.clean and stable,
        stable=stable,
        repository=second.repository,
        head_sha=second.head_sha,
        observations=2,
        blockers=blockers,
        review=second.review,
    )
