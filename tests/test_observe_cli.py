"""Tests for `agentic-pr-dash observe` subcommand (cli.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_pr_dash import cli
from agentic_pr_dash.observability import EventStore, ObservabilityEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ev(**kwargs) -> ObservabilityEvent:
    defaults: dict = dict(ts=_BASE_TS, kind="poll_tick")
    defaults.update(kwargs)
    return ObservabilityEvent(**defaults)


def _comment_scan_event(pr: int, decisions: list[dict], picked: int, ts=None) -> ObservabilityEvent:
    return _ev(
        ts=ts or _BASE_TS,
        kind="comment_scan",
        pr_number=pr,
        details={"decisions": decisions, "picked": picked},
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded_store(tmp_path: Path, monkeypatch) -> EventStore:
    """Build and seed an EventStore; patch cli._observe_get_event_store to return it."""
    store = EventStore(tmp_path / "events.jsonl")

    # comment_scan for PR 7 with two decisions
    decisions = [
        {
            "thread_id": "thread-aaa111",
            "author": "alice",
            "created_at": "2024-06-01T10:00:00Z",
            "age_seconds": 7200,
            "decision": "PICKED",
            "marker_state": "none",
            "claim_age_seconds": 0,
        },
        {
            "thread_id": "thread-bbb222",
            "author": "bob",
            "created_at": "2024-06-01T11:00:00Z",
            "age_seconds": 3600,
            "decision": "SKIP_OUTDATED",
            "marker_state": "done",
            "claim_age_seconds": 120,
        },
    ]
    store.append(_comment_scan_event(pr=7, decisions=decisions, picked=1, ts=_BASE_TS))

    # A couple of other-kind events
    store.append(_ev(kind="poll_tick", pr_number=7, ts=_BASE_TS - timedelta(minutes=5)))
    store.append(
        _ev(
            kind="dispatch",
            pr_number=7,
            ts=_BASE_TS - timedelta(minutes=10),
            details={"executor": "codex"},
        )
    )
    store.append(_ev(kind="poll_tick", pr_number=99, ts=_BASE_TS - timedelta(minutes=2)))

    monkeypatch.setattr(cli, "_observe_get_event_store", lambda cwd: store)
    return store


# ---------------------------------------------------------------------------
# Tests: --pr view (comment_scan table)
# ---------------------------------------------------------------------------


def test_observe_pr_shows_both_decisions(seeded_store, capsys):
    rc = cli.main(["observe", "--pr", "7"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alice" in out
    assert "bob" in out
    assert "PICKED" in out
    assert "SKIP_OUTDATED" in out


def test_observe_pr_shows_picked_summary(seeded_store, capsys):
    rc = cli.main(["observe", "--pr", "7"])
    assert rc == 0
    out = capsys.readouterr().out
    # The summary line must mention "picked 1 of 2"
    assert "picked 1 of 2" in out


def test_observe_pr_shows_thread_short_id(seeded_store, capsys):
    rc = cli.main(["observe", "--pr", "7"])
    assert rc == 0
    out = capsys.readouterr().out
    # thread-aaa111 → last ≤12 chars shown
    assert "aaa111" in out or "thread-aaa111" in out


def test_observe_pr_no_events_prints_message(seeded_store, capsys):
    rc = cli.main(["observe", "--pr", "999"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no comment_scan" in out
    assert "999" in out


# ---------------------------------------------------------------------------
# Tests: generic listing
# ---------------------------------------------------------------------------


def test_observe_kind_filter_lists_matching_events(seeded_store, capsys):
    rc = cli.main(["observe", "--kind", "poll_tick"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == 2
    assert all("poll_tick" in l for l in lines)


def test_observe_generic_newest_first(seeded_store, capsys):
    rc = cli.main(["observe", "--kind", "poll_tick"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.strip()]
    # Newest-first: PR#99 (BASE_TS - 2min) before PR#7 (BASE_TS - 5min)
    assert "PR#99" in lines[0]
    assert "PR#7" in lines[1]


def test_observe_limit_respected(seeded_store, capsys):
    # Total events = 4; limit=2 should only print 2.
    rc = cli.main(["observe", "--limit", "2"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == 2


def test_observe_pr_and_kind_together_uses_generic_listing(seeded_store, capsys):
    """--pr + --kind skips the comment_scan table view and does a generic listing."""
    rc = cli.main(["observe", "--pr", "7", "--kind", "comment_scan"])
    assert rc == 0
    out = capsys.readouterr().out
    # Should print the single comment_scan line, not the table
    assert "comment_scan" in out
    # Should NOT show the table header
    assert "DECISION" not in out


def test_observe_since_filter(seeded_store, capsys):
    # since = BASE_TS (inclusive) → should include comment_scan (at BASE_TS) and poll_tick PR#99
    # but exclude poll_tick PR#7 (BASE_TS - 5min) and dispatch (BASE_TS - 10min)
    since_str = _BASE_TS.isoformat()
    rc = cli.main(["observe", "--since", since_str])
    assert rc == 0
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.strip()]
    # comment_scan at BASE_TS, poll_tick PR#99 at BASE_TS - 2min — wait, that's BEFORE BASE_TS
    # So only comment_scan at exactly BASE_TS should appear
    assert len(lines) == 1
    assert "comment_scan" in lines[0]


# ---------------------------------------------------------------------------
# Tests: help / parser
# ---------------------------------------------------------------------------


def test_observe_help_does_not_crash():
    """The observe subcommand argparse parser must parse --help without errors."""
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["observe", "--help"])
    # argparse exits 0 for --help
    assert exc_info.value.code == 0
