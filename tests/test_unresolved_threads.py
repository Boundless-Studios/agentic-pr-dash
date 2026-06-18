from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash import github_api
from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment
from agentic_pr_dash.models import PRData, PRStatus
from agentic_pr_dash._maintenance import markers as _markers_mod
from agentic_pr_dash._maintenance import pr_state as _pr_state_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod


def _thread(resolved=False, outdated=False):
    c = ReviewThreadComment(database_id=42, path="f.py", line=7, body="fix this",
                            author="rev", created_at="2026-01-01T00:00:00Z")
    return ReviewThread(node_id="t1", is_resolved=resolved, is_outdated=outdated, top=c)


def _clean_pr(**kw):
    base = dict(number=5, title="t", branch="b", url="https://x/pull/5",
                failing_checks=[], review_comments=[], merge_state="CLEAN",
                latest_commit_sha="sha", worktree_path="/wt", status=PRStatus.CLEAN)
    base.update(kw)
    return PRData(**base)


def _patch_check_env(monkeypatch):
    """Neutralize ownership/heartbeat side effects so _check_worktree is pure."""
    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(_worktrees_mod, "_live_independent_owner_paths", lambda paths, sid: set())
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda cwd: None)
    monkeypatch.setattr(_markers_mod, "_touch_owner_heartbeat", lambda *a, **k: None)


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


# --- round-2 #3: get_unaddressed_comments drops outdated threads -------------

def test_get_unaddressed_comments_skips_outdated_thread(monkeypatch):
    monkeypatch.setattr(github_api, "get_review_threads",
                        lambda pr, cwd=None: [_thread(outdated=True)])
    # No CHANGES_REQUESTED reviews.
    monkeypatch.setattr(github_api, "_run",
                        lambda *a, **k: __import__("subprocess").CompletedProcess([], 0, "", ""))
    assert github_api.get_unaddressed_comments(5, "2026-01-01T00:00:00Z", ".") == []


# --- round-2 #3: outdated-only thread keeps a green-CI PR clean --------------

def test_check_outdated_only_thread_stays_clean(monkeypatch):
    _patch_check_env(monkeypatch)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: _clean_pr())
    # Authoritative thread check sees only an outdated thread -> not actionable.
    monkeypatch.setattr(github_api, "get_review_threads",
                        lambda pr, cwd=None: [_thread(outdated=True)])
    code, text = mc._check_worktree("/wt", "sess-A")
    assert code == 0
    assert text == "nothing pending"


# --- round-2 #4: old unresolved thread blocks AND hydrates the prompt --------

def test_check_unresolved_thread_blocks_with_details(monkeypatch):
    _patch_check_env(monkeypatch)
    # PR resolves clean (get_unaddressed_comments missed the old thread).
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: _clean_pr())
    monkeypatch.setattr(github_api, "get_review_threads",
                        lambda pr, cwd=None: [_thread()])
    code, text = mc._check_worktree("/wt", "sess-A")
    assert code == 10
    # The synthetic blocker hydrated the thread into the prompt with file/line/body.
    assert "## Review Comments" in text
    assert "f.py:7" in text
    assert "fix this" in text
    assert "Comment 42 by @rev" in text
    assert "PR_NUMBER=5" in text
