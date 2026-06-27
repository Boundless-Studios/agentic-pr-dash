"""BOU-1801 — the monitoring loop must reliably pick up fresh review feedback.

Confirmed bug (github_api.py:1840-1892): when the reviewer who *opened* a thread
posts a follow-up reply before any dashboard marker, the thread state machine
walks the reply as a "human reply" and flips the thread to the sticky
``human_resolved`` state — so ``get_unaddressed_comments`` drops it. The reply is
the reviewer's OWN fresh feedback, not a third-party resolution, so the loop
never dispatches against it. That is the "not reliably picking up new comments"
symptom.

This test asserts the observable behaviour at the github_api boundary: a thread
whose top AND only reply are authored by the same reviewer, with no dashboard
marker, must be returned as unaddressed. It stubs ONLY the gh API boundary
(``get_review_threads`` / ``_run``); it does NOT stub the thread state machine
(``_thread_is_addressed_or_claimed``), which is the code under test.
"""

from types import SimpleNamespace

from agentic_pr_dash import github_api
from agentic_pr_dash.github_api import (
    COMPLETE_MARKER,
    ReviewThread,
    ReviewThreadComment,
)


def _comment(author: str, body: str, created_at: str) -> ReviewThreadComment:
    return ReviewThreadComment(
        database_id=hash(body) & 0xFFFF,
        path="src/foo.py",
        line=10,
        body=body,
        author=author,
        created_at=created_at,
    )


def _no_review_level_comments(monkeypatch) -> None:
    """Stub the REST /reviews call so the review-level branch adds nothing."""
    monkeypatch.setattr(
        github_api,
        "_run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""),
    )


def test_reviewer_followup_reply_keeps_thread_unaddressed(monkeypatch):
    """A same-author follow-up reply (no marker) must stay actionable."""
    thread = ReviewThread(
        node_id="THREAD_A",
        is_resolved=False,
        is_outdated=False,
        top=_comment("reviewer-bot", "please rename this", "2026-06-27T10:00:00Z"),
        replies=[
            _comment("reviewer-bot", "also fix the docstring", "2026-06-27T10:05:00Z"),
        ],
    )
    monkeypatch.setattr(github_api, "get_review_threads", lambda *a, **k: [thread])
    _no_review_level_comments(monkeypatch)

    out = github_api.get_unaddressed_comments(1, "2026-06-27T09:00:00Z", cwd=".")

    assert any(c.thread_id == "THREAD_A" for c in out), (
        "reviewer's own follow-up reply was silently dropped as human_resolved"
    )


def test_dashboard_completed_thread_is_not_returned(monkeypatch):
    """Guard against over-correction: a COMPLETE-marked thread stays skipped."""
    thread = ReviewThread(
        node_id="THREAD_B",
        is_resolved=False,
        is_outdated=False,
        top=_comment("reviewer-bot", "please rename this", "2026-06-27T10:00:00Z"),
        replies=[
            _comment("agent", f"done {COMPLETE_MARKER}", "2026-06-27T10:05:00Z"),
        ],
    )
    monkeypatch.setattr(github_api, "get_review_threads", lambda *a, **k: [thread])
    _no_review_level_comments(monkeypatch)

    out = github_api.get_unaddressed_comments(1, "2026-06-27T09:00:00Z", cwd=".")

    assert not any(c.thread_id == "THREAD_B" for c in out), (
        "a dashboard-completed thread must not be re-surfaced as unaddressed"
    )
