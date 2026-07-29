"""BOU-1924: the feedback waiter is SESSION-scoped, not per-worktree.

A session working N PRs across ONE moving worktree must hold a SINGLE waiter
that covers ALL its owned PRs, so ``_await_alive`` is true for any owned PR
regardless of which branch is currently checked out. Previously the pidfile
lived under the *worktree's* state dir, so ``_await_alive(other_worktree,
session)`` read False for the session's other owned PRs and the loop treated a
live stacked session as wake-less (BOU-1879 / #60 take-over firing against a
live session).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentic_pr_dash._maintenance import waiter

SID = "sess-1924"
PROCESS_IDENTITY = "Mon Jul 27 12:34:56 2026"


@pytest.fixture(autouse=True)
def _isolate_ledger(tmp_path, monkeypatch):
    # session_ledger._DEFAULT_DIR is frozen at import, so isolate per test.
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    # These tests use the pytest process as a stand-in for a live waiter. The
    # process-identity behavior itself is covered by test_await.py.
    monkeypatch.setattr(
        waiter,
        "_process_identity",
        lambda pid: PROCESS_IDENTITY,
    )


def _write_session_pidfile(session_id: str, pid: int, **extra) -> None:
    p = Path(waiter._await_pidfile("", session_id))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "pid": pid,
                "session_id": session_id,
                "process_identity": PROCESS_IDENTITY,
                **extra,
            }
        ),
        encoding="utf-8",
    )


def test_pidfile_is_session_scoped_across_worktrees(tmp_path):
    a = tmp_path / "wtA"
    b = tmp_path / "wtB"
    a.mkdir()
    b.mkdir()
    pa = waiter._await_pidfile(str(a), SID)
    pb = waiter._await_pidfile(str(b), SID)
    # ONE pidfile for the session, independent of the querying worktree.
    assert pa == pb
    # ...and NOT nested under either worktree.
    assert not pa.startswith(str(a))
    assert not pa.startswith(str(b))


def test_await_alive_true_across_worktrees(tmp_path):
    a = tmp_path / "wtA"
    b = tmp_path / "wtB"
    a.mkdir()
    b.mkdir()
    _write_session_pidfile(SID, os.getpid())  # our pid is definitely alive
    # Launched "from" A, queried "from" B → still alive (session-scoped).
    assert waiter._await_alive(str(b), SID) is True


def test_await_alive_false_for_dead_pid(tmp_path):
    _write_session_pidfile(SID, 2147480000)  # unassigned high pid
    assert waiter._await_alive(str(tmp_path), SID) is False


def test_await_alive_ignores_mismatched_session(tmp_path):
    _write_session_pidfile("other-session", os.getpid())
    assert waiter._await_alive(str(tmp_path), SID) is False


def test_await_anchors_span_ledger_repos(tmp_path, monkeypatch):
    """PR #61 review (P1): the single session waiter must poll every repo the
    session owns PRs in — anchor at each still-present ledger worktree, not just
    the launch cwd. A ledger worktree that no longer exists is skipped."""
    from agentic_pr_dash import maintenance_check as mc
    from agentic_pr_dash import session_ledger as sl

    launch = tmp_path / "gaia-wt"
    other = tmp_path / "other-repo-wt"
    gone = tmp_path / "torn-down-wt"
    launch.mkdir()
    other.mkdir()  # exists, in a different repo → must be anchored
    # `gone` is never created → must be skipped

    sl.append("sess-A", pr=1, branch="b1", worktree=str(other), repo="o/other")
    sl.append("sess-A", pr=2, branch="b2", worktree=str(gone), repo="o/gone")

    anchors = mc._await_anchors("sess-A", str(launch))
    assert str(launch) in anchors
    assert str(other) in anchors
    assert str(gone) not in anchors


def test_await_alive_does_not_claim_uncovered_markerless_repo(tmp_path, monkeypatch):
    from agentic_pr_dash._maintenance import markers

    covered = tmp_path / "covered"
    other = tmp_path / "markerless-other"
    covered.mkdir()
    other.mkdir()
    _write_session_pidfile(SID, os.getpid(), covered_roots=[str(covered)])
    monkeypatch.setattr(markers, "_marker_session_id", lambda cwd: None)

    assert waiter._await_alive(str(covered), SID) is True
    assert waiter._await_alive(str(other), SID) is False


def test_await_alive_requests_coverage_for_marker_owned_repo(tmp_path, monkeypatch):
    from agentic_pr_dash import maintenance_check as mc
    from agentic_pr_dash._maintenance import markers

    covered = tmp_path / "covered"
    marker_only = tmp_path / "marker-only"
    covered.mkdir()
    marker_only.mkdir()
    _write_session_pidfile(SID, os.getpid(), covered_roots=[str(covered)])
    monkeypatch.setattr(
        markers,
        "_marker_session_id",
        lambda cwd: SID if Path(cwd) == marker_only else None,
    )

    assert waiter._await_alive(str(marker_only), SID) is True
    data = waiter._read_await_pidfile("", SID)
    assert str(marker_only) in data["requested_roots"]
    assert str(marker_only) in mc._await_anchors(SID, str(covered))


def test_await_alive_legacy_per_worktree_pidfile_honored(tmp_path):
    """An in-flight session that started its waiter BEFORE the upgrade wrote a
    per-worktree pidfile; dual-read must still see it alive (back-compat)."""
    wt = tmp_path / "wt"
    wt.mkdir()
    legacy = Path(waiter._legacy_await_pidfile(str(wt), SID))
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps({"pid": os.getpid(), "session_id": SID}), encoding="utf-8"
    )
    assert waiter._await_alive(str(wt), SID) is True


def test_await_alive_identityless_session_pidfile_honored_during_upgrade(tmp_path):
    """A pre-upgrade waiter may already use the session-scoped path while still
    writing the old identity-less payload. Its start time must be validated
    against the pidfile mtime just like the legacy per-worktree format."""
    wt = tmp_path / "wt"
    wt.mkdir()
    session = Path(waiter._await_pidfile("", SID))
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "session_id": SID,
                "covered_roots": [str(wt)],
            }
        ),
        encoding="utf-8",
    )

    assert waiter._await_alive(str(wt), SID) is True
