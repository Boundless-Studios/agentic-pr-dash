"""Tests for PRData.ci_watch_pending and watch_pending_for_pr (BOU-1789 Task 2)."""
from __future__ import annotations

import pytest

from agentic_pr_dash.models import PRData, PRStatus
from agentic_pr_dash.maintenance import watch_pending_for_pr, blockers_for_pr


def _pr(**kwargs) -> PRData:
    defaults = dict(number=1, title="T", branch="b", url="http://example.com/1")
    defaults.update(kwargs)
    return PRData(**defaults)


def test_ci_watch_pending_default_false():
    """PRData.ci_watch_pending defaults to False."""
    pr = _pr()
    assert pr.ci_watch_pending is False


def test_watch_pending_true_when_ci_watch_pending_and_no_blockers():
    """watch_pending_for_pr returns True when ci_watch_pending=True and no blockers."""
    pr = _pr(ci_watch_pending=True)
    assert blockers_for_pr(pr) == []
    assert watch_pending_for_pr(pr) is True


def test_watch_pending_false_when_blocker_present():
    """watch_pending_for_pr returns False when there's an actionable blocker."""
    pr = _pr(ci_watch_pending=True, failing_checks=["CI / build"])
    assert "ci_failure" in blockers_for_pr(pr)
    assert watch_pending_for_pr(pr) is False


def test_watch_pending_false_when_ci_watch_pending_false():
    """watch_pending_for_pr returns False when ci_watch_pending is False."""
    pr = _pr(ci_watch_pending=False)
    assert watch_pending_for_pr(pr) is False


def test_watch_pending_false_with_review_comments_blocker():
    """watch_pending_for_pr returns False when review_comments is a blocker."""
    from agentic_pr_dash.models import ReviewComment
    comment = ReviewComment(id=1, author="bob", body="fix this", created_at="2024-01-01T00:00:00Z")
    pr = _pr(ci_watch_pending=True, review_comments=[comment])
    assert "review_comments" in blockers_for_pr(pr)
    assert watch_pending_for_pr(pr) is False


def test_watch_pending_false_with_merge_conflict():
    """watch_pending_for_pr returns False when merge conflict is a blocker."""
    pr = _pr(ci_watch_pending=True, merge_state="DIRTY")
    assert "merge_conflict" in blockers_for_pr(pr)
    assert watch_pending_for_pr(pr) is False
