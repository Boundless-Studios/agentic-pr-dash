"""Focused tests for the BOU-1637 PR-dash hardening batch.

Each test targets exactly one of the 8 items shipped in this PR (item 4 —
orchestrator parallel enrichment — ships in a sibling PR):

  1. ledger claim-file pruning
  2. session-registry compaction + bounded read
  3. maintenance state pruning
  5. fix_lease_until parse hardening (corrupt -> active/defer)
  6. executor validation at loop startup
  7. heartbeat write coalescing (not rewritten when unchanged)
  8. ledger append is a true append, read still dedups
  9. claim-fingerprint gating (same-fp defers, new-fp surfaces)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest


# ---------------------------------------------------------------------------
# Item 1 — ledger claim-file pruning
# ---------------------------------------------------------------------------


def test_prune_removes_claim_file(tmp_path, monkeypatch):
    from agentic_pr_dash import session_ledger as sl

    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("GAIA_PR_CLAIM_DIR", str(tmp_path / "claims"))

    sl.append("sess-A", pr=7, branch="b7", worktree="/wt7", repo="o/r")
    sl.write_claim(7, "sess-A", pid=123, repo="o/r")
    claim_file = sl.claim_path(7, "o/r")
    assert os.path.exists(claim_file)

    sl.prune("sess-A", {7}, repo="o/r")

    assert [e.pr for e in sl.read("sess-A")] == []
    assert not os.path.exists(claim_file), "claim file should be removed with the ledger entry"


def test_prune_only_removes_dropped_entries_claim(tmp_path, monkeypatch):
    from agentic_pr_dash import session_ledger as sl

    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("GAIA_PR_CLAIM_DIR", str(tmp_path / "claims"))

    sl.append("sess-A", pr=7, branch="b7", worktree="/wt7", repo="o/r")
    sl.append("sess-A", pr=8, branch="b8", worktree="/wt8", repo="o/r")
    sl.write_claim(7, "sess-A", pid=1, repo="o/r")
    sl.write_claim(8, "sess-A", pid=1, repo="o/r")

    sl.prune("sess-A", {7}, repo="o/r")

    assert not os.path.exists(sl.claim_path(7, "o/r"))
    assert os.path.exists(sl.claim_path(8, "o/r")), "surviving entry's claim must be kept"


# ---------------------------------------------------------------------------
# Item 8 — ledger append is a true append, read still dedups
# ---------------------------------------------------------------------------


def test_append_is_true_append_and_read_dedups(tmp_path, monkeypatch):
    from agentic_pr_dash import session_ledger as sl

    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path))

    sl.append("sess-A", pr=1, branch="old", worktree="/wt/old")
    sl.append("sess-A", pr=1, branch="new", worktree="/wt/new")  # re-arm same PR

    # The on-disk file has TWO physical lines (true append, no rewrite)...
    raw = open(sl.ledger_path("sess-A"), encoding="utf-8").read().splitlines()
    assert len(raw) == 2, "append must NOT rewrite the file; it appends a new line"

    # ...but read() collapses to the last-wins entry.
    entries = sl.read("sess-A")
    assert len(entries) == 1
    assert entries[0].branch == "new" and entries[0].worktree == "/wt/new"


def test_compact_reclaims_superseded_lines(tmp_path, monkeypatch):
    from agentic_pr_dash import session_ledger as sl

    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path))
    for branch in ("a", "b", "c"):
        sl.append("sess-A", pr=1, branch=branch, worktree="/wt")
    assert len(open(sl.ledger_path("sess-A")).read().splitlines()) == 3

    sl.compact("sess-A")

    raw = open(sl.ledger_path("sess-A")).read().splitlines()
    assert len(raw) == 1, "compact collapses duplicate (repo, pr) lines"
    assert sl.read("sess-A")[0].branch == "c"  # contents unchanged (last wins)


def test_append_self_compacts_over_threshold(tmp_path, monkeypatch):
    from agentic_pr_dash import session_ledger as sl

    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv("GAIA_PR_LEDGER_COMPACT_THRESHOLD", "5")

    # Re-arm the same PR many times; the physical file must stay BOUNDED near the
    # threshold (it self-compacts) instead of growing once per append, and read()
    # still returns the single last-wins entry.
    for i in range(40):
        sl.append("sess-A", pr=1, branch=f"b{i}", worktree="/wt")

    raw = open(sl.ledger_path("sess-A")).read().splitlines()
    assert len(raw) <= 5, f"append should keep the ledger bounded; got {len(raw)} lines"
    assert sl.read("sess-A")[0].branch == "b39"  # last write wins


# ---------------------------------------------------------------------------
# Item 2 — session-registry compaction + bounded read
# ---------------------------------------------------------------------------


def test_read_events_applies_default_tail_cap(tmp_path, monkeypatch):
    from agentic_pr_dash import session_registry as sr

    monkeypatch.setenv("AGENTIC_PR_DASH_REGISTRY_READ_LIMIT", "5")
    reg = tmp_path / "events.jsonl"
    with reg.open("w", encoding="utf-8") as fh:
        for i in range(50):
            fh.write(json.dumps({"session_id": f"s{i}", "event": "started", "timestamp": "x"}) + "\n")

    events = sr.read_events(path=reg)
    assert len(events) == 5, "unbounded read must apply the default tail cap"
    # The tail (most-recent) events are the ones kept.
    assert events[-1]["session_id"] == "s49"


def test_read_events_cap_disabled_by_zero(tmp_path, monkeypatch):
    from agentic_pr_dash import session_registry as sr

    monkeypatch.setenv("AGENTIC_PR_DASH_REGISTRY_READ_LIMIT", "0")
    reg = tmp_path / "events.jsonl"
    with reg.open("w", encoding="utf-8") as fh:
        for i in range(50):
            fh.write(json.dumps({"session_id": f"s{i}", "event": "started", "timestamp": "x"}) + "\n")
    assert len(sr.read_events(path=reg)) == 50


def test_compact_registry_drops_old_terminal_sessions(tmp_path):
    from agentic_pr_dash import session_registry as sr

    reg = tmp_path / "events.jsonl"
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    rows = [
        {"session_id": "dead-old", "event": "completed", "timestamp": old},
        {"session_id": "live", "event": "started", "timestamp": old},  # non-terminal: keep
        {"session_id": "recent-done", "event": "completed", "timestamp": recent},  # in window: keep
    ]
    with reg.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    removed = sr.compact_registry(path=reg, retention_seconds=7 * 24 * 3600)
    assert removed == 1

    kept_ids = {e["session_id"] for e in sr.read_events(path=reg)}
    assert kept_ids == {"live", "recent-done"}
    assert "dead-old" not in kept_ids


def test_compact_registry_keeps_live_session_even_if_old(tmp_path):
    from agentic_pr_dash import session_registry as sr

    reg = tmp_path / "events.jsonl"
    old = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat().replace("+00:00", "Z")
    with reg.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"session_id": "live", "event": "started", "timestamp": old}) + "\n")

    assert sr.compact_registry(path=reg) == 0
    assert sr.read_events(path=reg)[0]["session_id"] == "live"


def test_record_event_self_compacts_over_threshold(tmp_path, monkeypatch):
    from agentic_pr_dash import session_registry as sr

    monkeypatch.setenv("AGENTIC_PR_DASH_REGISTRY_COMPACT_THRESHOLD", "10")
    monkeypatch.setenv("AGENTIC_PR_DASH_REGISTRY_READ_LIMIT", "0")  # don't cap the assert read
    reg = tmp_path / "events.jsonl"

    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    with reg.open("w", encoding="utf-8") as fh:
        for i in range(12):
            fh.write(json.dumps({"session_id": f"old{i}", "event": "completed", "timestamp": old}) + "\n")

    # This write crosses the threshold and should trigger compaction of the old
    # terminal sessions (only the just-written live event survives).
    sr.record_event(event="started", session_id="live", path=reg)

    ids = {e["session_id"] for e in sr.read_events(path=reg)}
    assert ids == {"live"}, "self-compaction should drop the old terminal sessions"


# ---------------------------------------------------------------------------
# Item 3 — maintenance state pruning
# ---------------------------------------------------------------------------


def test_prune_state_removes_file(tmp_path):
    from agentic_pr_dash import maintenance
    from agentic_pr_dash.models import MaintenanceStatus

    wt = str(tmp_path)
    state = maintenance.build_maintenance_state(
        pr_number=42, branch="b", worktree_path=wt, blockers=[],
        state=MaintenanceStatus.QUEUED,
    )
    maintenance.save_state(state)
    path = maintenance.state_path(wt, 42)
    assert path.exists()

    assert maintenance.prune_state(wt, 42) is True
    assert not path.exists()
    # Idempotent: pruning an absent file is a no-op returning False.
    assert maintenance.prune_state(wt, 42) is False


# ---------------------------------------------------------------------------
# Item 5 — fix_lease_until parse hardening
# ---------------------------------------------------------------------------


def test_fix_lease_corrupt_is_active(capsys):
    from agentic_pr_dash import maintenance_check as mc

    # Empty -> no lease.
    assert mc._fix_lease_active("") is False
    # Valid future -> active.
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert mc._fix_lease_active(future) is True
    # Valid past -> expired.
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert mc._fix_lease_active(past) is False
    # CORRUPT but present -> fail-safe ACTIVE (defer), with a warning.
    assert mc._fix_lease_active("not-a-timestamp") is True
    assert "unparseable fix_lease_until" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Item 6 — executor validation at loop startup
# ---------------------------------------------------------------------------


def test_validate_executor_resolves_on_path():
    from agentic_pr_dash import loop

    # A real binary on PATH (python) resolves cleanly.
    assert loop._validate_executor("python3 -p {prompt}") is None


def test_validate_executor_rejects_missing_binary():
    from agentic_pr_dash import loop

    err = loop._validate_executor("definitely-not-a-real-binary-xyz {prompt}")
    assert err is not None and "not found on PATH" in err


def test_validate_executor_rejects_empty():
    from agentic_pr_dash import loop

    assert loop._validate_executor("") is not None


def test_loop_main_fails_fast_on_bad_executor(capsys):
    from agentic_pr_dash import loop

    rc = loop.main(["--once", "--executor", "definitely-not-a-real-binary-xyz {prompt}"])
    assert rc == 2
    assert "not found on PATH" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Item 7 — heartbeat write coalescing
# ---------------------------------------------------------------------------


def _write_marker(path, fields):
    path.write_text("".join(f"{k}={v}\n" for k, v in fields.items()), encoding="utf-8")


def test_heartbeat_not_rewritten_when_fresh(tmp_path, monkeypatch):
    from agentic_pr_dash import maintenance_check as mc

    marker = tmp_path / "pr-watch.armed"
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_marker(marker, {"pr": "5", "session_id": "S1", "pid": "1", "heartbeat": fresh})

    monkeypatch.setattr(mc, "_marker_path", lambda cwd: str(marker))
    before = marker.stat().st_mtime_ns
    import time as _t
    _t.sleep(0.01)

    # Clean tick with a still-fresh heartbeat and no lease: must NOT rewrite.
    mc._touch_owner_heartbeat(str(tmp_path), "S1", work_found=False)
    assert marker.stat().st_mtime_ns == before, "fresh heartbeat must not be rewritten"


def test_heartbeat_rewritten_when_stale(tmp_path, monkeypatch):
    from agentic_pr_dash import maintenance_check as mc

    marker = tmp_path / "pr-watch.armed"
    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_marker(marker, {"pr": "5", "session_id": "S1", "pid": "1", "heartbeat": stale})

    monkeypatch.setattr(mc, "_marker_path", lambda cwd: str(marker))
    mc._touch_owner_heartbeat(str(tmp_path), "S1", work_found=False)

    fields = mc._read_marker(str(tmp_path))
    assert fields["heartbeat"] != stale, "a stale heartbeat must be refreshed"


def test_heartbeat_work_found_always_writes_lease(tmp_path, monkeypatch):
    from agentic_pr_dash import maintenance_check as mc

    marker = tmp_path / "pr-watch.armed"
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_marker(marker, {"pr": "5", "session_id": "S1", "pid": "1", "heartbeat": fresh})

    monkeypatch.setattr(mc, "_marker_path", lambda cwd: str(marker))
    mc._touch_owner_heartbeat(str(tmp_path), "S1", work_found=True)

    fields = mc._read_marker(str(tmp_path))
    assert "fix_lease_until" in fields, "a work-found tick must always stamp the fix lease"


# ---------------------------------------------------------------------------
# Item 9 — claim-fingerprint gating
# ---------------------------------------------------------------------------


def _pr(comments, **kw):
    from agentic_pr_dash.models import PRData, ReviewComment

    review = [
        ReviewComment(id=c, author="r", body="fix", created_at="2026-06-11T12:00:00Z")
        for c in comments
    ]
    base = dict(
        number=123, title="t", branch="b",
        url="https://github.com/o/r/pull/123", worktree_path="/tmp/wt",
        failing_checks=[], review_comments=review, merge_state="CLEAN",
    )
    base.update(kw)
    return PRData(**base)


def test_active_claim_fingerprint_lookup_ignores_current_fingerprint(tmp_path, monkeypatch):
    from agentic_pr_dash import coordinator

    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(tmp_path / "claims.jsonl"))
    # Live owner pid (our own pid is alive).
    pr1 = _pr([11])
    claim = coordinator.claim_pr(pr1, session_id="S1", pid=os.getpid(), agent="a", lease_seconds=1800)
    assert claim is not None

    # Look up by a DIFFERENT-fingerprint PR (new feedback): the round-1 claim is
    # still found by task_id, and its fingerprint differs from the round-2 PR's.
    pr2 = _pr([11, 22])
    active_fp = coordinator.active_claim_fingerprint_for_pr(pr2)
    assert active_fp == coordinator.fingerprint_for_pr(pr1)
    assert active_fp != coordinator.fingerprint_for_pr(pr2)


def test_check_surfaces_new_fingerprint_feedback(tmp_path, monkeypatch):
    """A new blocker fingerprint must NOT be suppressed by a still-active round-1
    claim — the core BOU-1637 correctness fix."""
    from agentic_pr_dash import coordinator, maintenance_check as mc

    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(tmp_path / "claims.jsonl"))

    # Round 1: another live session claims the PR for blocker set {11}.
    pr1 = _pr([11])
    coordinator.claim_pr(pr1, session_id="OTHER", pid=os.getpid(), agent="a", lease_seconds=1800)

    # Round 2: new review thread {22} arrives -> different fingerprint.
    pr2 = _pr([11, 22])

    # Stub out everything _check_worktree touches except the coordinator gate.
    monkeypatch.setattr(mc, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(mc, "_resolve_pr_for_branch", lambda cwd: pr2)
    monkeypatch.setattr(mc, "_live_independent_owner_paths", lambda paths, sid: set())
    monkeypatch.setattr(mc, "_unresolved_review_threads", lambda pr, cwd: [])
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda *a, **k: None)
    import agentic_pr_dash.github_api as gh
    monkeypatch.setattr(gh, "get_failed_logs", lambda *a, **k: {})

    code, text = mc._check_worktree(str(tmp_path), "SELF", claim=True)
    assert code == 10, "new-fingerprint feedback must surface (exit 10), not defer"
    assert "PR_NUMBER=123" in text


class _FakeThreadTop:
    def __init__(self, cid):
        self.database_id = cid
        self.author = "reviewer"
        self.body = "please fix this"
        self.path = "src/foo.py"
        self.line = 10
        self.created_at = "2026-06-11T12:00:00Z"


class _FakeThread:
    def __init__(self, cid):
        self.top = _FakeThreadTop(cid)
        self.node_id = f"node-{cid}"
        self.is_resolved = False
        self.is_outdated = False


def test_check_surfaces_new_thread_when_other_blocker_present(tmp_path, monkeypatch):
    """The real masking bug: round-1 had a CI blocker (so the unresolved-thread
    hydration was skipped), then a NEW review thread arrived. Without merging the
    thread into the fingerprint, the round-1 claim masks it. This proves the merge
    makes the fingerprint change so `check` surfaces the work."""
    from agentic_pr_dash import coordinator, maintenance_check as mc

    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(tmp_path / "claims.jsonl"))

    # Round 1: PR has a CI failure, no review comments. Another session claims it.
    pr_round1 = _pr([], failing_checks=["unit"])
    coordinator.claim_pr(pr_round1, session_id="OTHER", pid=os.getpid(), agent="a", lease_seconds=1800)
    # Sanity: same state would defer.
    assert coordinator.active_claim_fingerprint_for_pr(pr_round1) == coordinator.fingerprint_for_pr(pr_round1)

    # Round 2: still failing CI, but now a NEW unresolved review thread exists that
    # does NOT appear in get_unaddressed_comments (so review_comments stays empty).
    pr_round2 = _pr([], failing_checks=["unit"])

    monkeypatch.setattr(mc, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(mc, "_resolve_pr_for_branch", lambda cwd: pr_round2)
    monkeypatch.setattr(mc, "_live_independent_owner_paths", lambda paths, sid: set())
    monkeypatch.setattr(mc, "_unresolved_review_threads", lambda pr, cwd: [_FakeThread(99)])
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda *a, **k: None)
    import agentic_pr_dash.github_api as gh
    monkeypatch.setattr(gh, "get_failed_logs", lambda *a, **k: {})

    code, text = mc._check_worktree(str(tmp_path), "SELF", claim=True)
    assert code == 10, "a new review thread must surface even with a pre-existing CI blocker"


def test_check_defers_same_fingerprint_claim(tmp_path, monkeypatch):
    """A same-fingerprint active claim (same blocker set already in flight) still
    defers — we don't double-dispatch identical work."""
    from agentic_pr_dash import coordinator, maintenance_check as mc

    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(tmp_path / "claims.jsonl"))

    pr1 = _pr([11])
    coordinator.claim_pr(pr1, session_id="OTHER", pid=os.getpid(), agent="a", lease_seconds=1800)

    monkeypatch.setattr(mc, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(mc, "_resolve_pr_for_branch", lambda cwd: _pr([11]))
    monkeypatch.setattr(mc, "_live_independent_owner_paths", lambda paths, sid: set())
    monkeypatch.setattr(mc, "_unresolved_review_threads", lambda pr, cwd: [])
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda *a, **k: None)

    code, text = mc._check_worktree(str(tmp_path), "SELF", claim=True)
    assert code == 0, "same-fingerprint in-flight work must defer"
    assert "deferring" in text
