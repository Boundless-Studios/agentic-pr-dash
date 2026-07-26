"""Persistent observability event store for agentic-pr-dash.

Stores a bounded JSONL file of structured events emitted by the maintenance
loop, stop-gate, and dispatch subsystems. Reading is always cheapest-first:
query() returns newest-first (reversed order of file).

Known event kinds (free-form strings, not an enum):
    poll_tick        — loop iteration started for a PR
    comment_scan     — review-comment scan completed
    dispatch         — agent executor dispatched for a PR
    dispatch_result  — outcome of a dispatch (success/failure/timeout)
    state_transition — PR maintenance-state changed (e.g. queued → running)
    ownership        — ownership marker claimed or released

Every event also carries an ``actor`` (BOU-2490). ``kind`` says *what happened*;
``actor`` says *who did it and whether they could write code*. Both matter —
``kind="dispatch"`` is emitted by the dashboard (queued a work order, wrote
nothing) and by the loop (ran ``codex --full-auto`` and pushed), and telling
those apart is the whole point.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from ..config import load as _load_config


class ObservabilityEvent(BaseModel):
    ts: datetime
    repo: str | None = None
    pr_number: int | None = None
    kind: str
    #: Which maintenance surface emitted this (a :class:`~..models.MaintenanceActor`
    #: value). ``kind`` alone is ambiguous: the dashboard's "queued a work order"
    #: and the loop's "ran the executor and pushed" are both ``kind="dispatch"``.
    #: Optional so rows written before BOU-2490 still deserialize; readers render
    #: a missing actor as ``unknown`` rather than guessing.
    actor: str | None = None
    session_id: str | None = None
    details: dict = Field(default_factory=dict)


class EventStore:
    """Append-only JSONL store for :class:`ObservabilityEvent` records.

    All writes are best-effort: any OSError or unexpected exception is silently
    swallowed so that observability never breaks callers.
    """

    def __init__(self, path: Path, max_bytes: int = 5_000_000) -> None:
        self.path = path
        self.max_bytes = max_bytes

    def append(self, event: ObservabilityEvent) -> None:
        """Append one event to the store (best-effort, never raises)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = event.model_dump_json() + "\n"
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
            self._maybe_truncate()
        except Exception:
            pass

    def _maybe_truncate(self) -> None:
        """If the file exceeds max_bytes, drop oldest lines from the front."""
        try:
            if self.path.stat().st_size <= self.max_bytes:
                return
            data = self.path.read_bytes()
            if len(data) <= self.max_bytes:
                return
            # Drop whole lines from the front until under cap.
            while len(data) > self.max_bytes:
                newline_pos = data.find(b"\n")
                if newline_pos == -1:
                    # Single line larger than cap — keep it anyway.
                    break
                data = data[newline_pos + 1 :]
            self.path.write_bytes(data)
        except Exception:
            pass

    def query(
        self,
        pr_number: int | None = None,
        kind: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[ObservabilityEvent]:
        """Return matching events, newest-first.

        Filters are ANDed. ``since`` means ts >= since (inclusive).
        Malformed lines are silently skipped. Missing file returns [].
        """
        if not self.path.exists():
            return []
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return []

        events: list[ObservabilityEvent] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = ObservabilityEvent.model_validate_json(line)
            except Exception:
                continue

            if pr_number is not None and ev.pr_number != pr_number:
                continue
            if kind is not None and ev.kind != kind:
                continue
            if since is not None:
                ev_ts = ev.ts
                # Make both tz-aware for comparison.
                if ev_ts.tzinfo is None:
                    ev_ts = ev_ts.replace(tzinfo=timezone.utc)
                cmp_since = since
                if cmp_since.tzinfo is None:
                    cmp_since = cmp_since.replace(tzinfo=timezone.utc)
                if ev_ts < cmp_since:
                    continue
            events.append(ev)

        # Newest-first (file is oldest-first by append order).
        events.reverse()

        if limit is not None:
            events = events[:limit]

        return events


def get_event_store(cwd: str | None = None) -> EventStore:
    """Return an EventStore rooted under the config state_dir for *cwd*."""
    cfg = _load_config(cwd)
    path = cfg.state_dir / "observability" / "events.jsonl"
    return EventStore(path)


def emit(
    cwd: str | None,
    kind: str,
    *,
    pr_number: int | None = None,
    session_id: str | None = None,
    details: dict | None = None,
) -> None:
    """Best-effort event emission from a module-level call site. Never raises.

    ``Orchestrator._emit`` is the same contract bound to an instance; this is
    for the maintenance-path modules that have a ``cwd`` but no orchestrator.
    Emission must never alter control flow (BOU-1801), so every failure —
    unwritable state dir, malformed details, missing config — is swallowed.
    """
    try:
        get_event_store(cwd).append(
            ObservabilityEvent(
                ts=datetime.now(timezone.utc),
                repo=cwd,
                pr_number=pr_number,
                kind=kind,
                session_id=session_id,
                details=details or {},
            )
        )
    except Exception:  # noqa: BLE001
        pass
