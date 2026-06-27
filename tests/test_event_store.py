"""Tests for agentic_pr_dash.observability.event_store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_pr_dash.observability import EventStore, ObservabilityEvent


def _make_event(**kwargs) -> ObservabilityEvent:
    defaults = dict(
        ts=datetime.now(timezone.utc),
        kind="poll_tick",
    )
    defaults.update(kwargs)
    return ObservabilityEvent(**defaults)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_append_then_query_round_trip(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.jsonl")
    ev = _make_event(
        kind="dispatch",
        repo="owner/repo",
        pr_number=42,
        session_id="sess-1",
        details={"executor": "codex", "exit_code": 0},
    )
    store.append(ev)

    results = store.query()
    assert len(results) == 1
    got = results[0]
    assert got.kind == "dispatch"
    assert got.repo == "owner/repo"
    assert got.pr_number == 42
    assert got.session_id == "sess-1"
    assert got.details == {"executor": "codex", "exit_code": 0}
    # Timestamps round-trip with microsecond precision (UTC).
    assert abs((got.ts - ev.ts).total_seconds()) < 0.001


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_query_filter_by_pr_number(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.jsonl")
    store.append(_make_event(kind="poll_tick", pr_number=1))
    store.append(_make_event(kind="poll_tick", pr_number=2))
    store.append(_make_event(kind="poll_tick", pr_number=1))

    results = store.query(pr_number=1)
    assert len(results) == 2
    assert all(r.pr_number == 1 for r in results)


def test_query_filter_by_kind(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.jsonl")
    store.append(_make_event(kind="dispatch"))
    store.append(_make_event(kind="poll_tick"))
    store.append(_make_event(kind="dispatch"))

    results = store.query(kind="dispatch")
    assert len(results) == 2
    assert all(r.kind == "dispatch" for r in results)


def test_query_filter_by_since(tmp_path: Path) -> None:
    base = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    store = EventStore(tmp_path / "events.jsonl")
    store.append(_make_event(kind="poll_tick", ts=base))
    store.append(_make_event(kind="poll_tick", ts=base + timedelta(hours=1)))
    store.append(_make_event(kind="poll_tick", ts=base + timedelta(hours=2)))

    # since is inclusive: ts >= since
    since = base + timedelta(hours=1)
    results = store.query(since=since)
    assert len(results) == 2
    assert all(r.ts >= since for r in results)


def test_query_limit(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.jsonl")
    for i in range(5):
        store.append(_make_event(kind="poll_tick", pr_number=i))

    results = store.query(limit=3)
    assert len(results) == 3


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_query_newest_first(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.jsonl")
    older = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    newer = datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc)

    store.append(_make_event(kind="poll_tick", ts=older))
    store.append(_make_event(kind="poll_tick", ts=newer))

    results = store.query()
    assert len(results) == 2
    # Newest first.
    assert results[0].ts == newer
    assert results[1].ts == older


# ---------------------------------------------------------------------------
# Size-cap (front-truncation)
# ---------------------------------------------------------------------------


def test_size_cap_keeps_newest_and_valid_jsonl(tmp_path: Path) -> None:
    # Use a very small cap so truncation fires quickly.
    max_bytes = 300
    store = EventStore(tmp_path / "events.jsonl", max_bytes=max_bytes)

    # Append enough events to exceed the cap.
    for i in range(20):
        store.append(
            _make_event(
                kind="poll_tick",
                pr_number=i,
                details={"index": i, "pad": "x" * 20},
            )
        )

    # File must exist and be at or near the cap (one-line tolerance).
    file_size = (tmp_path / "events.jsonl").stat().st_size
    # Allow one extra line beyond cap.
    single_line_estimate = 300  # generous upper bound per line
    assert file_size <= max_bytes + single_line_estimate, (
        f"File size {file_size} exceeds cap {max_bytes} by more than one line"
    )

    # All remaining lines must parse as valid JSONL.
    raw = (tmp_path / "events.jsonl").read_text()
    for line in raw.splitlines():
        line = line.strip()
        if line:
            ObservabilityEvent.model_validate_json(line)  # must not raise

    # The NEWEST event (pr_number=19) must be retained.
    results = store.query()
    pr_numbers = {r.pr_number for r in results}
    assert 19 in pr_numbers, f"Newest event missing; retained pr_numbers={pr_numbers}"


# ---------------------------------------------------------------------------
# Best-effort (never raises)
# ---------------------------------------------------------------------------


def test_append_to_unwritable_path_does_not_raise(tmp_path: Path) -> None:
    # Make the "parent" a file so mkdir fails.
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a dir")
    store = EventStore(blocker / "events.jsonl")
    # Must not raise.
    store.append(_make_event(kind="poll_tick"))


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------


def test_query_missing_file_returns_empty(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "nonexistent" / "events.jsonl")
    assert store.query() == []
