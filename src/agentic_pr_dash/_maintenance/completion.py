"""Completion-phase helpers — thread resolution and completion reply."""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from agent_review_coordinator import (
    ArchitectureDecision,
    ArchitectureDecisionKind,
    Disposition,
    Finding,
    FindingSettlementState,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# A token that looks like a Python module path or a concrete source filename.
_FILE_REF_RE = re.compile(
    r"""
    (?:
        # Backtick/quote-wrapped or bare path with a source extension.
        (?P<path>[\w./-]+\.(?:py|pyi|ts|tsx|js|jsx|gd|go|rs|java|kt|rb|c|h|cpp|hpp|sql|sh))
      |
        # Dotted module path with >=2 segments, e.g. gaia.api.worker_app.
        (?P<module>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*){1,})(?![\w(])
    )
    """,
    re.VERBOSE,
)

# Module-ish tokens that are almost always prose, not real module references.
_MODULE_REF_STOPWORDS = frozenset({"e.g", "i.e", "etc"})

# BOU-2095: how far (in lines) a changed hunk may sit from a thread's anchored
# line and still count as evidence that the comment's subject was edited. Three
# lines matches the diff context GitHub shows around an inline comment.
_ANCHOR_CONTEXT_LINES = 3


class SettlementReplyMetadata(BaseModel):
    """Machine-readable fields carried by one visible settlement reply."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    head_sha: str = Field(min_length=1)
    finding_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: Disposition
    rationale: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    fixing_commit: str | None = None
    duplicate_of: str | None = None
    deferred_to_issue: str | None = None
    architecture_decision: ArchitectureDecisionKind | None = None
    architecture_lineage_id: str | None = None
    architecture_decided_by: str | None = None
    architecture_rationale: str | None = None


@dataclass(frozen=True)
class PolicyFindingClosure:
    """One finding classified by the coordinator's settlement evaluator."""

    finding: Finding
    state: FindingSettlementState
    architecture_decision: ArchitectureDecision | None = None

    @property
    def addressed(self) -> bool:
        return self.state is not FindingSettlementState.UNRESOLVED

    @property
    def resolve_thread(self) -> bool:
        return self.state is FindingSettlementState.FIXED


class SettlementReplyStatus(StrEnum):
    """Visibility state of the current policy disposition on a review thread."""

    MISSING = "missing"
    FRESH = "fresh"
    REOPENED = "reopened"


@dataclass(frozen=True)
class ThreadSettlementOutcome:
    """Result of publishing one policy closure to a provider thread."""

    reply_visible: bool
    reply_posted: bool
    resolved: bool
    actionable: bool


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _settlement_evidence(
    finding: Finding,
    *,
    fixing_commit: str | None,
    architecture_decision: ArchitectureDecision | None,
) -> str:
    evidence: list[str] = []
    if finding.evidence and finding.evidence.strip():
        evidence.append(_one_line(finding.evidence))
    evidence.extend(
        f"{artifact.key}: {_one_line(artifact.summary)}"
        for artifact in finding.evidence_artifacts
    )
    if finding.p2_evidence is not None:
        evidence.append(
            "p2_evidence="
            + json.dumps(
                finding.p2_evidence.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    if finding.reproduction and finding.reproduction.strip():
        evidence.append(f"reproduction={_one_line(finding.reproduction)}")
    if not evidence:
        if finding.verification_passed:
            evidence.append("verification_passed=true")
        if fixing_commit:
            evidence.append(f"fixing_commit={fixing_commit}")
        if finding.duplicate_of:
            evidence.append(f"duplicate_of={finding.duplicate_of}")
        if finding.deferred_to_issue:
            evidence.append(f"existing_issue={finding.deferred_to_issue}")
        if architecture_decision is not None:
            evidence.append(
                "architecture_decision="
                f"{architecture_decision.decision.value};"
                f"decided_by={architecture_decision.decided_by}"
            )
    if not evidence:
        raise ValueError("structured settlement reply requires evidence")
    return "; ".join(evidence)


def structured_settlement_reply_body(
    *,
    marker: str,
    finding: Finding,
    head_sha: str,
    fixing_commit: str | None = None,
    architecture_decision: ArchitectureDecision | None = None,
) -> str:
    """Render a deterministic, parseable, evidence-backed policy reply."""

    if finding.head_sha != head_sha:
        raise ValueError("settlement reply head does not match finding snapshot")
    if finding.disposition is None:
        raise ValueError("settlement reply requires a disposition")
    if not finding.rationale or not finding.rationale.strip():
        raise ValueError("settlement reply requires a rationale")
    if finding.disposition is Disposition.FIXED:
        if not finding.verification_passed:
            raise ValueError("fixed settlement reply requires passed verification")
        if not fixing_commit:
            raise ValueError("fixed settlement reply requires a fixing commit")
    if finding.disposition is Disposition.DUPLICATE and not finding.duplicate_of:
        raise ValueError("duplicate settlement reply requires duplicate_of")
    if (
        finding.disposition is Disposition.DEFERRED_TO_EXISTING_ISSUE
        and not finding.deferred_to_issue
    ):
        raise ValueError("deferred settlement reply requires an existing issue")
    if (
        architecture_decision is not None
        and architecture_decision.lineage_id != finding.lineage_id
    ):
        raise ValueError("architecture decision does not match finding lineage")

    evidence = _settlement_evidence(
        finding,
        fixing_commit=fixing_commit,
        architecture_decision=architecture_decision,
    )
    lines = [
        marker,
        "Review settlement:",
        f"- Head SHA: `{head_sha}`",
        f"- Finding fingerprint: `{finding.fingerprint}`",
        f"- Disposition: `{finding.disposition.value}`",
        f"- Rationale: {_one_line(finding.rationale)}",
        f"- Evidence: {evidence}",
    ]
    if fixing_commit:
        lines.append(f"- Fixing commit: `{fixing_commit}`")
    if finding.duplicate_of:
        lines.append(f"- Duplicate of: `{finding.duplicate_of}`")
    if finding.deferred_to_issue:
        lines.append(f"- Existing issue: `{finding.deferred_to_issue}`")
    if architecture_decision is not None:
        lines.extend(
            [
                (
                    "- Architecture decision: "
                    f"`{architecture_decision.decision.value}`"
                ),
                f"- Architecture lineage: `{architecture_decision.lineage_id}`",
                f"- Architecture decided by: `{architecture_decision.decided_by}`",
                (
                    "- Architecture rationale: "
                    f"{_one_line(architecture_decision.rationale)}"
                ),
            ]
        )
    return "\n".join(lines)


def _reply_field(body: str, label: str, *, quoted: bool = False) -> str | None:
    prefix = f"- {label}: "
    value = next(
        (line[len(prefix):] for line in body.splitlines() if line.startswith(prefix)),
        None,
    )
    if value is None:
        return None
    if quoted:
        if len(value) < 2 or not (value.startswith("`") and value.endswith("`")):
            return None
        value = value[1:-1]
    return value.strip() or None


def parse_structured_settlement_reply(body: str) -> SettlementReplyMetadata | None:
    """Parse a reply rendered by :func:`structured_settlement_reply_body`."""

    if "Review settlement:" not in body.splitlines():
        return None
    try:
        return SettlementReplyMetadata(
            head_sha=_reply_field(body, "Head SHA", quoted=True) or "",
            finding_fingerprint=(
                _reply_field(body, "Finding fingerprint", quoted=True) or ""
            ),
            disposition=Disposition(
                _reply_field(body, "Disposition", quoted=True) or ""
            ),
            rationale=_reply_field(body, "Rationale") or "",
            evidence=_reply_field(body, "Evidence") or "",
            fixing_commit=_reply_field(body, "Fixing commit", quoted=True),
            duplicate_of=_reply_field(body, "Duplicate of", quoted=True),
            deferred_to_issue=_reply_field(body, "Existing issue", quoted=True),
            architecture_decision=(
                ArchitectureDecisionKind(value)
                if (
                    value := _reply_field(
                        body, "Architecture decision", quoted=True
                    )
                )
                else None
            ),
            architecture_lineage_id=_reply_field(
                body, "Architecture lineage", quoted=True
            ),
            architecture_decided_by=_reply_field(
                body, "Architecture decided by", quoted=True
            ),
            architecture_rationale=_reply_field(body, "Architecture rationale"),
        )
    except (ValidationError, ValueError):
        return None


def _reply_matches_closure(
    metadata: SettlementReplyMetadata,
    closure: PolicyFindingClosure,
) -> bool:
    finding = closure.finding
    decision = closure.architecture_decision
    return all(
        (
            metadata.head_sha == finding.head_sha,
            metadata.finding_fingerprint == finding.fingerprint,
            metadata.disposition is finding.disposition,
            metadata.rationale == _one_line(finding.rationale or ""),
            metadata.duplicate_of == finding.duplicate_of,
            metadata.deferred_to_issue == finding.deferred_to_issue,
            metadata.architecture_decision
            == (decision.decision if decision is not None else None),
            metadata.architecture_lineage_id
            == (decision.lineage_id if decision is not None else None),
            metadata.architecture_decided_by
            == (decision.decided_by if decision is not None else None),
            metadata.architecture_rationale
            == (_one_line(decision.rationale) if decision is not None else None),
        )
    )


def _reply_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (AttributeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def settlement_reply_status(
    thread,  # type: ignore[no-untyped-def]
    closure: PolicyFindingClosure,
) -> SettlementReplyStatus:
    """Return whether the exact current closure is visible after reviewer input."""

    matching_reply_times: list[datetime] = []
    reviewer_times = [_reply_timestamp(thread.top.created_at)]
    for reply in thread.replies:
        metadata = parse_structured_settlement_reply(reply.body)
        timestamp = _reply_timestamp(reply.created_at)
        if (
            metadata is not None
            and timestamp is not None
            and _reply_matches_closure(metadata, closure)
        ):
            matching_reply_times.append(timestamp)
        elif metadata is None:
            # Any later non-settlement response is new review input. The
            # responder need not be the thread's original author: another
            # reviewer can reopen a previously addressed finding.
            reviewer_times.append(timestamp)
    if not matching_reply_times:
        return SettlementReplyStatus.MISSING

    if any(timestamp is None for timestamp in reviewer_times):
        return SettlementReplyStatus.REOPENED
    latest_reviewer = max(
        timestamp for timestamp in reviewer_times if timestamp is not None
    )
    if max(matching_reply_times) > latest_reviewer:
        return SettlementReplyStatus.FRESH
    return SettlementReplyStatus.REOPENED


def apply_thread_settlement(
    *,
    thread,  # type: ignore[no-untyped-def]
    closure: PolicyFindingClosure,
    marker: str,
    head_sha: str,
    fixing_commit: str | None,
    reply: Callable[[str], bool],
    resolve: Callable[[], bool],
) -> ThreadSettlementOutcome:
    """Publish a coordinator closure, resolving only a verified fixed finding."""

    if not closure.addressed:
        return ThreadSettlementOutcome(False, False, False, True)

    status = settlement_reply_status(thread, closure)
    if status is SettlementReplyStatus.REOPENED:
        return ThreadSettlementOutcome(False, False, False, True)

    reply_posted = False
    if status is SettlementReplyStatus.MISSING:
        body = structured_settlement_reply_body(
            marker=marker,
            finding=closure.finding,
            head_sha=head_sha,
            fixing_commit=fixing_commit,
            architecture_decision=closure.architecture_decision,
        )
        try:
            reply_visible = bool(reply(body))
        except Exception:  # noqa: BLE001
            reply_visible = False
        if not reply_visible:
            return ThreadSettlementOutcome(False, False, False, True)
        reply_posted = True

    if not closure.resolve_thread:
        return ThreadSettlementOutcome(True, reply_posted, False, False)
    try:
        resolved = bool(resolve())
    except Exception:  # noqa: BLE001
        resolved = False
    return ThreadSettlementOutcome(True, reply_posted, resolved, not resolved)


def _spans_intersect_line(
    spans: list[tuple[int, int, int, int]],
    anchor_line: int,
    side: str,
) -> bool:
    """True if a changed hunk touches ``anchor_line`` (± context fuzz) on ``side``.

    ``spans`` come from :func:`agentic_pr_dash.github_api.get_changed_line_spans`
    — ``(old_start, old_end, new_start, new_end)`` per hunk, where an empty side
    is encoded as ``end < start``. ``side`` names the diff side the anchor
    coordinate lives on: a non-outdated thread's ``line`` is head-side
    (``"new"``); an outdated thread's ``original_line`` is base-side
    (``"old"``). Comparing a coordinate against the WRONG side let unrelated
    hunks masquerade as evidence — e.g. a deletion above a live thread whose
    old-side numbers happen to overlap the head-side anchor (PR #78 review).
    """
    for old_start, old_end, new_start, new_end in spans:
        start, end = (
            (new_start, new_end) if side == "new" else (old_start, old_end)
        )
        if end < start:  # empty side: pure insertion/deletion counterpart
            continue
        if start - _ANCHOR_CONTEXT_LINES <= anchor_line <= end + _ANCHOR_CONTEXT_LINES:
            return True
    return False


def _thread_completion_evidence(
    thread,  # type: ignore[no-untyped-def]
    spans: list[tuple[int, int, int, int]] | None,
) -> str | None:
    """Positive per-thread evidence that the completing commits addressed THIS thread.

    BOU-2095: "the anchor file was touched" / "GitHub marks the thread
    outdated" is NOT evidence — line drift and unrelated same-file edits were
    silently resolving live feedback. Returns the evidence kind, or ``None``
    when there is none (caller must leave the thread OPEN):

    - ``"reply"``  — a completion-marker reply is the thread's TERMINAL state
      per :func:`agentic_pr_dash.github_api._thread_state` (an earlier
      `complete` run replied but the resolve mutation failed). A marker
      followed by a human follow-up is ``reopened`` — the reviewer rejected
      the completion — so a merely-historical marker is NOT evidence
      (PR #78 review);
    - ``"hunk"``   — the completing commits changed content at the thread's
      anchored line (± :data:`_ANCHOR_CONTEXT_LINES`), compared on the diff
      side the coordinate lives on (head-side ``line`` vs base-side
      ``original_line``);
    - ``"file"``   — the thread is file-level (no line anchor at all) and the
      file's content changed; the whole file IS the anchor span.

    A terminal marker followed by non-marker human feedback is ``reopened``.
    Reopened threads always return ``None``: commit timestamps and inferred diff
    coordinates cannot prove that the reviewer accepted a later attempt, so
    automation must leave the thread open for manual confirmation.

    ``spans is None`` means the base..head diff was unavailable — that is
    absence of proof, never proof of absence, so it yields ``None``.
    """
    from agentic_pr_dash.github_api import _thread_state

    replies_as_dicts = [
        {"body": r.body, "created_at": r.created_at, "author": r.author}
        for r in thread.replies
    ]
    state, _ = _thread_state(replies_as_dicts, top_author=thread.top.author)
    if state == "completed":
        return "reply"
    if state == "reopened":
        return None
    anchor_line = thread.top.line
    anchor_side = "new"  # non-outdated `line` is a head-side coordinate
    if anchor_line is None:
        anchor_line = thread.top.original_line
        anchor_side = "old"  # `original_line` is a base-side coordinate
    if anchor_line is None:
        return "file" if spans else None
    if spans and _spans_intersect_line(spans, anchor_line, anchor_side):
        return "hunk"
    return None


def _commit_subject(message: str) -> str:
    """First non-empty line of a commit message, trimmed."""
    for line in message.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return message.strip()


def _completion_reply_body(
    marker: str,
    path: str | None,
    commits_by_file: dict[str, list[tuple[str, str]]],
    all_commits: list[tuple[str, str]],
) -> str:
    """Build a substantive completion reply that cites the fixing commit(s)."""
    cites = commits_by_file.get(path, []) if path else []
    if not cites:
        cites = all_commits
    seen: set[str] = set()
    unique = [(sha, msg) for sha, msg in cites if not (sha in seen or seen.add(sha))]
    lines = [marker]
    if len(unique) == 1:
        sha, msg = unique[0]
        lines.append(f"Addressed by the local maintenance loop in {sha[:9]} — {_commit_subject(msg)}.")
    elif unique:
        lines.append("Addressed by the local maintenance loop in:")
        lines.extend(f"- `{sha[:9]}` {_commit_subject(msg)}" for sha, msg in unique[:6])
    else:
        lines.append("Addressed by the local maintenance loop.")
    return "\n".join(lines)


def _mark_maintenance_complete(maintenance, cwd: str, pr_number: int) -> None:  # type: ignore[no-untyped-def]
    """Best-effort: write COMPLETE to the on-disk maintenance state."""
    try:
        from agentic_pr_dash.models import MaintenanceStatus

        state = maintenance.load_state(cwd, pr_number)
        if state is not None:
            maintenance.mark_state(state, MaintenanceStatus.COMPLETE)
    except Exception:  # noqa: BLE001, S110 - completion bookkeeping is best-effort
        pass


def _review_comments_from_threads(threads) -> list:
    """Build ReviewComment records from review threads."""
    from agentic_pr_dash.models import ReviewComment

    out = []
    for t in threads:
        top = t.top
        out.append(ReviewComment(
            id=top.database_id,
            author=top.author,
            body=top.body,
            path=top.path,
            line=top.line,
            created_at=top.created_at,
            is_inline=True,
            thread_id=t.node_id,
        ))
    return out


def _candidate_file_refs(body: str) -> list[str]:
    """Extract file/module references from a review-thread body."""
    refs: list[str] = []
    body_without_urls = re.sub(r"(?:https?:)?//[^\s)>]+", "", body or "")
    for m in _FILE_REF_RE.finditer(body_without_urls):
        path = m.group("path")
        if path:
            refs.append(path)
            continue
        module = m.group("module")
        if module and module.lower() not in _MODULE_REF_STOPWORDS:
            refs.append(module)
    return refs


def _ref_matches_touched(ref: str, touched: set[str]) -> bool:
    """True if a body file/module reference plausibly matches a touched path."""
    if not touched:
        return False
    if "/" in ref or ref.endswith(
        (".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".gd", ".go", ".rs",
         ".java", ".kt", ".rb", ".c", ".h", ".cpp", ".hpp", ".sql", ".sh")
    ):
        base = ref.rsplit("/", 1)[-1]
        for t in touched:
            if t == ref or t.endswith("/" + ref) or t.rsplit("/", 1)[-1] == base:
                return True
        return False
    segments = ref.split(".")
    frag = "/".join(segments)
    last = segments[-1]
    for t in touched:
        no_ext = t.rsplit(".", 1)[0]
        if frag in no_ext:
            return True
        if no_ext.rsplit("/", 1)[-1] == last:
            return True
    return False


def _thread_elsewhere_refs(body: str, anchor_path: str | None,
                           touched: set[str]) -> list[str]:
    """Body file/module refs that point at something NOT among ``touched``.

    Returns the concrete references (in body order, de-duplicated) that neither
    match a touched path nor resolve to the thread's own anchor. An empty list
    means the body points only at touched files / the anchor — i.e. it does not
    point elsewhere. Surfacing the refs lets the caller name the specific
    conflicting paths when it declines to auto-resolve (BOU-1748).
    """
    anchor_base = anchor_path.rsplit("/", 1)[-1] if anchor_path else None
    elsewhere: list[str] = []
    for ref in _candidate_file_refs(body):
        if _ref_matches_touched(ref, touched):
            continue
        if anchor_base:
            ref_base = ref.rsplit("/", 1)[-1]
            if ref_base == anchor_base:
                continue
            if (
                "." in ref
                and not ref_base.endswith(
                    tuple(
                        f".{ext}"
                        for ext in (
                            "py", "pyi", "ts", "tsx", "js", "jsx", "gd", "go",
                            "rs", "java", "kt", "rb", "c", "h", "cpp", "hpp",
                            "sql", "sh",
                        )
                    )
                )
                and ref.split(".")[-1] == anchor_base.rsplit(".", 1)[0]
            ):
                continue
        if ref not in elsewhere:
            elsewhere.append(ref)
    return elsewhere


def _thread_points_elsewhere(body: str, anchor_path: str | None,
                             touched: set[str]) -> bool:
    """True if the thread body references a file/module NOT among ``touched``."""
    return bool(_thread_elsewhere_refs(body, anchor_path, touched))
