"""Semantic guardrails for `complete`'s review-thread auto-resolution.

BOU-1641 — `complete` must not auto-resolve a review thread solely because a
post-baseline commit touched the thread's ANCHOR file, when the comment body
requests a change in a DIFFERENT (untouched) file/module.

The confirmed symptom (gaia PR #2139): a thread anchored on
`backend/src/gaia/api/app.py` asked for a fix in `gaia.api.worker_app`
(`worker_app.py`). An unrelated commit touched `app.py`, so the old
`path in touched` heuristic auto-resolved the thread before `worker_app.py` was
ever fixed — stranding real feedback behind a resolved marker.

BOU-2095 — beyond BOU-1641, "anchor file touched" is not sufficient evidence at
all: resolution now requires the completing commits to have changed the
thread's ANCHORED HUNK (base..head diff intersects the anchor line ± context),
or an explicit completion-marker reply on the thread. GitHub's "outdated" flag
(pure line drift) never resolves anything, and the former BOU-1748
"anchor touched AND outdated" widening is removed.
"""

import argparse
from pathlib import Path

import pytest
from agent_review_coordinator import (
    ArchitectureDecision,
    ArchitectureDecisionKind,
    Disposition,
    Finding,
    FindingSettlementState,
    ReviewLedger,
    ReviewPolicy,
    ReviewResult,
    ReviewStage,
    Severity,
)

from agentic_pr_dash import github_api, maintenance
from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash._maintenance import completion, review_settlement
from agentic_pr_dash.github_api import (
    COMPLETE_MARKER,
    ReviewThread,
    ReviewThreadComment,
)
from agentic_pr_dash.models import PRData, PRStatus

ANCHOR = "backend/src/gaia/api/app.py"
REPOSITORY = "Boundless-Studios/agentic-pr-dash"

# Spans intersecting the default anchor line (7): the fixing commits changed
# the thread's own hunk — the genuine-fix shape.
SPANS_AT_ANCHOR = [(6, 8, 6, 9)]
# Spans far from the default anchor line: same file touched, different hunk.
SPANS_ELSEWHERE_IN_FILE = [(100, 110, 100, 112)]


def _thread(body, *, path=ANCHOR, created="2026-01-01T00:00:00Z", outdated=False,
            line=7, original_line=None, replies=(), node_id="t1"):
    c = ReviewThreadComment(
        database_id=42, path=path, line=line, body=body,
        author="rev", created_at=created, original_line=original_line,
    )
    reply_comments = [
        ReviewThreadComment(
            database_id=43 + i, path=path, line=line, body=reply_body,
            author="bot", created_at=created,
        )
        for i, reply_body in enumerate(replies)
    ]
    return ReviewThread(node_id=node_id, is_resolved=False, is_outdated=outdated,
                        top=c, replies=reply_comments)


def _pr():
    return PRData(
        number=2139, repo=REPOSITORY, title="t", branch="b",
        url="https://x/pull/2139",
        failing_checks=[], review_comments=[], merge_state="CLEAN",
        latest_commit_sha="headsha", latest_commit_date="2026-02-01T00:00:00Z",
        worktree_path="/wt", status=PRStatus.CLEAN,
    )


def _wire(monkeypatch, *, thread, touched_files, spans=SPANS_AT_ANCHOR,
          threads=None, resolve_result=True, reply_result=True, events=None,
          commits=None, files_by_commit=None, commit_dates=None,
          thread_snapshots=None, mutation_author="maintenance-bot"):
    """Stub the gh/GraphQL boundary so `_cmd_complete` runs offline.

    Records every `resolve_review_thread` call into ``resolved`` and every
    completion reply into ``replied`` so a test can assert whether the thread
    was (or was not) auto-resolved / replied to. ``spans`` is what the
    base..head diff reports as changed hunks for ANY anchor path (default: the
    fix changed the default anchor line's own hunk); pass ``None`` to model an
    unavailable diff.
    """
    resolved_calls: list[str] = []
    reply_calls: list[object] = []

    monkeypatch.setattr(mc, "_resolve_pr_by_number", lambda n, cwd, **kw: _pr())
    # No local-head override: keep the API head/date.
    monkeypatch.setattr(github_api, "get_local_pr_head", lambda branch, cwd: ("", ""))
    monkeypatch.setattr(github_api, "_is_ancestor", lambda a, d, cwd: False)
    # One post-baseline commit exists (a real fixing push landed) ...
    effective_commits = commits or [("c0ffee", "fix: logging")]
    monkeypatch.setattr(
        github_api, "get_new_pr_commits",
        lambda *a, **k: list(effective_commits),
    )
    # ... and it touched exactly `touched_files`.
    monkeypatch.setattr(
        github_api, "get_commit_changed_files",
        lambda sha, cwd=None: list(
            files_by_commit[sha] if files_by_commit is not None
            else touched_files
        ),
    )
    effective_commit_dates = commit_dates or {
        sha: "2026-02-01T00:00:00Z" for sha, _message in effective_commits
    }
    monkeypatch.setattr(
        github_api,
        "get_commit_date",
        lambda sha, cwd=None: effective_commit_dates.get(sha, ""),
    )
    # ... changing exactly `spans` (hunk line ranges) in each touched file.
    monkeypatch.setattr(
        github_api, "get_changed_line_spans",
        lambda base, head, path, cwd=None: None if spans is None else list(spans),
    )
    all_threads = threads if threads is not None else [thread]
    snapshots = list(thread_snapshots or ())
    thread_read_count = 0

    def _get_review_threads(n, cwd=None, **kwargs):
        nonlocal thread_read_count
        if snapshots:
            index = min(thread_read_count, len(snapshots) - 1)
            thread_read_count += 1
            return list(snapshots[index])
        return list(all_threads)

    monkeypatch.setattr(github_api, "get_review_threads", _get_review_threads)

    def _resolve(node_id, cwd=None):
        if events is not None:
            events.append(("resolve", node_id))
        resolved_calls.append(node_id)
        return resolve_result

    def _reply(pr_number, comment, body, cwd=None):
        if events is not None:
            events.append(("reply", comment.thread_id))
        reply_calls.append((comment.thread_id, body))
        if reply_result and not snapshots:
            thread.replies.append(
                ReviewThreadComment(
                    database_id=9000 + len(reply_calls),
                    path=thread.top.path,
                    line=thread.top.line,
                    body=body,
                    author=mutation_author,
                    created_at="2026-02-02T00:00:00Z",
                )
            )
        return reply_result

    monkeypatch.setattr(github_api, "resolve_review_thread", _resolve)
    monkeypatch.setattr(github_api, "reply_to_review_comment", _reply)
    # Short-circuit the post-resolve bead bookkeeping.
    monkeypatch.setattr(mc, "_mark_maintenance_complete", lambda *a, **k: None)
    monkeypatch.setattr(maintenance, "blockers_for_pr", lambda pr: [])

    return resolved_calls, reply_calls


def _args(*, cwd="."):
    return argparse.Namespace(cwd=cwd, pr=2139, baseline="basesha")


def _write_policy_context(
    worktree: Path,
    thread: ReviewThread,
    disposition: Disposition,
) -> tuple[ReviewLedger, Finding]:
    policy = ReviewPolicy.model_validate(
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
    finding = review_settlement.finding_from_thread(
        thread,
        repository=REPOSITORY,
        head_sha="headsha",
        reviewer_execution_id="local-review",
    )
    ledger = ReviewLedger(
        repository=REPOSITORY,
        head_sha="headsha",
        delivery_id="delivery-pr-2139",
        review_charter_version="review-charter-v1",
    )
    ledger.submit(
        ReviewResult(
            repository=REPOSITORY,
            head_sha="headsha",
            stage=ReviewStage.LOCAL,
            round_number=1,
            slot_number=1,
            reviewer_execution_id="local-review",
            findings=[finding],
        )
    )
    ledger.submit(
        ReviewResult(
            repository=REPOSITORY,
            head_sha="headsha",
            stage=ReviewStage.BACKSTOP,
            round_number=1,
            slot_number=1,
            reviewer_execution_id="backstop-review",
        )
    )
    ledger.record_disposition(
        fingerprint=finding.fingerprint,
        disposition=disposition,
        rationale="The policy evidence supports this recorded outcome.",
        evidence="targeted lifecycle boundary reproduction",
    )
    if disposition is Disposition.FIXED:
        ledger.record_verification(fingerprint=finding.fingerprint, passed=True)

    policy_path = worktree / "config" / "review-policy.yaml"
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text(
        "version: 1\n"
        "review:\n"
        "  local:\n"
        "    reviewer_count: 1\n"
        "  backstop:\n"
        "    reviewer_count: 1\n"
        "    trigger: new_head_sha\n",
        encoding="utf-8",
    )
    ledger_path = worktree / ".agentic-review" / "ledger.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(ledger.model_dump_json(), encoding="utf-8")
    (worktree / "agentic-pr-dash.toml").write_text(
        'maintenance_mutation_identity = "maintenance-bot"\n',
        encoding="utf-8",
    )
    assert policy == ReviewPolicy.from_yaml(policy_path.read_text(encoding="utf-8"))
    return ledger, finding


def test_complete_production_path_publishes_fixed_closure_before_resolve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    thread = _thread("[P1] Preserve the lifecycle fence")
    _, finding = _write_policy_context(tmp_path, thread, Disposition.FIXED)
    events: list[tuple[str, str]] = []
    fixing_commit = "c" * 40
    resolved, replied = _wire(
        monkeypatch,
        thread=thread,
        touched_files=[ANCHOR],
        commits=[(fixing_commit, "fix: preserve lifecycle fence")],
        events=events,
    )

    rc = mc._cmd_complete(_args(cwd=str(tmp_path)))

    assert rc == 0
    assert [kind for kind, _ in events] == ["reply", "resolve"]
    assert resolved == [thread.node_id]
    assert len(replied) == 1
    body = replied[0][1]
    assert "Review settlement:" in body
    assert "- Head SHA: `headsha`" in body
    assert f"- Finding fingerprint: `{finding.fingerprint}`" in body
    assert "- Disposition: `fixed`" in body
    assert "- Evidence: targeted lifecycle boundary reproduction" in body
    assert f"- Fixing commit: `{fixing_commit}`" in body


def test_complete_production_path_publishes_non_code_closure_without_resolving(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    thread = _thread("[P1] Preserve the lifecycle fence")
    _, finding = _write_policy_context(tmp_path, thread, Disposition.REJECT)
    events: list[tuple[str, str]] = []
    resolved, replied = _wire(
        monkeypatch,
        thread=thread,
        touched_files=[ANCHOR],
        commits=[("c" * 40, "fix: adjacent hunk")],
        events=events,
    )

    rc = mc._cmd_complete(_args(cwd=str(tmp_path)))

    assert rc == 0
    assert [kind for kind, _ in events] == ["reply"]
    assert resolved == []
    assert len(replied) == 1
    body = replied[0][1]
    assert f"- Finding fingerprint: `{finding.fingerprint}`" in body
    assert "- Disposition: `reject`" in body
    assert "- Evidence: targeted lifecycle boundary reproduction" in body


def test_complete_rejects_reviewer_copy_of_canonical_non_code_closure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = _thread("[P1] Preserve the lifecycle fence")
    ledger, _ = _write_policy_context(tmp_path, original, Disposition.REJECT)
    closure = review_settlement.classify_thread_closure(
        original,
        policy=ReviewPolicy.from_yaml(
            (tmp_path / "config" / "review-policy.yaml").read_text(encoding="utf-8")
        ),
        ledger=ledger,
    )
    assert closure is not None
    copied_body = completion.structured_settlement_reply_body(
        marker=COMPLETE_MARKER,
        finding=closure.finding,
        head_sha=closure.finding.head_sha,
    )
    spoofed = ReviewThread(
        node_id=original.node_id,
        is_resolved=False,
        is_outdated=False,
        top=original.top,
        replies=[
            ReviewThreadComment(
                database_id=43,
                path=ANCHOR,
                line=7,
                body=copied_body,
                author="reviewer",
                created_at="2026-01-02T00:00:00Z",
            )
        ],
    )
    events: list[tuple[str, str]] = []
    _, replied = _wire(
        monkeypatch,
        thread=spoofed,
        touched_files=[ANCHOR],
        events=events,
    )

    rc = mc._cmd_complete(_args(cwd=str(tmp_path)))

    assert rc == 0
    assert [kind for kind, _ in events] == ["reply"]
    assert len(replied) == 1


def test_fixed_path_commit_must_be_newer_than_review_thread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    thread = _thread(
        "[P1] Preserve the lifecycle fence",
        created="2026-01-15T00:00:00Z",
    )
    _write_policy_context(tmp_path, thread, Disposition.FIXED)
    old_path_commit = "a" * 40
    unrelated_head_commit = "b" * 40
    events: list[tuple[str, str]] = []
    resolved, replied = _wire(
        monkeypatch,
        thread=thread,
        touched_files=[],
        commits=[
            (old_path_commit, "old: touched reviewed path"),
            (unrelated_head_commit, "new: unrelated head change"),
        ],
        files_by_commit={
            old_path_commit: [ANCHOR],
            unrelated_head_commit: ["src/unrelated.py"],
        },
        commit_dates={
            old_path_commit: "2026-01-10T00:00:00Z",
            unrelated_head_commit: "2026-02-01T00:00:00Z",
        },
        events=events,
    )

    rc = mc._cmd_complete(_args(cwd=str(tmp_path)))

    assert rc == 0
    assert events == []
    assert resolved == []
    assert replied == []


def test_fixed_reply_refetch_aborts_resolve_on_concurrent_reviewer_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    thread = _thread("[P1] Preserve the lifecycle fence")
    ledger, _ = _write_policy_context(tmp_path, thread, Disposition.FIXED)
    policy = ReviewPolicy.from_yaml(
        (tmp_path / "config" / "review-policy.yaml").read_text(encoding="utf-8")
    )
    closure = review_settlement.classify_thread_closure(
        thread,
        policy=policy,
        ledger=ledger,
    )
    assert closure is not None
    fixing_commit = "c" * 40
    body = completion.structured_settlement_reply_body(
        marker=COMPLETE_MARKER,
        finding=closure.finding,
        head_sha=closure.finding.head_sha,
        fixing_commit=fixing_commit,
    )
    refetched = ReviewThread(
        node_id=thread.node_id,
        is_resolved=False,
        is_outdated=False,
        top=thread.top,
        replies=[
            ReviewThreadComment(
                database_id=43,
                path=ANCHOR,
                line=7,
                body=body,
                author="maintenance-bot",
                created_at="2026-02-02T00:00:00Z",
            ),
            ReviewThreadComment(
                database_id=44,
                path=ANCHOR,
                line=7,
                body="The concurrent review still rejects this fix.",
                author="reviewer",
                created_at="2026-02-02T00:00:01Z",
            ),
        ],
    )
    events: list[tuple[str, str]] = []
    resolved, _ = _wire(
        monkeypatch,
        thread=thread,
        touched_files=[ANCHOR],
        commits=[(fixing_commit, "fix: preserve lifecycle fence")],
        commit_dates={fixing_commit: "2026-02-01T00:00:00Z"},
        thread_snapshots=[[thread], [refetched]],
        events=events,
    )

    rc = mc._cmd_complete(_args(cwd=str(tmp_path)))

    assert rc == 0
    assert [kind for kind, _ in events] == ["reply"]
    assert resolved == []


def test_structured_fixed_reply_names_snapshot_fingerprint_and_evidence():
    head_sha = "f" * 40
    fixing_commit = "c0ffee" * 6 + "c0ff"
    finding = Finding(
        repository="Boundless-Studios/agentic-pr-dash",
        head_sha=head_sha,
        reviewer_execution_id="github-backstop",
        reviewer_provider="rev",
        severity=Severity.P1,
        title="Preserve the lifecycle fence",
        explanation="[P1] Preserve the lifecycle fence",
        path=ANCHOR,
        line=7,
        invariant="A stale actor cannot mutate the active generation",
        evidence="pytest: lifecycle fence regression passed",
        disposition=Disposition.FIXED,
        rationale="The fenced mutation now rejects stale generations.",
        verification_passed=True,
    )

    body = completion.structured_settlement_reply_body(
        marker=COMPLETE_MARKER,
        finding=finding,
        head_sha=head_sha,
        fixing_commit=fixing_commit,
    )

    assert body == "\n".join(
        [
            COMPLETE_MARKER,
            "Review settlement:",
            f"- Head SHA: `{head_sha}`",
            f"- Finding fingerprint: `{finding.fingerprint}`",
            "- Disposition: `fixed`",
            "- Rationale: The fenced mutation now rejects stale generations.",
            "- Evidence: pytest: lifecycle fence regression passed",
            f"- Fixing commit: `{fixing_commit}`",
        ]
    )
    parsed = completion.parse_structured_settlement_reply(body)
    assert parsed is not None
    assert parsed.head_sha == head_sha
    assert parsed.finding_fingerprint == finding.fingerprint
    assert parsed.disposition is Disposition.FIXED
    assert parsed.rationale == finding.rationale
    assert parsed.evidence == finding.evidence
    assert parsed.fixing_commit == fixing_commit


def test_structured_fixed_reply_without_fixing_commit_is_malformed():
    closure = _policy_closure(Disposition.FIXED, FindingSettlementState.FIXED)
    body = completion.structured_settlement_reply_body(
        marker=COMPLETE_MARKER,
        finding=closure.finding,
        head_sha=closure.finding.head_sha,
        fixing_commit="c" * 40,
    )
    malformed = "\n".join(
        line for line in body.splitlines() if not line.startswith("- Fixing commit:")
    )

    assert completion.parse_structured_settlement_reply(malformed) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body + "\nReviewer-authored explanation",
        lambda body: body + "\n- Unknown field: injected",
        lambda body: body + "\n- Evidence: duplicate injected evidence",
        lambda body: body.replace(COMPLETE_MARKER, "<!-- copied marker -->", 1),
    ],
)
def test_structured_reply_parser_rejects_noncanonical_body(mutate):
    closure = _policy_closure(
        Disposition.WRONG_OWNER,
        FindingSettlementState.DECLINED_WITH_RATIONALE,
    )
    canonical = completion.structured_settlement_reply_body(
        marker=COMPLETE_MARKER,
        finding=closure.finding,
        head_sha=closure.finding.head_sha,
    )

    assert completion.parse_structured_settlement_reply(mutate(canonical)) is None


def _policy_closure(
    disposition: Disposition,
    state: FindingSettlementState,
    *,
    duplicate_of: str | None = None,
    deferred_to_issue: str | None = None,
    architecture_decision: ArchitectureDecision | None = None,
):
    finding = Finding(
        repository="Boundless-Studios/agentic-pr-dash",
        head_sha="f" * 40,
        reviewer_execution_id="github-backstop",
        reviewer_provider="rev",
        severity=Severity.P1,
        title="Preserve the lifecycle fence",
        explanation="[P1] Preserve the lifecycle fence",
        path=ANCHOR,
        line=7,
        invariant="A stale actor cannot mutate the active generation",
        evidence="review evidence at the fenced mutation boundary",
        duplicate_of=duplicate_of,
        deferred_to_issue=deferred_to_issue,
        disposition=disposition,
        rationale="Evidence supports the recorded policy outcome.",
        verification_passed=state is FindingSettlementState.FIXED,
    )
    return completion.PolicyFindingClosure(
        finding=finding,
        state=state,
        architecture_decision=architecture_decision,
    )


def _visible_reply_boundary(thread, events, *, result=True):
    def _reply(body):
        events.append(("reply", body))
        if result:
            thread.replies.append(
                ReviewThreadComment(
                    database_id=9000 + len(thread.replies),
                    path=thread.top.path,
                    line=thread.top.line,
                    body=body,
                    author="maintenance-bot",
                    created_at="2026-02-02T00:00:00Z",
                )
            )
        return result

    return _reply, lambda: thread


def test_verified_fixed_publication_replies_before_resolving():
    closure = _policy_closure(Disposition.FIXED, FindingSettlementState.FIXED)
    thread = _thread("[P1] Preserve the lifecycle fence")
    events: list[tuple[str, str]] = []
    reply, refetch = _visible_reply_boundary(thread, events)

    outcome = completion.apply_thread_settlement(
        thread=thread,
        closure=closure,
        marker=COMPLETE_MARKER,
        head_sha=closure.finding.head_sha,
        fixing_commit="c" * 40,
        maintenance_author="maintenance-bot",
        reply=reply,
        refetch=refetch,
        resolve=lambda: events.append(("resolve", thread.node_id)) or True,
    )

    assert [kind for kind, _ in events] == ["reply", "resolve"]
    assert outcome.reply_visible
    assert outcome.resolved
    assert not outcome.actionable


def test_fixed_publication_replies_again_when_visible_commit_is_unrelated():
    closure = _policy_closure(Disposition.FIXED, FindingSettlementState.FIXED)
    old_body = completion.structured_settlement_reply_body(
        marker=COMPLETE_MARKER,
        finding=closure.finding,
        head_sha=closure.finding.head_sha,
        fixing_commit="a" * 40,
    )
    thread = _thread(
        "[P1] Preserve the lifecycle fence",
        created="2026-01-01T00:00:00Z",
    )
    thread = ReviewThread(
        node_id=thread.node_id,
        is_resolved=False,
        is_outdated=False,
        top=thread.top,
        replies=[
            ReviewThreadComment(
                database_id=43,
                path=ANCHOR,
                line=7,
                body=old_body,
                author="maintenance-bot",
                created_at="2026-01-02T00:00:00Z",
            )
        ],
    )
    events: list[tuple[str, str]] = []
    reply, refetch = _visible_reply_boundary(thread, events)

    outcome = completion.apply_thread_settlement(
        thread=thread,
        closure=closure,
        marker=COMPLETE_MARKER,
        head_sha=closure.finding.head_sha,
        fixing_commit="b" * 40,
        maintenance_author="maintenance-bot",
        reply=reply,
        refetch=refetch,
        resolve=lambda: events.append(("resolve", thread.node_id)) or True,
    )

    assert [kind for kind, _ in events] == ["reply", "resolve"]
    assert "- Fixing commit: `bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`" in events[0][1]
    assert outcome.resolved


def test_failed_structured_reply_prevents_resolution_and_stays_actionable():
    closure = _policy_closure(Disposition.FIXED, FindingSettlementState.FIXED)
    thread = _thread("[P1] Preserve the lifecycle fence")
    events: list[tuple[str, str]] = []
    reply, refetch = _visible_reply_boundary(thread, events, result=False)

    outcome = completion.apply_thread_settlement(
        thread=thread,
        closure=closure,
        marker=COMPLETE_MARKER,
        head_sha=closure.finding.head_sha,
        fixing_commit="c" * 40,
        maintenance_author="maintenance-bot",
        reply=reply,
        refetch=refetch,
        resolve=lambda: events.append(("resolve", thread.node_id)) or True,
    )

    assert [kind for kind, _ in events] == ["reply"]
    assert not outcome.reply_visible
    assert not outcome.resolved
    assert outcome.actionable


@pytest.mark.parametrize(
    ("disposition", "state", "duplicate_of", "deferred_to_issue", "expected"),
    [
        (Disposition.REJECT, FindingSettlementState.DECLINED_WITH_RATIONALE, None, None, "- Disposition: `reject`"),
        (Disposition.STALE, FindingSettlementState.DECLINED_WITH_RATIONALE, None, None, "- Disposition: `stale`"),
        (Disposition.DECLINED, FindingSettlementState.DECLINED_WITH_RATIONALE, None, None, "- Disposition: `declined`"),
        (Disposition.WRONG_OWNER, FindingSettlementState.DECLINED_WITH_RATIONALE, None, None, "- Disposition: `wrong_owner`"),
        (Disposition.DUPLICATE, FindingSettlementState.DECLINED_WITH_RATIONALE, "finding:prior", None, "- Duplicate of: `finding:prior`"),
        (Disposition.DEFERRED_TO_EXISTING_ISSUE, FindingSettlementState.DEFERRED_TO_EXISTING_ISSUE, None, "BOU-918", "- Existing issue: `BOU-918`"),
    ],
)
def test_non_code_policy_outcomes_reply_but_never_resolve(
    disposition,
    state,
    duplicate_of,
    deferred_to_issue,
    expected,
):
    closure = _policy_closure(
        disposition,
        state,
        duplicate_of=duplicate_of,
        deferred_to_issue=deferred_to_issue,
    )
    thread = _thread("[P1] Preserve the lifecycle fence")
    events: list[tuple[str, str]] = []
    reply, refetch = _visible_reply_boundary(thread, events)

    outcome = completion.apply_thread_settlement(
        thread=thread,
        closure=closure,
        marker=COMPLETE_MARKER,
        head_sha=closure.finding.head_sha,
        fixing_commit=None,
        maintenance_author="maintenance-bot",
        reply=reply,
        refetch=refetch,
        resolve=lambda: events.append(("resolve", thread.node_id)) or True,
    )

    assert [kind for kind, _ in events] == ["reply"]
    assert expected in events[0][1]
    assert outcome.reply_visible
    assert not outcome.resolved
    assert not outcome.actionable


def test_architecture_publication_names_typed_decision_and_lineage():
    base = _policy_closure(
        Disposition.REJECT,
        FindingSettlementState.DECLINED_WITH_RATIONALE,
    )
    decision = ArchitectureDecision(
        repository=base.finding.repository,
        delivery_id="delivery-pr-24",
        review_charter_version="review-charter-v1",
        lineage_id=base.finding.lineage_id,
        decision=ArchitectureDecisionKind.CORE_FIX_PLANNED,
        rationale="Rearchitect the scheduler in the next delivery.",
        decided_by="architecture-owner",
    )
    closure = completion.PolicyFindingClosure(
        finding=base.finding,
        state=base.state,
        architecture_decision=decision,
    )

    body = completion.structured_settlement_reply_body(
        marker=COMPLETE_MARKER,
        finding=closure.finding,
        head_sha=closure.finding.head_sha,
        architecture_decision=decision,
    )

    assert "- Architecture decision: `core_fix_planned`" in body
    assert f"- Architecture lineage: `{base.finding.lineage_id}`" in body
    assert "- Architecture decided by: `architecture-owner`" in body
    assert "- Architecture rationale: Rearchitect the scheduler" in body


def test_reviewer_followup_reopens_structured_non_code_reply():
    closure = _policy_closure(
        Disposition.WRONG_OWNER,
        FindingSettlementState.DECLINED_WITH_RATIONALE,
    )
    body = completion.structured_settlement_reply_body(
        marker=COMPLETE_MARKER,
        finding=closure.finding,
        head_sha=closure.finding.head_sha,
    )
    top = ReviewThreadComment(
        database_id=42,
        path=ANCHOR,
        line=7,
        body="[P1] Preserve the lifecycle fence",
        author="rev",
        created_at="2026-01-01T00:00:00Z",
    )
    thread = ReviewThread(
        node_id="t1",
        is_resolved=False,
        is_outdated=False,
        top=top,
        replies=[
            ReviewThreadComment(
                database_id=43,
                path=ANCHOR,
                line=7,
                body=body,
                author="maintenance-bot",
                created_at="2026-01-02T00:00:00Z",
            ),
            ReviewThreadComment(
                database_id=44,
                path=ANCHOR,
                line=7,
                body="This still belongs to the current owner.",
                author="rev",
                created_at="2026-01-03T00:00:00Z",
            ),
        ],
    )
    events: list[str] = []

    status = completion.settlement_reply_status(
        thread,
        closure,
        marker=COMPLETE_MARKER,
        maintenance_author="maintenance-bot",
    )
    outcome = completion.apply_thread_settlement(
        thread=thread,
        closure=closure,
        marker=COMPLETE_MARKER,
        head_sha=closure.finding.head_sha,
        fixing_commit=None,
        maintenance_author="maintenance-bot",
        reply=lambda body: events.append("reply") or True,
        refetch=lambda: thread,
        resolve=lambda: events.append("resolve") or True,
    )

    assert status is completion.SettlementReplyStatus.REOPENED
    assert events == []
    assert not outcome.reply_visible
    assert outcome.actionable


def test_same_second_reply_order_breaks_settlement_timestamp_ties():
    closure = _policy_closure(
        Disposition.WRONG_OWNER,
        FindingSettlementState.DECLINED_WITH_RATIONALE,
    )
    body = completion.structured_settlement_reply_body(
        marker=COMPLETE_MARKER,
        finding=closure.finding,
        head_sha=closure.finding.head_sha,
    )
    thread = _thread("[P2] Preserve ordering", created="2026-01-01T00:00:00Z")
    thread.replies.extend(
        [
            ReviewThreadComment(
                database_id=43, path=ANCHOR, line=7, body="Still open",
                author="reviewer", created_at="2026-01-01T00:00:00Z",
            ),
            ReviewThreadComment(
                database_id=44, path=ANCHOR, line=7, body=body,
                author="maintenance-bot", created_at="2026-01-01T00:00:00Z",
            ),
        ]
    )

    assert completion.settlement_reply_status(
        thread, closure, marker=COMPLETE_MARKER,
        maintenance_author="maintenance-bot",
    ) is completion.SettlementReplyStatus.FRESH


def test_unrelated_evidence_does_not_make_non_code_reply_fresh():
    closure = _policy_closure(
        Disposition.WRONG_OWNER,
        FindingSettlementState.DECLINED_WITH_RATIONALE,
    )
    body = completion.structured_settlement_reply_body(
        marker=COMPLETE_MARKER,
        finding=closure.finding,
        head_sha=closure.finding.head_sha,
    ).replace(
        "- Evidence: review evidence at the fenced mutation boundary",
        "- Evidence: unrelated observation",
    )
    thread = _thread(
        "[P1] Preserve the lifecycle fence",
        created="2026-01-01T00:00:00Z",
    )
    thread = ReviewThread(
        node_id=thread.node_id,
        is_resolved=False,
        is_outdated=False,
        top=thread.top,
        replies=[
            ReviewThreadComment(
                database_id=43,
                path=ANCHOR,
                line=7,
                body=body,
                author="maintenance-bot",
                created_at="2026-01-02T00:00:00Z",
            )
        ],
    )

    assert (
        completion.settlement_reply_status(
            thread,
            closure,
            marker=COMPLETE_MARKER,
            maintenance_author="maintenance-bot",
        )
        is completion.SettlementReplyStatus.MISSING
    )


def test_later_structured_nonmatching_reply_reopens_current_closure():
    closure = _policy_closure(
        Disposition.WRONG_OWNER,
        FindingSettlementState.DECLINED_WITH_RATIONALE,
    )
    matching = completion.structured_settlement_reply_body(
        marker=COMPLETE_MARKER,
        finding=closure.finding,
        head_sha=closure.finding.head_sha,
    )
    nonmatching = matching.replace(
        closure.finding.fingerprint,
        "0" * 64,
    )
    thread = _thread(
        "[P1] Preserve the lifecycle fence",
        created="2026-01-01T00:00:00Z",
    )
    thread = ReviewThread(
        node_id=thread.node_id,
        is_resolved=False,
        is_outdated=False,
        top=thread.top,
        replies=[
            ReviewThreadComment(
                database_id=43,
                path=ANCHOR,
                line=7,
                body=matching,
                author="maintenance-bot",
                created_at="2026-01-02T00:00:00Z",
            ),
            ReviewThreadComment(
                database_id=44,
                path=ANCHOR,
                line=7,
                body=nonmatching,
                author="reviewer-bot",
                created_at="2026-01-03T00:00:00Z",
            ),
        ],
    )

    assert (
        completion.settlement_reply_status(
            thread,
            closure,
            marker=COMPLETE_MARKER,
            maintenance_author="maintenance-bot",
        )
        is completion.SettlementReplyStatus.REOPENED
    )


def test_different_reviewer_followup_reopens_structured_non_code_reply():
    closure = _policy_closure(
        Disposition.WRONG_OWNER,
        FindingSettlementState.DECLINED_WITH_RATIONALE,
    )
    body = completion.structured_settlement_reply_body(
        marker=COMPLETE_MARKER,
        finding=closure.finding,
        head_sha=closure.finding.head_sha,
    )
    thread = _thread(
        "[P1] Preserve the lifecycle fence",
        created="2026-01-01T00:00:00Z",
        replies=(),
    )
    thread = ReviewThread(
        node_id=thread.node_id,
        is_resolved=False,
        is_outdated=False,
        top=thread.top,
        replies=[
            ReviewThreadComment(
                database_id=43,
                path=ANCHOR,
                line=7,
                body=body,
                author="maintenance-bot",
                created_at="2026-01-02T00:00:00Z",
            ),
            ReviewThreadComment(
                database_id=44,
                path=ANCHOR,
                line=7,
                body="I am also reviewing this; the ownership concern remains.",
                author="second-reviewer",
                created_at="2026-01-03T00:00:00Z",
            ),
        ],
    )

    assert (
        completion.settlement_reply_status(
            thread,
            closure,
            marker=COMPLETE_MARKER,
            maintenance_author="maintenance-bot",
        )
        is completion.SettlementReplyStatus.REOPENED
    )


# --- negative: anchor touched, body points elsewhere -> DO NOT resolve --------

def test_anchor_touch_but_body_points_at_untouched_module_not_resolved(monkeypatch):
    thread = _thread(
        "Initialize logging for worker Cloud Run app too — see "
        "`gaia.api.worker_app` (worker_app.py)."
    )
    # The fixing commit touched ONLY the anchor file, never worker_app.py.
    resolved, replied = _wire(monkeypatch, thread=thread, touched_files=[ANCHOR])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    # Ambiguous: requested file (worker_app.py) was never touched -> left OPEN.
    assert resolved == []
    assert replied == []
    # And it stays counted as unresolved so the prompt re-surfaces it.
    monkeypatch.setattr(github_api, "get_review_threads", lambda n, cwd=None: [thread])
    assert mc.pr_has_unresolved_review_threads(2139, ".") is True


def test_anchor_touch_but_body_points_at_untouched_path_not_resolved(monkeypatch):
    thread = _thread("The real change belongs in backend/src/gaia/api/worker_app.py")
    resolved, _ = _wire(monkeypatch, thread=thread, touched_files=[ANCHOR])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []


# --- positive: anchored hunk changed, body has no other-file ref -> resolves ---

def test_anchored_hunk_changed_body_no_other_ref_still_resolves(monkeypatch):
    thread = _thread("Please add a docstring and fix the typo here.")
    resolved, replied = _wire(monkeypatch, thread=thread, touched_files=[ANCHOR])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    # Clear case — the fix changed the thread's own hunk, no untouched file
    # referenced.
    assert resolved == ["t1"]
    assert [t for t, _ in replied] == ["t1"]


def test_anchored_hunk_changed_body_references_the_anchor_file_still_resolves(monkeypatch):
    # Body references its own anchor module — that is NOT "elsewhere".
    thread = _thread("Fix the logging init in app.py / gaia.api.app")
    resolved, _ = _wire(monkeypatch, thread=thread, touched_files=[ANCHOR])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == ["t1"]


def test_anchored_hunk_changed_body_references_a_touched_other_file_still_resolves(monkeypatch):
    # Body points at worker_app.py AND the fixing commit DID touch it -> clear.
    thread = _thread("Initialize logging in gaia.api.worker_app too (worker_app.py).")
    resolved, _ = _wire(
        monkeypatch, thread=thread,
        touched_files=[ANCHOR, "backend/src/gaia/api/worker_app.py"],
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == ["t1"]


# --- direct unit coverage of the heuristic helper -----------------------------

def test_thread_points_elsewhere_helper():
    touched = {ANCHOR}
    # References an untouched module -> ambiguous.
    assert mc._thread_points_elsewhere(
        "see gaia.api.worker_app", ANCHOR, touched) is True
    # References only its own anchor -> not elsewhere.
    assert mc._thread_points_elsewhere(
        "fix app.py here", ANCHOR, touched) is False
    # No file/module reference at all -> not elsewhere.
    assert mc._thread_points_elsewhere(
        "add a docstring", ANCHOR, touched) is False
    # Prose dotted token (e.g.) must not trip the gate.
    assert mc._thread_points_elsewhere(
        "do this, e.g. add a guard", ANCHOR, touched) is False


def test_thread_points_elsewhere_ignores_badge_url_and_callable_mentions():
    body = (
        "<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)"
        "</sub></sub>\n\n"
        "Make sure `scheduler.stop()` runs before returning."
    )

    assert mc._thread_elsewhere_refs(body, ANCHOR, {ANCHOR}) == []


# --- BOU-2095 supersedes BOU-1748: "outdated" never widens resolution ----------

DECK_CONF = "worktree-deck.conf"
OTHER_PY = "scripts/cleanup-orphan-worktrees.py"


def test_bou2095_outdated_plus_elsewhere_mention_no_longer_resolves(monkeypatch):
    # BOU-1748's former widening: thread anchored on worktree-deck.conf, the fix
    # touched worktree-deck.conf, GitHub marks the thread outdated, and the body
    # also mentions scripts/cleanup-orphan-worktrees.py (untouched). BOU-2095
    # removes the widening: "outdated" is line drift, not evidence, and a
    # cross-file ask stays ambiguous even when the anchored hunk changed —
    # the thread stays OPEN for a human.
    thread = _thread(
        "Bump the orphan-sweep age here; this is what "
        "`scripts/cleanup-orphan-worktrees.py` reads.",
        path=DECK_CONF,
        outdated=True,
    )
    resolved, replied = _wire(monkeypatch, thread=thread, touched_files=[DECK_CONF])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []
    assert replied == []


def test_anchor_touched_but_not_outdated_body_points_elsewhere_stays_open(monkeypatch):
    # Same shape but the thread is NOT outdated: the BOU-1641 guard must still
    # hold so a real "fix belongs in the other file" comment is not lost.
    thread = _thread(
        "Bump the orphan-sweep age here; this is what "
        "`scripts/cleanup-orphan-worktrees.py` reads.",
        path=DECK_CONF,
        outdated=False,
    )
    resolved, _ = _wire(monkeypatch, thread=thread, touched_files=[DECK_CONF])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []


def test_leaving_open_message_names_the_conflicting_path(monkeypatch, capsys):
    thread = _thread(
        "The real change belongs in backend/src/gaia/api/worker_app.py",
        outdated=False,
    )
    _wire(monkeypatch, thread=thread, touched_files=[ANCHOR])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    err = capsys.readouterr().err
    # AC: when still left open, the message explains the specific conflicting path.
    assert "backend/src/gaia/api/worker_app.py" in err
    assert "ambiguous resolution" in err


def test_thread_elsewhere_refs_helper_returns_conflicting_refs():
    touched = {ANCHOR}
    # Untouched module reference is reported.
    assert mc._thread_elsewhere_refs(
        "see gaia.api.worker_app", ANCHOR, touched) == ["gaia.api.worker_app"]
    # Only the anchor / touched refs -> nothing points elsewhere.
    assert mc._thread_elsewhere_refs("fix app.py here", ANCHOR, touched) == []
    assert mc._thread_elsewhere_refs("add a docstring", ANCHOR, touched) == []
    # De-duplicated, in body order.
    assert mc._thread_elsewhere_refs(
        "scripts/a.py then scripts/a.py then scripts/b.py",
        DECK_CONF, {DECK_CONF},
    ) == ["scripts/a.py", "scripts/b.py"]


# --- BOU-2095: hunk-level evidence required; drift/file-touch never resolve ----


def test_bou2095_file_touched_but_anchored_hunk_untouched_not_resolved(monkeypatch):
    # Case (a): thread on ANCHOR line 7; the completing commit touched ANCHOR
    # but only changed lines 100-110 — a different hunk. Must stay OPEN with no
    # "Addressed by" reply.
    thread = _thread("Guard against a None campaign here.")
    resolved, replied = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_ELSEWHERE_IN_FILE,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []
    assert replied == []


def test_bou2095_pure_line_drift_outdated_not_resolved(monkeypatch):
    # Case (b): the commit inserted lines at the TOP of the file, so GitHub
    # marks the thread outdated (line=None, originalLine kept) — pure drift.
    # The anchored hunk (line 50) never changed. Must stay OPEN.
    thread = _thread(
        "Rename this variable for clarity.",
        outdated=True, line=None, original_line=50,
    )
    # Insertion at top: old side empty (end < start), new lines 1-3 added.
    resolved, replied = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=[(1, 0, 1, 3)],
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []
    assert replied == []


def test_bou2095_anchored_hunk_content_changed_resolves(monkeypatch):
    # Case (c): the completing commit modified the anchored hunk itself —
    # genuine auto-resolution is preserved, with the completion reply.
    thread = _thread("Guard against a None campaign here.")
    resolved, replied = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_AT_ANCHOR,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == ["t1"]
    assert [t for t, _ in replied] == ["t1"]


def test_bou2320_completion_replies_before_resolving(monkeypatch):
    thread = _thread("Guard against a None campaign here.")
    events = []
    _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_AT_ANCHOR, events=events,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert events == [("reply", "t1"), ("resolve", "t1")]


def test_bou2320_resolve_failure_keeps_marker_reply_and_blocker(
        monkeypatch, capsys):
    thread = _thread("Guard against a None campaign here.")
    resolved, replied = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_AT_ANCHOR, resolve_result=False,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == ["t1"]
    assert len(replied) == 1
    assert COMPLETE_MARKER in replied[0][1]
    captured = capsys.readouterr()
    assert "bead left open" in captured.out
    assert "review_comments" in captured.out
    assert "could not resolve thread t1" in captured.err


def test_bou2320_reply_failure_prevents_resolution(monkeypatch, capsys):
    thread = _thread("Guard against a None campaign here.")
    resolved, replied = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_AT_ANCHOR, reply_result=False,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert len(replied) == 1
    assert resolved == []
    captured = capsys.readouterr()
    assert "bead left open" in captured.out
    assert "review_comments" in captured.out
    assert "could not reply to thread t1" in captured.err


def test_bou2095_multi_thread_same_file_only_fixed_hunk_resolves(monkeypatch):
    # Case (d): two threads on the same file; the fix changed only t1's hunk
    # (line 7). t2 (line 200) must stay OPEN.
    t1 = _thread("Fix the off-by-one here.", node_id="t1", line=7)
    t2 = _thread("This branch needs a docstring.", node_id="t2", line=200)
    resolved, replied = _wire(
        monkeypatch, thread=t1, threads=[t1, t2], touched_files=[ANCHOR],
        spans=SPANS_AT_ANCHOR,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == ["t1"]
    assert [t for t, _ in replied] == ["t1"]


def test_bou2095_outdated_anchored_hunk_changed_still_resolves(monkeypatch):
    # Outdated is not a blocker either: when the base..head diff shows the
    # ORIGINAL anchor line's hunk was rewritten, that is positive evidence.
    thread = _thread(
        "Guard against a None campaign here.",
        outdated=True, line=None, original_line=7,
    )
    resolved, _ = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_AT_ANCHOR,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == ["t1"]


def test_bou2095_diff_unavailable_is_no_evidence_not_resolved(monkeypatch):
    # Absence of proof is not proof: when the base..head diff cannot be
    # computed (spans=None), the thread stays OPEN even though the file was
    # touched.
    thread = _thread("Guard against a None campaign here.")
    resolved, replied = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR], spans=None,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []
    assert replied == []


def test_bou2095_existing_completion_reply_resolves_as_retry(monkeypatch):
    # An earlier `complete` run already posted the completion-marker reply but
    # the resolve mutation failed. Retrying resolves even without hunk
    # intersection — the reply IS the explicit per-thread evidence.
    thread = _thread(
        "Guard against a None campaign here.",
        replies=(f"{COMPLETE_MARKER}\nAddressed by the local maintenance loop.",),
    )
    resolved, replied = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_ELSEWHERE_IN_FILE,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == ["t1"]
    assert replied == []


def test_pr78_stale_marker_after_reviewer_followup_does_not_resolve(monkeypatch):
    # PR #78 review (comment 3605704909): a historical completion marker whose
    # thread was REOPENED by a later human follow-up is NOT evidence — the
    # reviewer rejected the completion. Without hunk evidence the thread must
    # stay open instead of short-circuiting on the stale marker.
    thread = _thread(
        "Guard against a None campaign here.",
        replies=(
            f"{COMPLETE_MARKER}\nAddressed by the local maintenance loop.",
            "This was NOT addressed — the guard is still missing.",
        ),
    )
    resolved, replied = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_ELSEWHERE_IN_FILE,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []
    assert replied == []


def test_bou2320_reopened_thread_with_newer_head_requires_manual_confirmation(
        monkeypatch, capsys):
    # The failed resolve left a marker, then the reviewer rejected the fix
    # AFTER the current HEAD was pushed. Even if the base..HEAD hunk still
    # intersects the anchor, reopened feedback requires manual confirmation.
    top = ReviewThreadComment(
        database_id=42, path=ANCHOR, line=7,
        body="Guard against a None campaign here.", author="rev",
        created_at="2026-01-01T00:00:00Z",
    )
    thread = ReviewThread(
        node_id="t1", is_resolved=False, is_outdated=False, top=top,
        replies=[
            ReviewThreadComment(
                database_id=43, path=ANCHOR, line=7,
                body=f"{COMPLETE_MARKER}\nAddressed in an earlier attempt.",
                author="bot", created_at="2026-01-15T00:00:00Z",
            ),
            ReviewThreadComment(
                database_id=44, path=ANCHOR, line=7,
                body="This is still not fixed.", author="rev",
                created_at="2026-03-01T00:00:00Z",
            ),
        ],
    )
    resolved, replied = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_AT_ANCHOR,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []
    assert replied == []
    captured = capsys.readouterr()
    assert "bead left open" in captured.out
    assert "review_comments" in captured.out


def test_bou2320_unrelated_newer_commit_does_not_freshen_old_anchor_change(
        monkeypatch, capsys):
    top = ReviewThreadComment(
        database_id=42, path=ANCHOR, line=7,
        body="Guard against a None campaign here.", author="rev",
        created_at="2026-01-01T00:00:00Z",
    )
    thread = ReviewThread(
        node_id="t1", is_resolved=False, is_outdated=False, top=top,
        replies=[
            ReviewThreadComment(
                database_id=43, path=ANCHOR, line=7,
                body=f"{COMPLETE_MARKER}\nAddressed in commit A.",
                author="bot", created_at="2026-01-15T00:00:00Z",
            ),
            ReviewThreadComment(
                database_id=44, path=ANCHOR, line=7,
                body="The change in A is still wrong.", author="rev",
                created_at="2026-01-20T00:00:00Z",
            ),
        ],
    )
    commits = [("aaaaaaa", "fix: change anchor"), ("bbbbbbb", "docs: unrelated")]
    resolved, replied = _wire(
        monkeypatch,
        thread=thread,
        touched_files=[],
        commits=commits,
        files_by_commit={"aaaaaaa": [ANCHOR], "bbbbbbb": ["README.md"]},
        spans=SPANS_AT_ANCHOR,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []
    assert replied == []
    captured = capsys.readouterr()
    assert "bead left open" in captured.out
    assert "review_comments" in captured.out


def test_bou2320_newer_anchor_change_still_requires_manual_confirmation(
        monkeypatch, capsys):
    top = ReviewThreadComment(
        database_id=42, path=ANCHOR, line=7,
        body="Guard against a None campaign here.", author="rev",
        created_at="2026-01-01T00:00:00Z",
    )
    thread = ReviewThread(
        node_id="t1", is_resolved=False, is_outdated=False, top=top,
        replies=[
            ReviewThreadComment(
                database_id=43, path=ANCHOR, line=7,
                body=f"{COMPLETE_MARKER}\nAddressed in commit A.",
                author="bot", created_at="2026-01-15T00:00:00Z",
            ),
            ReviewThreadComment(
                database_id=44, path=ANCHOR, line=7,
                body="The change in A is still wrong.", author="rev",
                created_at="2026-01-20T00:00:00Z",
            ),
        ],
    )
    commits = [("aaaaaaa", "fix: first attempt"), ("ccccccc", "fix: follow-up")]
    resolved, replied = _wire(
        monkeypatch,
        thread=thread,
        touched_files=[],
        commits=commits,
        files_by_commit={"aaaaaaa": [ANCHOR], "ccccccc": [ANCHOR]},
        spans=SPANS_AT_ANCHOR,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []
    assert replied == []
    captured = capsys.readouterr()
    assert "manual confirmation" in captured.err
    assert "review_comments" in captured.out


def test_pr78_head_side_anchor_matching_only_old_span_not_resolved(monkeypatch):
    # PR #78 review (comment 3605704914): a live thread's `line` is a HEAD-side
    # coordinate. A rewrite whose OLD-side span happens to overlap that number
    # (48-52) while the new-side content moved elsewhere (120-124) must not
    # count as hunk evidence.
    thread = _thread("Guard against a None campaign here.", line=50)
    resolved, replied = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=[(48, 52, 120, 124)],
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []
    assert replied == []


def test_pr78_outdated_anchor_matching_only_new_span_not_resolved(monkeypatch):
    # Mirror case: an outdated thread's `original_line` is a BASE-side
    # coordinate; overlap with only the new-side span is not evidence.
    thread = _thread(
        "Guard against a None campaign here.",
        outdated=True, line=None, original_line=122,
    )
    resolved, _ = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=[(48, 52, 120, 124)],
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []


def test_pr78_left_open_outdated_thread_keeps_review_comments_blocker(
        monkeypatch, capsys):
    # PR #78 review (comment 3605704917, P1): a drift-outdated thread the gate
    # deliberately leaves open must keep the bead open as a review_comments
    # blocker — the downstream scan filters skip is_outdated, so without this
    # `complete` would report "no blockers remain" and close the bead.
    thread = _thread(
        "Rename this variable for clarity.",
        outdated=True, line=None, original_line=50,
    )
    resolved, _ = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=[(1, 0, 1, 3)],
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []
    captured = capsys.readouterr()
    assert "bead left open" in captured.out
    assert "review_comments" in captured.out
    assert "remain unresolved" in captured.err


def test_bou2095_file_level_comment_resolves_on_file_content_change(monkeypatch):
    # A file-level comment (no line anchor at all) anchors the whole file, so
    # any content change in the file is anchored-hunk evidence.
    thread = _thread("Please add a module docstring.", line=None, original_line=None)
    resolved, _ = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_ELSEWHERE_IN_FILE,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == ["t1"]


def test_bou2095_leaving_open_message_explains_hunk_mismatch(monkeypatch, capsys):
    thread = _thread("Guard against a None campaign here.")
    _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_ELSEWHERE_IN_FILE,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    err = capsys.readouterr().err
    assert "leaving thread t1 open" in err
    assert "file-touch/line-drift alone" in err


# --- direct unit coverage of the span helpers ----------------------------------


def test_spans_intersect_line_helper():
    spans = [(10, 12, 10, 14)]
    # Inside the hunk, matched on the anchor's own side.
    assert mc._spans_intersect_line(spans, 11, "new") is True
    assert mc._spans_intersect_line(spans, 11, "old") is True
    # Within the ±context fuzz.
    assert mc._spans_intersect_line(spans, 14 + mc._ANCHOR_CONTEXT_LINES, "new") is True
    assert mc._spans_intersect_line(spans, 10 - mc._ANCHOR_CONTEXT_LINES, "new") is True
    # Beyond the fuzz.
    assert mc._spans_intersect_line(spans, 14 + mc._ANCHOR_CONTEXT_LINES + 1, "new") is False
    assert mc._spans_intersect_line(spans, 1, "new") is False
    # PR #78 review: each coordinate is compared ONLY against its own diff
    # side — old-side overlap is not evidence for a head-side anchor and
    # vice versa.
    disjoint = [(48, 52, 120, 124)]
    assert mc._spans_intersect_line(disjoint, 50, "old") is True
    assert mc._spans_intersect_line(disjoint, 50, "new") is False
    assert mc._spans_intersect_line(disjoint, 122, "new") is True
    assert mc._spans_intersect_line(disjoint, 122, "old") is False
    # Empty side (pure insertion: old side (1, 0)) never matches on that side.
    assert mc._spans_intersect_line([(1, 0, 1, 3)], 50, "old") is False
    assert mc._spans_intersect_line([(1, 0, 1, 3)], 2, "old") is False
    assert mc._spans_intersect_line([], 5, "new") is False


def test_get_changed_line_spans_parses_real_git_diff(tmp_path):
    import subprocess

    def _git(*args):
        subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True, capture_output=True, text=True,
        )

    _git("init", "-q")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    f = tmp_path / "a.py"
    f.write_text("".join(f"line{i}\n" for i in range(1, 21)))
    _git("add", "a.py")
    _git("commit", "-qm", "base")
    base = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    # Change line 5 and delete line 15.
    lines = f.read_text().splitlines(keepends=True)
    lines[4] = "line5-changed\n"
    del lines[14]
    f.write_text("".join(lines))
    _git("commit", "-aqm", "fix")
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    spans = github_api.get_changed_line_spans(base, head, "a.py", str(tmp_path))

    # Deletion hunk: old line 15 removed; git reports the new side as
    # `+14,0` -> empty new span encoded (14, 13).
    assert spans == [(5, 5, 5, 5), (15, 15, 14, 13)]
    # Unknown SHA -> None (no evidence), not [].
    assert github_api.get_changed_line_spans(
        "0" * 40, head, "a.py", str(tmp_path)) is None
    # Untouched path -> [] (real evidence of no change).
    assert github_api.get_changed_line_spans(base, head, "other.py", str(tmp_path)) == []


# --- BOU-2408: the "not addressed" skip must explain itself --------------------
#
# Of the three paths that leave a thread unresolved, `not addressed` was the
# only silent one. The `evidence is None` and cross-file `elsewhere` cases each
# print an `info:` line; this one appended to `left_unresolved` and moved on.
#
# The visible result was `complete` printing
#     completed (bead left open; blockers remain: review_comments)
# with NOTHING on stderr, which reads as "the tool failed" rather than "the
# fixing commits never touched the file this thread is anchored on". Observed
# on gaia-free PR #2801, where the fix landed entirely in a test file while the
# thread was anchored on the script under test — a correct refusal, reported
# as silence.


def test_bou2408_untouched_anchor_explains_why_thread_stays_open(monkeypatch, capsys):
    """Anchored file not touched by the fixing commits -> say so."""
    thread = _thread("Force the Docker path in remote-mount tests.")
    resolved, replied = _wire(
        monkeypatch, thread=thread, touched_files=["some/other/file.py"],
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == [], "must not auto-resolve an untouched anchor"
    assert replied == [], "must not reply to an untouched anchor"

    err = capsys.readouterr().err
    assert "leaving thread t1 open" in err, (
        "the untouched-anchor skip must announce itself like the other two "
        f"skip paths do; stderr was: {err!r}"
    )
    assert ANCHOR in err, "the message must name the anchored file"
    assert "not touched by the fixing commits" in err


def test_bou2408_no_new_commits_explains_why_thread_stays_open(monkeypatch, capsys):
    """Nothing pushed since the baseline -> say that, not nothing.

    `_wire` cannot express this case: it does `commits or [default]`, so an
    empty list falls back to the default one-commit fixture. Patch the boundary
    directly after wiring.
    """
    thread = _thread("Please fix this.")
    _wire(monkeypatch, thread=thread, touched_files=[ANCHOR])
    monkeypatch.setattr(github_api, "get_new_pr_commits", lambda *a, **k: [])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    err = capsys.readouterr().err
    assert "leaving thread t1 open" in err
    assert "no new commits" in err, (
        f"an empty baseline..HEAD range must be named as the reason; got: {err!r}"
    )
