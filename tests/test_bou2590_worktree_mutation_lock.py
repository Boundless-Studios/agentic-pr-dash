"""BOU-2590: one worktree, one writer.

The ownership marker answers "who is responsible for this PR" and already stops
duplicate maintenance dispatch. It does NOT stop two actors writing the same
files, and during gaia PR #2875 the detached loop's executor committed the live
session's in-progress work and started a merge underneath it — leaving
``MERGE_HEAD``, a conflict, and ambiguous authorship for the session to untangle.

These tests pin the acceptance criteria on the ticket: exactly one actor may
mutate at a time, the loser stands down WITHOUT touching git state, stale locks
recover from durable pid liveness, and release is fenced.
"""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from agentic_pr_dash import loop
from agentic_pr_dash._maintenance import mutation_lock


def test_a_live_session_holder_makes_the_executor_stand_down(tmp_path, monkeypatch):
    """The headline case: session is editing, loop wants to dispatch.

    The loop must not run the executor at all — not run it and lose a race.
    """
    wt = tmp_path / "worktree"
    wt.mkdir()

    # A live session holds the worktree. Its pid is this process, which is alive.
    # pid 1 stands in for a DIFFERENT live process: the lock is re-entrant per
    # pid, so a holder recorded as this process would (correctly) let the loop
    # straight through and the test would prove nothing.
    mutation_lock.acquire(str(wt), actor="session", session_id="sess-live", pid=1)

    spawned: list[list[str]] = []
    monkeypatch.setattr(
        loop.subprocess,
        "run",
        lambda parts, **kw: spawned.append(list(parts)) or SimpleNamespace(returncode=0),
    )

    rc = loop._run_executor("echo {prompt}", "fix the PR", str(wt))

    assert rc == loop.EXECUTOR_STOOD_DOWN, (
        "the loop reported a normal result while another actor held the worktree"
    )
    assert spawned == [], (
        "the executor was SPAWNED against a worktree a live session is editing "
        "-- this is the concurrent-mutation bug, not a race it can win"
    )


def test_stand_down_is_not_counted_as_an_executor_failure(tmp_path, monkeypatch):
    """Standing down must not feed the per-PR failure streak.

    Otherwise a PR whose only problem is that its owner is currently editing it
    accrues failures and eventually escalates.
    """
    wt = tmp_path / "worktree"
    wt.mkdir()
    # pid 1 stands in for a DIFFERENT live process: the lock is re-entrant per
    # pid, so a holder recorded as this process would (correctly) let the loop
    # straight through and the test would prove nothing.
    mutation_lock.acquire(str(wt), actor="session", session_id="sess-live", pid=1)

    monkeypatch.setattr(
        loop.subprocess, "run", lambda parts, **kw: SimpleNamespace(returncode=0)
    )

    serviced, errors = loop._dispatch_with_fallback(
        "echo {prompt}", "echo fallback {prompt}", "prompt", str(wt), 2875
    )

    assert serviced is False, "nothing ran, so the PR was not serviced"
    assert errors == {}, (
        "a stand-down was reported as an executor failure; it would feed the "
        f"streak and escalate a PR nobody has actually failed on. Got: {errors}"
    )


def test_the_fallback_executor_does_not_queue_behind_the_same_lock(
    tmp_path, monkeypatch
):
    """Control on the above: the fallback must not be attempted either."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    # pid 1 stands in for a DIFFERENT live process: the lock is re-entrant per
    # pid, so a holder recorded as this process would (correctly) let the loop
    # straight through and the test would prove nothing.
    mutation_lock.acquire(str(wt), actor="session", session_id="sess-live", pid=1)

    spawned: list[list[str]] = []
    monkeypatch.setattr(
        loop.subprocess,
        "run",
        lambda parts, **kw: spawned.append(list(parts)) or SimpleNamespace(returncode=0),
    )

    loop._dispatch_with_fallback(
        "primary {prompt}", "fallback {prompt}", "p", str(wt), 2875
    )

    assert spawned == [], f"fallback ran against the held worktree: {spawned}"


def test_the_loser_reports_who_holds_the_worktree(tmp_path):
    """"Busy" is useless without naming the holder — that is what made the
    original incident take a manual commit-attribution dig."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    mutation_lock.acquire(str(wt), actor="session", session_id="sess-abcdef123")

    with pytest.raises(mutation_lock.WorktreeBusy) as caught:
        mutation_lock.acquire(str(wt), actor="pr-maintenance-loop", pid=999999)

    message = str(caught.value)
    assert "session" in message
    assert "sess-abc" in message, f"holder session not named: {message}"
    assert str(os.getpid()) in message, f"holder pid not named: {message}"


def test_a_dead_holders_lock_is_reclaimed(tmp_path):
    """Stale locks recover from durable pid liveness, not a timeout.

    A TTL would either strand the worktree (too long) or hand it to a second
    writer mid-commit (too short); "is the holder still running" is the question
    actually being asked.
    """
    wt = tmp_path / "worktree"
    wt.mkdir()
    # PID 1 exists, so use a pid that cannot: 0 is never a live process here.
    mutation_lock.acquire(str(wt), actor="pr-maintenance-loop", pid=0)

    taken = mutation_lock.acquire(str(wt), actor="session", session_id="sess-new")

    assert taken.actor == "session"
    assert mutation_lock.read_owner(str(wt)).session_id == "sess-new"


def test_release_is_fenced_against_a_superseded_holder(tmp_path):
    """A stale holder waking up late must not release its successor's lock."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    stale = mutation_lock.acquire(str(wt), actor="pr-maintenance-loop", pid=0)
    successor = mutation_lock.acquire(str(wt), actor="session", session_id="sess-new")

    assert mutation_lock.release(str(wt), stale) is False, (
        "the superseded holder released a lock it no longer owns"
    )
    assert mutation_lock.read_owner(str(wt)) is not None, "lock was dropped"
    assert mutation_lock.release(str(wt), successor) is True
    assert mutation_lock.read_owner(str(wt)) is None


def test_reacquire_by_the_same_process_does_not_deadlock(tmp_path):
    """Re-entrancy: a holder taking its own lock again gets it back."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    first = mutation_lock.acquire(str(wt), actor="session")
    again = mutation_lock.acquire(str(wt), actor="session")

    assert again.token == first.token


def test_lock_is_released_when_the_executor_finishes(tmp_path, monkeypatch):
    """The loop must not leave the worktree locked after a dispatch."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    monkeypatch.setattr(
        loop.subprocess, "run", lambda parts, **kw: SimpleNamespace(returncode=0)
    )

    assert loop._run_executor("echo {prompt}", "p", str(wt)) == 0
    assert mutation_lock.read_owner(str(wt)) is None, (
        "the worktree stayed locked after the executor exited -- the next "
        "session to open it would be told it is busy forever"
    )


def test_lock_is_released_even_when_the_executor_raises(tmp_path, monkeypatch):
    """A crashing executor must not strand the lock either."""
    wt = tmp_path / "worktree"
    wt.mkdir()

    def _boom(parts, **kw):
        raise OSError("no such binary")

    monkeypatch.setattr(loop.subprocess, "run", _boom)

    with pytest.raises(OSError):
        loop._run_executor("missing {prompt}", "p", str(wt))

    assert mutation_lock.read_owner(str(wt)) is None


def test_standing_down_leaves_git_state_untouched(tmp_path, monkeypatch):
    """Acceptance criterion, end to end against a REAL repo.

    The loser must change nothing: same HEAD, same status, no MERGE_HEAD — the
    exact residue the original incident left behind.
    """
    wt = tmp_path / "repo"
    wt.mkdir()

    # Pinned BEFORE the monkeypatch below: `loop.subprocess` is the same module
    # object as this one, so patching the loop's runner would otherwise replace
    # the runner this helper needs to observe the repo afterwards.
    real_run = subprocess.run

    def git(*args: str) -> str:
        return real_run(
            ["git", "-C", str(wt), *args],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (wt / "f.txt").write_text("one\n", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-qm", "first")

    # A live session holds the worktree and has uncommitted work in flight.
    (wt / "f.txt").write_text("two\n", encoding="utf-8")
    # pid 1 stands in for a DIFFERENT live process: the lock is re-entrant per
    # pid, so a holder recorded as this process would (correctly) let the loop
    # straight through and the test would prove nothing.
    mutation_lock.acquire(str(wt), actor="session", session_id="sess-live", pid=1)

    head_before = git("rev-parse", "HEAD")
    status_before = git("status", "--porcelain")

    monkeypatch.setattr(
        loop.subprocess, "run", lambda parts, **kw: SimpleNamespace(returncode=0)
    )
    rc = loop._run_executor("echo {prompt}", "fix", str(wt))

    assert rc == loop.EXECUTOR_STOOD_DOWN
    assert git("rev-parse", "HEAD") == head_before, "the loser moved HEAD"
    assert git("status", "--porcelain") == status_before, (
        "the loser changed the working tree out from under the session"
    )
    assert not (wt / ".git" / "MERGE_HEAD").exists(), (
        "the loser started a merge -- the exact residue of the incident"
    )
    assert (wt / "f.txt").read_text(encoding="utf-8") == "two\n"
