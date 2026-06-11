from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash import github_api
from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment


def _thread(resolved=False, outdated=False):
    c = ReviewThreadComment(database_id=1, path="f.py", line=1, body="fix",
                            author="rev", created_at="2026-01-01T00:00:00Z")
    return ReviewThread(node_id="t1", is_resolved=resolved, is_outdated=outdated, top=c)


def test_unresolved_nonoutdated_thread_blocks(monkeypatch):
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [_thread()])
    assert mc.pr_has_unresolved_review_threads(5, ".") is True


def test_resolved_thread_does_not_block(monkeypatch):
    monkeypatch.setattr(github_api, "get_review_threads",
                        lambda pr, cwd=None: [_thread(resolved=True)])
    assert mc.pr_has_unresolved_review_threads(5, ".") is False


def test_outdated_thread_does_not_block(monkeypatch):
    monkeypatch.setattr(github_api, "get_review_threads",
                        lambda pr, cwd=None: [_thread(outdated=True)])
    assert mc.pr_has_unresolved_review_threads(5, ".") is False


def test_no_threads_does_not_block(monkeypatch):
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [])
    assert mc.pr_has_unresolved_review_threads(5, ".") is False
