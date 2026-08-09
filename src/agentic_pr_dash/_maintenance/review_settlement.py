"""GitHub adapter for provider-neutral review settlement."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

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
    r"^\s*(?:\*\*)?\[(P[0-3])\]\s*",
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


def declared_review_body_lines(body: str) -> list[str]:
    """Return each line with a severity declared by the typed contract."""

    return [
        line.strip()
        for line in body.splitlines()
        if _SEVERITY_PREFIX.match(line.strip())
    ]


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
            Severity(declared.group(1).lower())
            if declared
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


def findings_from_review_submission(
    review,
    *,
    repository: str,
    head_sha: str,
    reviewer_execution_id: str,
) -> list[Finding]:
    """Translate each typed severity declaration in a GitHub review body."""

    declared_lines = [
        (line, _SEVERITY_PREFIX.match(line))
        for line in declared_review_body_lines(review.body)
    ]
    return [
        Finding(
            repository=repository,
            head_sha=head_sha,
            reviewer_execution_id=reviewer_execution_id,
            reviewer_provider=review.author or None,
            severity=Severity(match.group(1).lower()),
            title=_thread_title(line),
            explanation=line,
            path=".github/pull-request",
            invariant=f"Review declaration {ordinal}: {line}",
            evidence=(
                f"GitHub top-level review {review.review_id}; "
                f"declaration {ordinal}"
            ),
        )
        for ordinal, (line, match) in enumerate(declared_lines, start=1)
    ]


def finding_from_review_submission(
    review,
    *,
    repository: str,
    head_sha: str,
    reviewer_execution_id: str,
) -> Finding | None:
    """Compatibility helper for callers expecting one declared finding."""

    findings = findings_from_review_submission(
        review,
        repository=repository,
        head_sha=head_sha,
        reviewer_execution_id=reviewer_execution_id,
    )
    return findings[0] if findings else None


def _github_execution_id(head_sha: str, threads: Sequence) -> str:
    identities = "\n".join(sorted(thread.node_id for thread in threads))
    digest = hashlib.sha256(identities.encode("utf-8")).hexdigest()[:16]
    return f"github-backstop:{head_sha}:{digest}"


@dataclass
class _BackstopEvidence:
    provider: str
    qualifies_for_slot: bool
    findings: list[Finding] = field(default_factory=list)
    disposition_keys: list[tuple[str, str]] = field(default_factory=list)


def _record_deferred_findings(
    ledger: ReviewLedger,
    *,
    evidence: _BackstopEvidence,
    deferrals: Mapping[str, Mapping[str, object]],
) -> None:
    for disposition_key, fingerprint in evidence.disposition_keys:
        record = deferrals.get(disposition_key)
        if record is None:
            continue
        reason = str(record.get("reason") or "").strip()
        if not reason:
            continue
        ledger.record_disposition(
            fingerprint=fingerprint,
            disposition=Disposition.DEFER,
            rationale=reason,
        )


def overlay_backstop_evidence(
    ledger: ReviewLedger,
    *,
    threads: Sequence,
    deferrals: Mapping[str, Mapping[str, object]],
    reviews: Sequence,
    reviewer_count: int,
) -> ReviewLedger:
    """Project all live findings and each completed review exactly once.

    Completed current-head reviews qualify for configured backstop slots.
    Findings from older or uncorrelated threads are retained in an overflow
    slot, so they block settlement without masquerading as current-head review
    evidence. Additional reviews after quorum are also retained there because
    quorum must never suppress a later finding.
    """

    overlaid = ledger.model_copy(deep=True)
    evidence_by_execution: dict[str, _BackstopEvidence] = {}

    for review in reviews:
        execution_id = f"github-{review.source}-{review.review_id}"
        evidence = evidence_by_execution.setdefault(
            execution_id,
            _BackstopEvidence(
                provider=review.author,
                qualifies_for_slot=True,
            ),
        )
        body_findings = findings_from_review_submission(
            review,
            repository=overlaid.repository,
            head_sha=overlaid.head_sha,
            reviewer_execution_id=execution_id,
        )
        evidence.findings.extend(body_findings)
        for ordinal, body_finding in enumerate(body_findings, start=1):
            disposition_key = (
                f"review:{review.review_id}"
                if len(body_findings) == 1
                else f"review:{review.review_id}:{ordinal}"
            )
            evidence.disposition_keys.append(
                (disposition_key, body_finding.fingerprint)
            )

    current_review_ids = {review.review_id for review in reviews}
    for thread in threads:
        review_id = thread.top.review_id
        if review_id is not None:
            execution_id = f"github-review-{review_id}"
        else:
            execution_id = _github_execution_id(overlaid.head_sha, [thread])
        evidence = evidence_by_execution.setdefault(
            execution_id,
            _BackstopEvidence(
                provider=thread.top.author or "github",
                qualifies_for_slot=review_id in current_review_ids,
            ),
        )
        finding = finding_from_thread(
            thread,
            repository=overlaid.repository,
            head_sha=overlaid.head_sha,
            reviewer_execution_id=execution_id,
        )
        evidence.findings.append(finding)
        evidence.disposition_keys.append((thread.node_id, finding.fingerprint))

    existing_results = [
        result
        for result in overlaid.results
        if not result.stale and result.stage is ReviewStage.BACKSTOP
    ]
    existing_slots = {
        result.reviewer_execution_id: result.slot_number
        for result in existing_results
    }
    occupied_slots = {
        result.slot_number
        for result in existing_results
        if result.slot_number <= reviewer_count
    }
    available_slots = [
        slot_number
        for slot_number in range(1, reviewer_count + 1)
        if slot_number not in occupied_slots
    ]
    overflow_slot = reviewer_count + 1

    for execution_id, evidence in evidence_by_execution.items():
        existing_slot = existing_slots.get(execution_id)
        if existing_slot is not None:
            slot_number = existing_slot
        elif evidence.qualifies_for_slot and available_slots:
            slot_number = available_slots.pop(0)
        else:
            slot_number = overflow_slot
        if existing_slot is None or evidence.findings:
            overlaid.submit(
                ReviewResult(
                    repository=overlaid.repository,
                    head_sha=overlaid.head_sha,
                    stage=ReviewStage.BACKSTOP,
                    round_number=1,
                    slot_number=slot_number,
                    reviewer_execution_id=execution_id,
                    reviewer_provider=evidence.provider,
                    findings=evidence.findings,
                )
            )
        _record_deferred_findings(
            overlaid,
            evidence=evidence,
            deferrals=deferrals,
        )
    return overlaid


def overlay_github_findings(
    ledger: ReviewLedger,
    *,
    threads: Sequence,
    deferrals: Mapping[str, Mapping[str, object]],
) -> ReviewLedger:
    """Overlay live GitHub findings without synthesizing an empty review run."""

    return overlay_backstop_evidence(
        ledger,
        threads=threads,
        deferrals=deferrals,
        reviews=[],
        reviewer_count=1,
    )


def overlay_backstop_results(
    ledger: ReviewLedger,
    *,
    reviews: Sequence,
    reviewer_count: int,
    thread_review_ids: set[int] | None = None,
) -> ReviewLedger:
    """Project current-head GitHub review submissions into backstop slots."""

    eligible_reviews = [
        review
        for review in reviews
        if review.review_id not in (thread_review_ids or set())
    ]
    return overlay_backstop_evidence(
        ledger,
        threads=[],
        deferrals={},
        reviews=eligible_reviews,
        reviewer_count=reviewer_count,
    )


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
    readiness_blockers = terminal_clean_blockers(
        readiness_pr, validated_head=ledger.head_sha
    )
    if pr.is_draft:
        # Drafts are not ship candidates: do not make their intentionally
        # incomplete CI/merge state a settlement blocker. Head drift remains a
        # blocker because it invalidates every other observation.
        readiness_blockers = [
            blocker for blocker in readiness_blockers if blocker == "head_drift"
        ]
    for blocker in readiness_blockers:
        _append_once(blockers, blocker)

    if not pr.is_draft:
        if pr.review_decision.upper() == "CHANGES_REQUESTED":
            _append_once(blockers, "changes_requested")
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
    settled_ledger = overlay_backstop_evidence(
        ledger,
        threads=current_threads,
        deferrals=deferrals,
        reviews=review_submissions,
        reviewer_count=policy.review.backstop.reviewer_count,
    )
    review = evaluate(policy=policy, ledger=settled_ledger)
    if pr.is_draft:
        # A draft never becomes a ship candidate, so its backstop quorum is not
        # applicable. Keep local-slot and finding actions intact.
        missing_slots = [
            slot
            for slot in review.missing_slots
            if not slot.startswith(f"{ReviewStage.BACKSTOP.value}:")
        ]
        review = review.model_copy(
            update={
                "settled": not missing_slots and not review.required_actions,
                "missing_slots": missing_slots,
            }
        )
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
