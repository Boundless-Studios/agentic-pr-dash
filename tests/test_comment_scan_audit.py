"""BOU-1801 — scan_review_threads produces per-thread audit decisions.

Verifies that scan_review_threads:
  - emits the correct `decision` for each skip/pick outcome
  - its picked_comments list is identical to get_unaddressed_comments for a
    mixed fixture (parity guarantee)

Stubs only the gh API boundary (get_review_threads / _run); the state machine
is exercised end-to-end.
"""

from types import SimpleNamespace
import json

from agentic_pr_dash import github_api
from agentic_pr_dash._maintenance import deferred_review
from agentic_pr_dash.github_api import (
    CLAIM_MARKER,
    COMPLETE_MARKER,
    FAILED_MARKER,
    ReviewThread,
    ReviewThreadComment,
    get_unaddressed_comments,
    scan_review_threads,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _comment(author: str, body: str, created_at: str, *, path: str = "src/foo.py", line: int = 1, db_id: int = 1) -> ReviewThreadComment:
    return ReviewThreadComment(
        database_id=db_id,
        path=path,
        line=line,
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


def _wire(monkeypatch, threads: list[ReviewThread]) -> None:
    monkeypatch.setattr(github_api, "get_review_threads", lambda *a, **k: threads)
    _no_review_level_comments(monkeypatch)


LATEST = "2026-06-27T09:00:00Z"


def test_deferred_review_body_is_not_returned_to_legacy_automation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(github_api, "get_review_threads", lambda *a, **k: [])
    review = {
        "id": 123,
        "author": "reviewer",
        "body": "[P2] Unsupported edge case.",
        "state": "CHANGES_REQUESTED",
        "submitted_at": "2026-06-27T10:00:00Z",
    }
    monkeypatch.setattr(
        github_api,
        "_run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(review),
            stderr="",
        ),
    )
    monkeypatch.setattr(
        deferred_review,
        "deferred_threads_for_pr",
        lambda *a, **k: {
            "review:123": {
                "reason": "No supported-path reproduction.",
            }
        },
    )

    comments, _ = scan_review_threads(1, LATEST, cwd=".")

    assert comments == []


# ---------------------------------------------------------------------------
# Individual decision type tests
# ---------------------------------------------------------------------------

def test_decision_skip_resolved(monkeypatch):
    thread = ReviewThread(
        node_id="T_RESOLVED",
        is_resolved=True,
        is_outdated=False,
        top=_comment("reviewer", "fix this", "2026-06-27T10:00:00Z", db_id=10),
    )
    _wire(monkeypatch, [thread])

    _, decisions = scan_review_threads(1, LATEST, cwd=".")

    assert len(decisions) == 1
    d = decisions[0]
    assert d.thread_id == "T_RESOLVED"
    assert d.decision == "SKIP_RESOLVED"
    assert d.author == "reviewer"


def test_decision_skip_outdated(monkeypatch):
    thread = ReviewThread(
        node_id="T_OUTDATED",
        is_resolved=False,
        is_outdated=True,
        top=_comment("reviewer", "outdated comment", "2026-06-27T10:00:00Z", db_id=20),
    )
    _wire(monkeypatch, [thread])

    _, decisions = scan_review_threads(1, LATEST, cwd=".")

    assert len(decisions) == 1
    d = decisions[0]
    assert d.thread_id == "T_OUTDATED"
    assert d.decision == "SKIP_OUTDATED"


def test_decision_skip_addressed(monkeypatch):
    """A thread with a COMPLETE_MARKER reply → SKIP_ADDRESSED."""
    thread = ReviewThread(
        node_id="T_ADDRESSED",
        is_resolved=False,
        is_outdated=False,
        top=_comment("reviewer", "fix the typo", "2026-06-27T10:00:00Z", db_id=30),
        replies=[
            _comment("agent", f"done {COMPLETE_MARKER}", "2026-06-27T10:05:00Z"),
        ],
    )
    _wire(monkeypatch, [thread])

    _, decisions = scan_review_threads(1, LATEST, cwd=".")

    assert len(decisions) == 1
    d = decisions[0]
    assert d.thread_id == "T_ADDRESSED"
    assert d.decision == "SKIP_ADDRESSED"
    assert d.marker_state == "completed"


def test_decision_skip_human_resolved_different_author(monkeypatch):
    """A non-marker reply from a DIFFERENT author than the top → SKIP_HUMAN_RESOLVED."""
    thread = ReviewThread(
        node_id="T_HUMAN",
        is_resolved=False,
        is_outdated=False,
        top=_comment("reviewer", "please fix this", "2026-06-27T10:00:00Z", db_id=40),
        replies=[
            _comment("different-human", "LGTM, merged this approach", "2026-06-27T10:05:00Z"),
        ],
    )
    _wire(monkeypatch, [thread])

    _, decisions = scan_review_threads(1, LATEST, cwd=".")

    assert len(decisions) == 1
    d = decisions[0]
    assert d.thread_id == "T_HUMAN"
    assert d.decision == "SKIP_HUMAN_RESOLVED"
    assert d.marker_state == "human_resolved"


def test_decision_skip_claimed_active(monkeypatch):
    """A thread with a fresh CLAIM_MARKER → SKIP_CLAIMED_ACTIVE."""
    import time
    recent_ts = "2026-06-27T10:59:00Z"  # well within STALE_CLAIM_SECONDS (3600s)

    # We need the claim to be "recent" relative to now; patch datetime to control time
    # instead, just use a claim from the near future (within 1h of a known-recent ts).
    # Since we can't mock datetime.now easily, use a timestamp that would be recent
    # given the test runs close to 2026-06-27. We'll use a truly future-relative trick:
    # compute a ts that's 5 minutes before "now" in real wall time.
    from datetime import datetime, timezone, timedelta
    now_ts = datetime.now(timezone.utc)
    claim_ts = (now_ts - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    thread = ReviewThread(
        node_id="T_CLAIMED",
        is_resolved=False,
        is_outdated=False,
        top=_comment("reviewer", "please rename", "2026-06-27T10:00:00Z", db_id=50),
        replies=[
            _comment("agent", f"working on it {CLAIM_MARKER}", claim_ts),
        ],
    )
    _wire(monkeypatch, [thread])

    _, decisions = scan_review_threads(1, LATEST, cwd=".")

    assert len(decisions) == 1
    d = decisions[0]
    assert d.thread_id == "T_CLAIMED"
    assert d.decision == "SKIP_CLAIMED_ACTIVE"
    assert d.marker_state == "claimed"
    assert d.claim_age_seconds is not None
    assert d.claim_age_seconds < 3600  # active claim


def test_decision_picked_open_thread(monkeypatch):
    """A thread with no replies → PICKED."""
    thread = ReviewThread(
        node_id="T_OPEN",
        is_resolved=False,
        is_outdated=False,
        top=_comment("reviewer", "add docstring", "2026-06-27T10:00:00Z", db_id=60),
    )
    _wire(monkeypatch, [thread])

    picked, decisions = scan_review_threads(1, LATEST, cwd=".")

    assert len(decisions) == 1
    d = decisions[0]
    assert d.thread_id == "T_OPEN"
    assert d.decision == "PICKED"

    assert len(picked) == 1
    assert picked[0].thread_id == "T_OPEN"


def test_decision_picked_after_failed_marker(monkeypatch):
    """A thread whose last marker is FAILED (no human reply after) → still picked (failed = retry needed)."""
    thread = ReviewThread(
        node_id="T_FAILED",
        is_resolved=False,
        is_outdated=False,
        top=_comment("reviewer", "needs fix", "2026-06-27T10:00:00Z", db_id=70),
        replies=[
            _comment("agent", f"push failed {FAILED_MARKER}", "2026-06-27T10:05:00Z"),
        ],
    )
    _wire(monkeypatch, [thread])

    picked, decisions = scan_review_threads(1, LATEST, cwd=".")

    # failed state → not addressed → thread is PICKED
    assert len(decisions) == 1
    d = decisions[0]
    assert d.thread_id == "T_FAILED"
    assert d.decision == "PICKED"
    assert len(picked) == 1


# ---------------------------------------------------------------------------
# Parity test: scan_review_threads[0] == get_unaddressed_comments for mixed fixture
# ---------------------------------------------------------------------------

def test_parity_with_get_unaddressed_comments(monkeypatch):
    """scan_review_threads()[0] must equal get_unaddressed_comments() for a mixed fixture."""
    from datetime import datetime, timezone, timedelta
    now_ts = datetime.now(timezone.utc)
    claim_ts = (now_ts - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    threads = [
        # PICKED — no replies
        ReviewThread(
            node_id="MIX_OPEN",
            is_resolved=False,
            is_outdated=False,
            top=_comment("rev-a", "fix this", "2026-06-27T10:00:00Z", db_id=101),
        ),
        # SKIP_RESOLVED
        ReviewThread(
            node_id="MIX_RESOLVED",
            is_resolved=True,
            is_outdated=False,
            top=_comment("rev-b", "old comment", "2026-06-27T09:00:00Z", db_id=102),
        ),
        # SKIP_ADDRESSED
        ReviewThread(
            node_id="MIX_ADDRESSED",
            is_resolved=False,
            is_outdated=False,
            top=_comment("rev-c", "rename this", "2026-06-27T10:00:00Z", db_id=103),
            replies=[
                _comment("agent", f"done {COMPLETE_MARKER}", "2026-06-27T10:10:00Z"),
            ],
        ),
        # SKIP_HUMAN_RESOLVED (different author)
        ReviewThread(
            node_id="MIX_HUMAN",
            is_resolved=False,
            is_outdated=False,
            top=_comment("rev-d", "consider this", "2026-06-27T10:00:00Z", db_id=104),
            replies=[
                _comment("other-human", "I handled it", "2026-06-27T10:06:00Z"),
            ],
        ),
        # PICKED — failed push, not addressed yet
        ReviewThread(
            node_id="MIX_FAILED",
            is_resolved=False,
            is_outdated=False,
            top=_comment("rev-e", "add logging", "2026-06-27T10:00:00Z", db_id=105),
            replies=[
                _comment("agent", f"push failed {FAILED_MARKER}", "2026-06-27T10:07:00Z"),
            ],
        ),
    ]

    monkeypatch.setattr(github_api, "get_review_threads", lambda *a, **k: threads)
    _no_review_level_comments(monkeypatch)

    picked, decisions = scan_review_threads(1, LATEST, cwd=".")
    direct = get_unaddressed_comments(1, LATEST, cwd=".")

    assert picked == direct, (
        f"scan_review_threads()[0] diverged from get_unaddressed_comments(): "
        f"scan={[c.thread_id for c in picked]}, direct={[c.thread_id for c in direct]}"
    )

    # Sanity: only the two PICKED threads appear
    picked_ids = {c.thread_id for c in picked}
    assert "MIX_OPEN" in picked_ids
    assert "MIX_FAILED" in picked_ids
    assert "MIX_RESOLVED" not in picked_ids
    assert "MIX_ADDRESSED" not in picked_ids
    assert "MIX_HUMAN" not in picked_ids

    # Decision count matches thread count (one per inline thread)
    assert len(decisions) == 5

    decision_map = {d.thread_id: d.decision for d in decisions}
    assert decision_map["MIX_OPEN"] == "PICKED"
    assert decision_map["MIX_RESOLVED"] == "SKIP_RESOLVED"
    assert decision_map["MIX_ADDRESSED"] == "SKIP_ADDRESSED"
    assert decision_map["MIX_HUMAN"] == "SKIP_HUMAN_RESOLVED"
    assert decision_map["MIX_FAILED"] == "PICKED"
