"""BOU-2210: the maintenance daemon must not die on one PR's bad luck, and the
session registry must stay bounded / fail-soft.

Two independent defects are covered here:

1. ``loop._tick`` only guarded ``StaleClaimError``. Every OTHER coordinator
   exception (``PermissionError`` from an owner-session mismatch, ``KeyError``
   from a rotated ``claims.jsonl``, ``ValueError`` from a released claim,
   ``OSError`` from a full disk during ``fsync``) propagated out of the
   un-``except``-ed ``while True: _tick(...)`` and killed the daemon silently.
   Adjacent: a stale release AFTER a successful dispatch+complete ``continue``d
   past ``reset_executor_failure``, leaving a failure streak un-reset.

2. ``compact_registry`` could only ever drop a session whose latest event is
   terminal. Harness-only sessions (keyed by ``conversation_id``, an id space
   the launcher never emits a terminal event for) were therefore retained
   forever — one permanently-unreclaimable line per rotated conversation.
   Adjacent: ``_merge_event`` coerced ints/floats with no guard, so a single
   malformed field took down the whole dashboard permanently.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import types

import pytest

from agent_coordinator.service import StaleClaimError

from agentic_pr_dash import loop
from agentic_pr_dash import session_registry as sr


# ---------------------------------------------------------------------------
# Defect 1 — daemon resilience
# ---------------------------------------------------------------------------


def _args(**kw):
    base = dict(no_discover_worktrees=False, session_id="sess", cwd=["/fallback"])
    base.update(kw)
    return types.SimpleNamespace(**base)


# Every non-StaleClaimError the pinned coordinator can realistically raise
# through our facade: service.py raises PermissionError on an owner_session_id
# mismatch and KeyError on an unknown claim_id; ValueError on a released claim;
# store.py's os.fsync raises OSError on a full disk.
_COORDINATOR_FAULTS = [
    PermissionError("owner_session_id does not own claim"),
    KeyError("unknown claim_id"),
    ValueError("cannot heartbeat a released claim"),
    OSError(28, "No space left on device"),
]
_FAULT_IDS = ["permission", "keyerror", "valueerror", "oserror"]


def _tick_harness(monkeypatch, tmp_path, *, heartbeat, release, calls):
    """Wire up a two-worktree tick where only the first worktree has work."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == [loop.sys.executable, "-m", "agentic_pr_dash"] and "check" in cmd:
            cwd = cmd[cmd.index("--cwd") + 1]
            calls.append(("check", cwd))
            if cwd == str(first):
                return types.SimpleNamespace(
                    returncode=loop.CHECK_WORK_FOUND,
                    stdout=(
                        "fix prompt\nPR_NUMBER=7\n"
                        "COORDINATOR_CLAIM_ID=claim-1\n"
                        "COORDINATOR_LEASE_EPOCH=4\n"
                    ),
                    stderr="",
                )
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == [loop.sys.executable, "-m", "agentic_pr_dash"] and "complete" in cmd:
            calls.append(("complete", cmd))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    monkeypatch.setattr(loop, "_discover_cwds", lambda args: [str(first), str(second)])
    monkeypatch.setattr(loop, "_cleanup_stale_no_pr_worktree", lambda cwd, session_id="": False)
    monkeypatch.setattr(loop, "_baseline_sha", lambda cwd, pr: "base-sha")
    monkeypatch.setattr(loop, "_repo_slug", lambda cwd: "testrepo")
    monkeypatch.setattr(loop, "_clear_recovered_streak", lambda cwd: None)
    monkeypatch.setattr(loop, "record_loop_health", lambda *a, **k: None)
    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    monkeypatch.setattr(
        loop,
        "_run_executor",
        lambda executor, prompt, cwd: calls.append(("executor", cwd)) or 0,
    )
    monkeypatch.setattr(loop.coordinator, "heartbeat_claim", heartbeat)
    monkeypatch.setattr(loop.coordinator, "release_claim", release)
    return first, second


@pytest.mark.parametrize("fault", _COORDINATOR_FAULTS, ids=_FAULT_IDS)
def test_tick_survives_heartbeat_coordinator_fault(monkeypatch, tmp_path, capsys, fault):
    """A heartbeat blowing up on ONE worktree must not abort the whole tick."""
    calls: list[tuple] = []

    def heartbeat(handle, session_id):
        calls.append(("heartbeat", handle.claim_id))
        raise fault

    def release(handle, session_id, reason):
        calls.append(("release", handle.claim_id, reason))

    _first, second = _tick_harness(
        monkeypatch, tmp_path, heartbeat=heartbeat, release=release, calls=calls
    )

    loop._tick(_args(cwd=["/repo/root"], session_id="sess-1"), "codex {prompt}")

    assert ("heartbeat", "claim-1") in calls
    # The other worktree is still serviced — the daemon survives.
    assert ("check", str(second)) in calls
    # And the failure is LOGGED, not swallowed.
    assert type(fault).__name__ in capsys.readouterr().err


@pytest.mark.parametrize("fault", _COORDINATOR_FAULTS, ids=_FAULT_IDS)
def test_tick_survives_release_coordinator_fault(monkeypatch, tmp_path, capsys, fault):
    """Ditto for the post-complete release."""
    calls: list[tuple] = []

    def heartbeat(handle, session_id):
        calls.append(("heartbeat", handle.claim_id))

    def release(handle, session_id, reason):
        calls.append(("release", handle.claim_id, reason))
        raise fault

    _first, second = _tick_harness(
        monkeypatch, tmp_path, heartbeat=heartbeat, release=release, calls=calls
    )

    loop._tick(_args(cwd=["/repo/root"], session_id="sess-1"), "codex {prompt}")

    assert ("release", "claim-1", "completed") in calls
    assert ("check", str(second)) in calls
    assert type(fault).__name__ in capsys.readouterr().err


def test_daemon_loop_survives_a_raising_tick(monkeypatch, tmp_path):
    """``main``'s ``while True`` must keep ticking after a tick-level blowup.

    A fault in ``_discover_cwds`` (or anywhere else outside the per-cwd body)
    would otherwise still kill the daemon.
    """
    ticks: list[int] = []

    def fake_tick(args, executor):
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("coordinator store exploded")
        # Second tick proves we survived; bail out of the infinite loop with a
        # BaseException the guard must NOT swallow.
        raise KeyboardInterrupt

    monkeypatch.setattr(loop, "_tick", fake_tick)
    monkeypatch.setattr(loop, "_validate_executor", lambda cmd: None)
    monkeypatch.setattr(loop.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(loop, "_write_loop_pidfile", lambda pidfile: None)
    monkeypatch.setattr(loop, "_remove_loop_pidfile", lambda pidfile: None)

    with pytest.raises(KeyboardInterrupt):
        loop.main(["--executor", "codex {prompt}", "--interval", "5", "--cwd", str(tmp_path)])

    assert len(ticks) == 2


def test_stale_release_after_complete_still_resets_failure_streak(monkeypatch, tmp_path):
    """A dispatch+complete that succeeded must reset the streak even when the
    claim was fenced out from under us in the meantime.

    The old ``continue`` skipped ``reset_executor_failure``, so two genuine
    later failures could trip a 3-strike escalation.
    """
    calls: list[tuple] = []
    resets: list[tuple] = []

    def heartbeat(handle, session_id):
        calls.append(("heartbeat", handle.claim_id))

    def release(handle, session_id, reason):
        calls.append(("release", handle.claim_id, reason))
        raise StaleClaimError(expected_epoch=5, received_epoch=4)

    _first, second = _tick_harness(
        monkeypatch, tmp_path, heartbeat=heartbeat, release=release, calls=calls
    )
    monkeypatch.setattr(
        loop, "reset_executor_failure", lambda cwd, pr: resets.append((cwd, pr))
    )

    loop._tick(_args(cwd=["/repo/root"], session_id="sess-1"), "codex {prompt}")

    assert resets == [(str(_first), 7)]
    assert ("check", str(second)) in calls


# ---------------------------------------------------------------------------
# Defect 2 — registry growth + fail-soft merge
# ---------------------------------------------------------------------------


def _iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat().replace("+00:00", "Z")


def _status_row(session_id: str, timestamp: str, **extra) -> dict:
    row = {
        "session_id": session_id,
        "event": "harness_status",
        "timestamp": timestamp,
        "chain_id": "chain-1",
        "generation": 1,
        "supervisor_state": "running",
        "worktree_path": "/tmp/wt",
    }
    row.update(extra)
    return row


def _write(reg, rows) -> None:
    reg.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_compact_registry_reclaims_stale_harness_only_sessions(tmp_path, monkeypatch):
    """Harness-only sessions are keyed by ``conversation_id`` — an id space no
    terminal event is ever written under — so they were unreclaimable forever.
    """
    monkeypatch.setenv("AGENTIC_PR_DASH_REGISTRY_READ_LIMIT", "0")
    reg = tmp_path / "events.jsonl"
    old = _iso(-timedelta(days=30))
    _write(
        reg,
        [
            _status_row("rotated-conversation", old),
            _status_row("recent-conversation", _iso()),
        ],
    )

    removed = sr.compact_registry(path=reg, retention_seconds=7 * 24 * 3600)

    assert removed == 1
    kept = {e["session_id"] for e in sr.read_events(path=reg)}
    assert kept == {"recent-conversation"}


def test_compact_registry_keeps_stale_harness_only_session_with_live_pid(
    tmp_path, monkeypatch
):
    """A long-idle but genuinely LIVE harness session must survive compaction."""
    monkeypatch.setenv("AGENTIC_PR_DASH_REGISTRY_READ_LIMIT", "0")
    reg = tmp_path / "events.jsonl"
    _write(reg, [_status_row("live-conversation", _iso(-timedelta(days=30)), pid=os.getpid())])

    assert sr.compact_registry(path=reg, retention_seconds=7 * 24 * 3600) == 0
    assert sr.read_events(path=reg)[0]["session_id"] == "live-conversation"


def test_compact_registry_keeps_stale_session_with_lifecycle_history(
    tmp_path, monkeypatch
):
    """The max-age rule applies ONLY to harness-only sessions. A session with a
    non-terminal lifecycle event (``started``) keeps its existing semantics."""
    monkeypatch.setenv("AGENTIC_PR_DASH_REGISTRY_READ_LIMIT", "0")
    reg = tmp_path / "events.jsonl"
    old = _iso(-timedelta(days=30))
    _write(
        reg,
        [
            {"session_id": "launched", "event": "started", "timestamp": old},
            _status_row("launched", old),
        ],
    )

    assert sr.compact_registry(path=reg, retention_seconds=7 * 24 * 3600) == 0
    assert {e["session_id"] for e in sr.read_events(path=reg)} == {"launched"}


@pytest.mark.parametrize(
    "field, value",
    [
        ("context_percent", "not-a-number"),
        ("context_tokens", "lots"),
        ("generation", None),
        ("pid", "n/a"),
        ("pr_number", "#12"),
    ],
)
def test_summarize_sessions_survives_malformed_numeric_field(tmp_path, field, value):
    """``read_events`` deliberately tolerates malformed LINES; ``_merge_event``
    must tolerate malformed FIELDS the same way.

    One bad value used to raise straight out of ``summarize_sessions`` into
    ``app.py``'s card builder and take the whole dashboard down permanently,
    until someone hand-edited the JSONL.
    """
    reg = tmp_path / "events.jsonl"
    row = _status_row("conversation-1", _iso())
    row[field] = value
    _write(reg, [row, _status_row("conversation-2", _iso())])

    summary = sr.summarize_sessions(path=reg)

    # The malformed field is dropped; every session still summarizes.
    assert set(summary.sessions) == {"conversation-1", "conversation-2"}
