"""BOU-2567 — the deferred-review-thread state store.

Unit coverage of ``_maintenance.deferred_review``: the persisted, thread-id
keyed fact that a review thread was deliberately deferred with a tracked
follow-up. Three-state coverage lives alongside the consumer tests
(test_deferred_review_gate.py, test_deferred_review_loop_dispatch.py,
test_deferred_review_complete.py); this file is the state store in isolation.
"""
from __future__ import annotations

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


def test_is_valid_ticket_accepts_common_formats() -> None:
    assert dr.is_valid_ticket("BOU-2559") is True
    assert dr.is_valid_ticket("ABC-1") is True
    assert dr.is_valid_ticket("") is False
    assert dr.is_valid_ticket(None) is False
    assert dr.is_valid_ticket("random text") is False
    assert dr.is_valid_ticket("BOU") is False
