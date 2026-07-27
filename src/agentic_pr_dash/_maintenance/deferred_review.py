"""Deferred review-thread state — BOU-2567.

A deliberately-deferred review thread (a genuine, verified finding that is out
of scope for this PR, tracked by a follow-up ticket) had no representation
anywhere in this codebase. Every automated surface read "unresolved" as
"unaddressed": the stop gate blocked, ``check`` reported pending work, and the
pr-maintenance loop dispatched an executor against it — once, actively undoing
a deferral decision made an hour earlier.

This module is the persisted, first-class fact. It is deliberately NOT layered
on top of GitHub's own thread resolution (``ReviewThread.is_resolved``):
resolving a thread to represent "deferred" is exactly the wrong move (it erases
the deferral and looks identical to "actually fixed"), and leaving it
unresolved with no other signal is the original bug. A review thread is
therefore always exactly one of three states — never a boolean overloading
either of the other two:

    unresolved  — no GitHub resolution, no deferral record.
    deferred    — no GitHub resolution, HAS a deferral record (this module).
    resolved    — GitHub resolution (``is_resolved=True``); deferral is moot.

See :func:`thread_state` for the composed three-way read. Every consumer reads
the SAME fact and applies its own policy (stop gate: non-blocking + reported
separately; ``check``/``reconcile-prs``: excluded from blockers; the
pr-maintenance loop: never dispatched against; ``complete``: never
auto-resolved) — this module does not encode any consumer's policy, only the
fact.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass

from agentic_pr_dash.config import load as load_config

_STATE_FILENAME = "pr-watch.deferred.json"

# A well-formed tracker reference (e.g. ``BOU-2559``). This repo has no live
# Linear client, so "requires a ticket ID that resolves" is enforced as "is a
# well-formed tracker reference" — the anti-abuse property that predicate
# exists for (reject empty/placeholder strings) holds either way.
_TICKET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")

_VALID_SEVERITIES = ("P1", "P2")


class DeferralError(ValueError):
    """Raised when a deferral request fails an anti-abuse check."""


@dataclass(frozen=True)
class DeferredThread:
    thread_id: str
    comment_id: int | None
    severity: str
    reason: str
    ticket: str
    deferred_at: str
    deferred_by: str

    def to_dict(self) -> dict:
        return {
            "thread_id": self.thread_id,
            "comment_id": self.comment_id,
            "severity": self.severity,
            "reason": self.reason,
            "ticket": self.ticket,
            "deferred_at": self.deferred_at,
            "deferred_by": self.deferred_by,
        }


def is_valid_ticket(ticket: str | None) -> bool:
    return bool(ticket) and bool(_TICKET_RE.match(ticket.strip()))


def _state_path(cwd: str) -> str:
    return str(load_config(cwd).state_dir_for(cwd) / _STATE_FILENAME)


def _load(cwd: str) -> dict:
    try:
        with open(_state_path(cwd), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(cwd: str, state: dict) -> None:
    """Best-effort atomic write — mirrors the other ``pr-watch.*.json`` stores.

    A failed write must not raise into a caller mid-CLI-command; the caller's
    own return code communicates success/failure of the overall operation.
    """
    path = _state_path(cwd)
    try:
        parent = os.path.dirname(path)
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".pr-watch.deferred.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
    except OSError:
        pass


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _pr_key(pr_number: int) -> str:
    return str(pr_number)


def _pr_record(state: dict, pr_number: int) -> dict:
    prs = state.setdefault("prs", {})
    return prs.setdefault(
        _pr_key(pr_number), {"pr": pr_number, "deferred": [], "followup_ticket": None}
    )


def deferred_threads_for_pr(cwd: str, pr_number: int) -> dict[str, dict]:
    """``thread_id -> deferral record`` for every thread deferred on this PR."""
    state = _load(cwd)
    record = state.get("prs", {}).get(_pr_key(pr_number), {})
    return {
        d["thread_id"]: d for d in record.get("deferred", []) if d.get("thread_id")
    }


def is_thread_deferred(cwd: str, pr_number: int, thread_id: str | None) -> bool:
    if not thread_id:
        return False
    return thread_id in deferred_threads_for_pr(cwd, pr_number)


def deferred_count_for_pr(cwd: str, pr_number: int) -> int:
    return len(deferred_threads_for_pr(cwd, pr_number))


def thread_state(cwd: str, pr_number: int, thread) -> str:
    """The composed three-way state for one ``ReviewThread``.

    Resolution always wins (a human/tool resolving a thread on GitHub is a
    stronger, later signal than a standing deferral record); otherwise the
    deferred fact applies; otherwise the thread is plain unresolved.
    """
    if getattr(thread, "is_resolved", False):
        return "resolved"
    if is_thread_deferred(cwd, pr_number, getattr(thread, "node_id", None)):
        return "deferred"
    return "unresolved"


def followup_ticket_for_pr(cwd: str, pr_number: int) -> str | None:
    state = _load(cwd)
    record = state.get("prs", {}).get(_pr_key(pr_number), {})
    return record.get("followup_ticket") or None


def set_followup_ticket(cwd: str, pr_number: int, ticket: str) -> None:
    if not is_valid_ticket(ticket):
        raise DeferralError(f"invalid follow-up ticket {ticket!r}")
    state = _load(cwd)
    _pr_record(state, pr_number)["followup_ticket"] = ticket.strip()
    _save(cwd, state)


def defer_thread(
    cwd: str,
    pr_number: int,
    *,
    thread_id: str,
    comment_id: int | None,
    severity: str,
    ticket: str,
    reason: str = "",
    deferred_by: str = "",
) -> DeferredThread:
    """Persist a deferral. Raises :class:`DeferralError` on any anti-abuse violation.

    Anti-abuse (BOU-2567 operator-decided design):
      * a ticket ID is REQUIRED and must be a well-formed tracker reference —
        deferral must never become a mute button with no tracked follow-up.
      * a P1 deferral additionally REQUIRES a non-empty free-text ``reason`` —
        P1 blocks by default, so deferring one is a deliberate, explained
        exception, never a silent one.
      * re-deferring the same ``thread_id`` is idempotent (the record is
        replaced) rather than an error — a retried CLI call must succeed.
    """
    severity = (severity or "").strip().upper()
    if severity not in _VALID_SEVERITIES:
        raise DeferralError(
            f"severity must be one of {_VALID_SEVERITIES}, got {severity!r}"
        )
    if not thread_id:
        raise DeferralError("thread_id is required to defer a review thread")
    if not is_valid_ticket(ticket):
        raise DeferralError(
            "a resolvable ticket ID is required to defer a thread "
            f"(got {ticket!r}) — deferral without a tracked follow-up is not "
            "allowed"
        )
    if severity == "P1" and not (reason or "").strip():
        raise DeferralError(
            "a P1 deferral requires a free-text --reason explaining why it is "
            "out of scope for this PR"
        )
    record = DeferredThread(
        thread_id=thread_id,
        comment_id=comment_id,
        severity=severity,
        reason=(reason or "").strip(),
        ticket=ticket.strip(),
        deferred_at=_now_iso(),
        deferred_by=deferred_by or "",
    )
    state = _load(cwd)
    pr_rec = _pr_record(state, pr_number)
    pr_rec["deferred"] = [
        d for d in pr_rec["deferred"] if d.get("thread_id") != thread_id
    ] + [record.to_dict()]
    _save(cwd, state)
    return record
