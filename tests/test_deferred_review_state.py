"""BOU-2567 — the deferred-review-thread state store.

Unit coverage of ``_maintenance.deferred_review``: the persisted, thread-id
keyed fact that a review thread was deliberately deferred with a tracked
follow-up. Three-state coverage lives alongside the consumer tests
(test_deferred_review_gate.py, test_deferred_review_loop_dispatch.py,
test_deferred_review_complete.py); this file is the state store in isolation.
"""
from __future__ import annotations

import fcntl
import os
import threading
import time as _time_mod
from pathlib import Path

import pytest

from agentic_pr_dash._maintenance import deferred_review as dr


def test_undeferred_thread_reads_as_not_deferred(tmp_path: Path) -> None:
    assert dr.is_thread_deferred(str(tmp_path), 1, "T1") is False
    assert dr.deferred_count_for_pr(str(tmp_path), 1) == 0
    assert dr.deferred_threads_for_pr(str(tmp_path), 1) == {}


def test_defer_then_read_round_trips(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    dr.defer_thread(
        cwd, 100, thread_id="T1", comment_id=42, severity="P2",
        ticket="BOU-1", reason="", deferred_by="sess-1",
    )

    assert dr.is_thread_deferred(cwd, 100, "T1") is True
    assert dr.deferred_count_for_pr(cwd, 100) == 1
    record = dr.deferred_threads_for_pr(cwd, 100)["T1"]
    assert record["severity"] == "P2"
    assert record["ticket"] == "BOU-1"
    assert record["deferred_by"] == "sess-1"
    assert record["deferred_at"]  # non-empty timestamp


def test_defer_is_scoped_per_pr(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    dr.defer_thread(cwd, 1, thread_id="T1", comment_id=1, severity="P2", ticket="BOU-1")

    # Same thread_id on a DIFFERENT PR number is not deferred — the fact is
    # keyed (pr, thread_id), matching GitHub's own per-PR thread scoping.
    assert dr.is_thread_deferred(cwd, 2, "T1") is False


def test_persists_across_process_boundary_reads(tmp_path: Path) -> None:
    """A second, independent read (a fresh call, no shared in-memory state)
    sees what a prior call wrote — this is the whole point of a persisted
    store: every consumer (check/stop-gate/complete/loop) is a separate
    process invocation."""
    cwd = str(tmp_path)
    dr.defer_thread(cwd, 5, thread_id="T9", comment_id=9, severity="P1",
                    ticket="BOU-9", reason="out of scope")

    # Fresh call path, no cached object reused.
    assert dr.is_thread_deferred(str(tmp_path), 5, "T9") is True


def test_redeferring_same_thread_is_idempotent_not_an_error(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    dr.defer_thread(cwd, 1, thread_id="T1", comment_id=1, severity="P2", ticket="BOU-1")
    # Re-defer with different details — must replace, not duplicate or error.
    dr.defer_thread(cwd, 1, thread_id="T1", comment_id=1, severity="P1",
                    ticket="BOU-2", reason="actually out of scope")

    assert dr.deferred_count_for_pr(cwd, 1) == 1
    record = dr.deferred_threads_for_pr(cwd, 1)["T1"]
    assert record["severity"] == "P1"
    assert record["ticket"] == "BOU-2"


# --- anti-abuse ---------------------------------------------------------


def test_defer_without_ticket_raises(tmp_path: Path) -> None:
    with pytest.raises(dr.DeferralError, match="ticket"):
        dr.defer_thread(str(tmp_path), 1, thread_id="T1", comment_id=1,
                        severity="P2", ticket="")


def test_defer_with_malformed_ticket_raises(tmp_path: Path) -> None:
    with pytest.raises(dr.DeferralError, match="ticket"):
        dr.defer_thread(str(tmp_path), 1, thread_id="T1", comment_id=1,
                        severity="P2", ticket="not-a-ticket")


def test_p1_defer_without_reason_raises(tmp_path: Path) -> None:
    with pytest.raises(dr.DeferralError, match="reason"):
        dr.defer_thread(str(tmp_path), 1, thread_id="T1", comment_id=1,
                        severity="P1", ticket="BOU-1", reason="")


def test_p1_defer_with_reason_succeeds(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    dr.defer_thread(cwd, 1, thread_id="T1", comment_id=1, severity="P1",
                    ticket="BOU-1", reason="out of scope: requires files this PR does not own")
    assert dr.is_thread_deferred(cwd, 1, "T1") is True


def test_p2_defer_without_reason_succeeds(tmp_path: Path) -> None:
    """P2 does not require a reason — only P1 does (severity x scope gate)."""
    cwd = str(tmp_path)
    dr.defer_thread(cwd, 1, thread_id="T1", comment_id=1, severity="P2", ticket="BOU-1")
    assert dr.is_thread_deferred(cwd, 1, "T1") is True


def test_defer_with_invalid_severity_raises(tmp_path: Path) -> None:
    with pytest.raises(dr.DeferralError, match="severity"):
        dr.defer_thread(str(tmp_path), 1, thread_id="T1", comment_id=1,
                        severity="P3", ticket="BOU-1")


def test_defer_without_thread_id_raises(tmp_path: Path) -> None:
    with pytest.raises(dr.DeferralError, match="thread_id"):
        dr.defer_thread(str(tmp_path), 1, thread_id="", comment_id=1,
                        severity="P2", ticket="BOU-1")


# --- followup ticket -----------------------------------------------------


def test_followup_ticket_round_trips(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    assert dr.followup_ticket_for_pr(cwd, 1) is None
    dr.set_followup_ticket(cwd, 1, "BOU-500")
    assert dr.followup_ticket_for_pr(cwd, 1) == "BOU-500"


def test_set_invalid_followup_ticket_raises(tmp_path: Path) -> None:
    with pytest.raises(dr.DeferralError):
        dr.set_followup_ticket(str(tmp_path), 1, "nope")


# --- three-way thread_state composition -----------------------------------


class _Thread:
    def __init__(self, node_id: str, is_resolved: bool) -> None:
        self.node_id = node_id
        self.is_resolved = is_resolved


def test_thread_state_is_unresolved_by_default(tmp_path: Path) -> None:
    assert dr.thread_state(str(tmp_path), 1, _Thread("T1", False)) == "unresolved"


def test_thread_state_is_deferred_when_recorded(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    dr.defer_thread(cwd, 1, thread_id="T1", comment_id=1, severity="P2", ticket="BOU-1")
    assert dr.thread_state(cwd, 1, _Thread("T1", False)) == "deferred"


def test_thread_state_resolved_wins_over_a_stale_deferral_record(tmp_path: Path) -> None:
    """GitHub resolution is a stronger, later signal than a standing deferral
    record — a thread a human later resolved directly on GitHub must read as
    resolved, not deferred, even if it was deferred earlier."""
    cwd = str(tmp_path)
    dr.defer_thread(cwd, 1, thread_id="T1", comment_id=1, severity="P2", ticket="BOU-1")
    assert dr.thread_state(cwd, 1, _Thread("T1", True)) == "resolved"


# --- BOU-2567 PR #122 review, P1 #1: cross-worktree / cross-consumer visibility ---
#
# A deferral is a fact about (repo, PR, thread) -- not about whichever
# worktree happened to run `complete --defer`. The dashboard's orchestrator
# calls scan_review_threads against the repository root; detached
# reconciliation runs after a worktree is torn down. Both must see a
# deferral recorded from a DIFFERENT worktree of the same repo.


def test_deferral_visible_from_a_different_worktree_of_the_same_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feature_worktree = tmp_path / "feature-wt"
    repo_root = tmp_path / "repo-root"
    feature_worktree.mkdir()
    repo_root.mkdir()

    # Both paths are checkouts of the SAME repo -> same slug in reality (two
    # worktrees, or a worktree + the repository root, share one git remote).
    monkeypatch.setattr(
        "agentic_pr_dash._maintenance._common._repo_slug",
        lambda cwd: "Boundless-Studios/agentic-pr-dash",
    )

    dr.defer_thread(
        str(feature_worktree), 122, thread_id="T1", comment_id=1,
        severity="P2", ticket="BOU-1",
    )

    # A DIFFERENT directory (simulating the repo root / a sibling worktree /
    # the dashboard's own cwd) must see the SAME deferral.
    assert dr.is_thread_deferred(str(repo_root), 122, "T1") is True


def test_deferral_survives_the_deferring_worktree_being_torn_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detached reconciliation runs from an ANCHOR cwd after the worktree that
    recorded the deferral is gone. The record must not have lived inside that
    now-deleted worktree's own directory."""
    import shutil

    feature_worktree = tmp_path / "feature-wt"
    anchor = tmp_path / "anchor"
    feature_worktree.mkdir()
    anchor.mkdir()
    monkeypatch.setattr(
        "agentic_pr_dash._maintenance._common._repo_slug",
        lambda cwd: "Boundless-Studios/agentic-pr-dash",
    )

    dr.defer_thread(
        str(feature_worktree), 999, thread_id="T9", comment_id=9,
        severity="P2", ticket="BOU-9",
    )
    shutil.rmtree(feature_worktree)  # the deferring worktree is torn down

    assert dr.is_thread_deferred(str(anchor), 999, "T9") is True


def test_different_repos_do_not_collide_on_the_same_pr_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store is now shared across the whole machine (like the coordinator
    claim store and the session ledger it mirrors), so two DIFFERENT repos
    reusing the same PR number must not bleed into each other."""
    wt_a = tmp_path / "a"
    wt_b = tmp_path / "b"
    wt_a.mkdir()
    wt_b.mkdir()
    slugs = {str(wt_a): "org/repo-a", str(wt_b): "org/repo-b"}
    monkeypatch.setattr(
        "agentic_pr_dash._maintenance._common._repo_slug",
        lambda cwd: slugs[str(cwd)],
    )

    dr.defer_thread(str(wt_a), 1, thread_id="T1", comment_id=1, severity="P2", ticket="BOU-1")

    assert dr.is_thread_deferred(str(wt_a), 1, "T1") is True
    assert dr.is_thread_deferred(str(wt_b), 1, "T1") is False


def test_is_valid_ticket_accepts_common_formats() -> None:
    assert dr.is_valid_ticket("BOU-2559") is True
    assert dr.is_valid_ticket("ABC-1") is True
    assert dr.is_valid_ticket("") is False
    assert dr.is_valid_ticket(None) is False
    assert dr.is_valid_ticket("random text") is False
    assert dr.is_valid_ticket("BOU") is False


# --- BOU-2567 PR #122 review, round 3, P1: serialize shared-store updates ---
#
# Moving the store to a single machine-wide file (round 2, P1 #1) made it a
# genuinely shared read-modify-write target: several sessions/worktrees can
# now run `complete --defer` / `--sweep-p2` concurrently against the SAME
# file. Without serialization, two concurrent load->mutate->atomic-replace
# cycles can each read the same pre-mutation snapshot and the LATER replace
# silently discards the EARLIER writer's update in full.


def test_concurrent_defers_on_different_threads_never_lose_an_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two threads defer DIFFERENT thread_ids on the SAME PR at (as close to)
    the same instant; both records must survive.

    Forces the race deterministically rather than relying on OS scheduling
    luck: a barrier releases both "writers" together, and the very first
    call through `_save` is slowed down (well past how long an unlocked
    concurrent load+save takes) so that -- WITHOUT a lock -- the second
    writer's whole load->mutate->save cycle races ahead and finishes first,
    and the first writer's later (slow) save then clobbers it outright. WITH
    the lock, the second writer's lock acquisition simply blocks until the
    first writer's entire critical section (including the slow save)
    completes, so no interleave -- and no lost update -- is possible.

    Run against pre-lock code first to confirm this reproduces the loss
    (RED), then against the fix (GREEN).
    """
    monkeypatch.setenv(
        "AGENTIC_PR_DASH_DEFERRED_STORE", str(tmp_path / "shared-store.json")
    )
    monkeypatch.setattr(
        "agentic_pr_dash._maintenance._common._repo_slug",
        lambda cwd: "org/repo",
    )

    barrier = threading.Barrier(2)
    slow_once = threading.Event()
    real_save = dr._save

    def _slow_save(state):
        if not slow_once.is_set():
            slow_once.set()
            _time_mod.sleep(0.3)
        real_save(state)

    monkeypatch.setattr(dr, "_save", _slow_save)

    errors: list[BaseException] = []

    def _writer(thread_id: str, ticket: str) -> None:
        try:
            barrier.wait(timeout=5)
            dr.defer_thread(
                str(tmp_path), 1, thread_id=thread_id, comment_id=1,
                severity="P2", ticket=ticket,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=_writer, args=("A", "BOU-1"))
    t2 = threading.Thread(target=_writer, args=("B", "BOU-2"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, errors
    records = dr.deferred_threads_for_pr(str(tmp_path), 1)
    assert set(records) == {"A", "B"}, (
        f"a concurrent writer's update was lost -- expected both A and B "
        f"to survive, got {set(records)!r}"
    )


def test_lock_acquisition_failure_is_loud_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale/abandoned lock (held by some OTHER process/fd, never
    released) must make `defer_thread` fail LOUDLY and QUICKLY -- never
    hang forever, and never silently proceed as though it wrote the record.
    A write that could not be serialized is not a write that happened."""
    store = tmp_path / "shared-store.json"
    monkeypatch.setenv("AGENTIC_PR_DASH_DEFERRED_STORE", str(store))
    monkeypatch.setenv("AGENTIC_PR_DASH_DEFERRED_LOCK_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setattr(
        "agentic_pr_dash._maintenance._common._repo_slug",
        lambda cwd: "org/repo",
    )

    lock_path = dr._lock_path()
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(holder_fd, fcntl.LOCK_EX)  # simulate a stale/abandoned holder
    try:
        start = _time_mod.monotonic()
        with pytest.raises(dr.DeferredStoreLockTimeout):
            dr.defer_thread(
                str(tmp_path), 1, thread_id="A", comment_id=1,
                severity="P2", ticket="BOU-1",
            )
        elapsed = _time_mod.monotonic() - start
        assert elapsed < 3.0, f"must fail fast, not hang; took {elapsed}s"
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    # Not silently "succeeded" -- nothing was persisted.
    assert dr.is_thread_deferred(str(tmp_path), 1, "A") is False


def test_lock_released_after_timeout_lets_a_later_call_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the stale holder releases, a subsequent call must succeed
    normally -- the timeout path must not leave the lock file itself wedged."""
    store = tmp_path / "shared-store.json"
    monkeypatch.setenv("AGENTIC_PR_DASH_DEFERRED_STORE", str(store))
    monkeypatch.setenv("AGENTIC_PR_DASH_DEFERRED_LOCK_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setattr(
        "agentic_pr_dash._maintenance._common._repo_slug",
        lambda cwd: "org/repo",
    )

    lock_path = dr._lock_path()
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(holder_fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(dr.DeferredStoreLockTimeout):
            dr.defer_thread(
                str(tmp_path), 1, thread_id="A", comment_id=1,
                severity="P2", ticket="BOU-1",
            )
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)

    dr.defer_thread(
        str(tmp_path), 1, thread_id="A", comment_id=1,
        severity="P2", ticket="BOU-1",
    )
    assert dr.is_thread_deferred(str(tmp_path), 1, "A") is True
