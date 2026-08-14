"""BOU-2567 — ``complete`` must never auto-resolve a deferred review thread.

Reuses the ``_wire`` harness shape from
``test_complete_thread_resolution_semantic.py`` (stub the gh/GraphQL boundary,
drive the real ``_cmd_complete``). The thread here has full "addressed"
evidence (a post-baseline commit changed its own anchored hunk) — the exact
shape that ``test_bou2095_anchored_hunk_content_changed_resolves`` proves DOES
auto-resolve today. The only difference is the thread is deferred; that alone
must be enough to leave it untouched — no reply, no resolve mutation, and not
counted toward the ``review_comments`` blocker (a deferred thread's disposition
already has a reply + a tracked ticket; re-litigating "addressed" evidence for
it would just duplicate work the deferral already settled).

This file was run against pre-fix code to confirm RED (deferred threads were
resolved/replied-to exactly like any other addressed thread) before the skip
was added to ``_cmd_complete``'s per-thread loop.
"""
from __future__ import annotations

import argparse

from agentic_pr_dash import github_api, maintenance
from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment
from agentic_pr_dash.models import PRData, PRStatus
from agentic_pr_dash._maintenance import deferred_review as dr

ANCHOR = "backend/src/gaia/api/app.py"
SPANS_AT_ANCHOR = [(6, 8, 6, 9)]
PR_NUMBER = 2139


def _thread(body: str = "Guard against a None campaign here.", node_id: str = "t1") -> ReviewThread:
    c = ReviewThreadComment(
        database_id=42, path=ANCHOR, line=7, body=body,
        author="rev", created_at="2026-01-01T00:00:00Z",
    )
    return ReviewThread(node_id=node_id, is_resolved=False, is_outdated=False, top=c)


def _pr() -> PRData:
    return PRData(
        number=PR_NUMBER, repo="boundless/test", title="t", branch="b", url=f"https://x/pull/{PR_NUMBER}",
        failing_checks=[], review_comments=[], merge_state="CLEAN",
        latest_commit_sha="headsha", latest_commit_date="2026-02-01T00:00:00Z",
        worktree_path="/wt", status=PRStatus.CLEAN,
    )


def _wire(monkeypatch, *, thread: ReviewThread):
    resolved_calls: list[str] = []
    reply_calls: list[object] = []

    monkeypatch.setattr(mc, "_resolve_pr_by_number", lambda n, cwd, **kw: _pr())
    monkeypatch.setattr(github_api, "get_local_pr_head", lambda branch, cwd: ("", ""))
    monkeypatch.setattr(github_api, "_is_ancestor", lambda a, d, cwd: False)
    monkeypatch.setattr(
        github_api, "get_new_pr_commits", lambda *a, **k: [("c0ffee", "fix: guard")]
    )
    monkeypatch.setattr(
        github_api, "get_commit_changed_files", lambda sha, cwd=None: [ANCHOR]
    )
    monkeypatch.setattr(
        github_api, "get_changed_line_spans",
        lambda base, head, path, cwd=None: list(SPANS_AT_ANCHOR),
    )
    monkeypatch.setattr(
        github_api, "get_review_threads", lambda n, cwd=None: [thread]
    )

    def _resolve(node_id, cwd=None):
        resolved_calls.append(node_id)
        return True

    def _reply(pr_number, comment, body, cwd=None):
        reply_calls.append((comment.thread_id, body))
        return True

    monkeypatch.setattr(github_api, "resolve_review_thread", _resolve)
    monkeypatch.setattr(github_api, "reply_to_review_comment", _reply)
    monkeypatch.setattr(mc, "_mark_maintenance_complete", lambda *a, **k: None)
    monkeypatch.setattr(maintenance, "blockers_for_pr", lambda pr: [])

    return resolved_calls, reply_calls


def _args(cwd: str = "."):
    return argparse.Namespace(cwd=cwd, pr=PR_NUMBER, baseline="basesha")


def test_control_fully_addressed_thread_still_auto_resolves(monkeypatch) -> None:
    """Baseline (matches test_bou2095_anchored_hunk_content_changed_resolves):
    a non-deferred thread with full evidence DOES auto-resolve."""
    thread = _thread()
    resolved, replied = _wire(monkeypatch, thread=thread)

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == ["t1"]
    assert [t for t, _ in replied] == ["t1"]


def test_deferred_thread_is_never_auto_resolved_even_with_full_evidence(
    tmp_path, monkeypatch
) -> None:
    """The fix: deferral outranks the evidence gate entirely — a deferred
    thread is never resolved or replied to by `complete`, regardless of
    whether the fixing commits would otherwise satisfy the evidence bar."""
    thread = _thread()
    cwd = str(tmp_path)
    resolved, replied = _wire(monkeypatch, thread=thread)
    dr.defer_thread(
        cwd, PR_NUMBER, thread_id="t1", comment_id=42, severity="P1",
        ticket="BOU-2559", reason="out of scope: requires files this PR does not own",
    )

    rc = mc._cmd_complete(_args(cwd=cwd))

    assert rc == 0
    assert resolved == [], "a deferred thread must never be auto-resolved by complete"
    assert replied == [], "complete must not post its own completion reply over a deferral"


def test_deferred_thread_does_not_count_as_a_remaining_blocker(
    tmp_path, monkeypatch, capsys
) -> None:
    """A PR whose only unresolved thread is deferred must close cleanly under
    `complete` — the deferred thread must not keep forcing the
    'review_comments' blocker that would otherwise leave the bead open
    forever."""
    thread = _thread()
    cwd = str(tmp_path)
    _wire(monkeypatch, thread=thread)
    dr.defer_thread(
        cwd, PR_NUMBER, thread_id="t1", comment_id=42, severity="P2", ticket="BOU-1000",
    )

    rc = mc._cmd_complete(_args(cwd=cwd))

    assert rc == 0
    out = capsys.readouterr().out
    assert "review_comments" not in out, (
        f"a deferred-only PR must not be kept open on the review_comments "
        f"blocker; got: {out!r}"
    )
