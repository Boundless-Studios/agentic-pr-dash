"""GitHub adapter for provider-neutral review settlement."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence

from agent_review_coordinator import (
    Disposition,
    Finding,
    ReviewLedger,
    ReviewResult,
    ReviewStage,
    Severity,
)

from .pr_state import _thread_is_p1

_SEVERITY_PREFIX = re.compile(r"^\s*(?:\*\*)?\[P[12]\]\s*", re.IGNORECASE)


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
        severity=Severity.P1 if _thread_is_p1(thread) else Severity.P2,
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
