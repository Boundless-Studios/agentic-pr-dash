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

from agentic_pr_dash._maintenance import waiter

SID = "sess-1924"


def _write_session_pidfile(session_id: str, pid: int) -> None:
    p = Path(waiter._await_pidfile("", session_id))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"pid": pid, "session_id": session_id}), encoding="utf-8")


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
