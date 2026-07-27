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

Storage (BOU-2567 PR #122 review, P1 #1): a deferral is a fact about
``(repo, pr_number, thread_id)`` — GitHub identities, not about whichever
worktree happened to run ``complete --defer``. It must therefore live
somewhere every consumer can read regardless of which worktree performed the
deferral, including the orchestrator's dashboard (which calls
``scan_review_threads`` against the repository root, not a feature worktree)
and detached reconciliation (whose worktree may already be gone). Storing it
under ``state_dir_for(cwd)`` — this repo's convention for WORKTREE-scoped facts
like ``pr-watch.armed`` — made it invisible to those consumers and, worse,
deletable by removing the worktree that recorded it. This mirrors the two
existing repo-shared, cross-worktree durable stores in this codebase:
``coordinator.store_path()`` (``~/.agent-coordinator/claims.jsonl``, keyed by
``(repo_slug, pr_number)`` — see ``ownership.py``'s own "the PR itself is the
claimed task; worktrees are incidental" framing) and
``session_ledger._DEFAULT_DIR`` (``~/.gaia/pr-watch/ledger``). One shared file,
keyed internally by repo slug + PR number, resolved from ``cwd`` only to
identify WHICH repo/PR a call is about — never to locate the store itself.
"""
from __future__ import annotations

import fcntl
import functools
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass

_STATE_FILENAME = "deferred-reviews.json"

# Overridable for tests/alternate installs, matching the naming convention of
# the other repo-shared stores (AGENTIC_PR_DASH_COORDINATOR_STORE / the legacy
# GAIA_ prefix honored elsewhere in this package).
_ENV_STORE_PATH = "AGENTIC_PR_DASH_DEFERRED_STORE"
_LEGACY_ENV_STORE_PATH = "GAIA_DEFERRED_STORE"

# BOU-2567 PR #122 review, round 3, P1: relocating the store to a single
# machine-wide file (round 2) made it genuinely shared -- several sessions'
# `complete --defer`/`--sweep-p2` calls can now race on the SAME file, and a
# shared read-modify-write store needs serialization or a later writer's
# atomic replace silently discards an earlier writer's still-unsaved update.
_ENV_LOCK_TIMEOUT_SECONDS = "AGENTIC_PR_DASH_DEFERRED_LOCK_TIMEOUT_SECONDS"
_DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.01

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


def _default_store_path() -> str:
    """The shared, cross-worktree, cross-session default location.

    Deliberately a plain function, NOT a module-level constant: a
    module-level ``os.path.expanduser("~/...")`` is evaluated once at import
    time and would freeze to whatever ``HOME`` happened to be at that moment
    (this bit ``session_ledger._DEFAULT_DIR`` — see conftest.py's isolation
    note, which has to explicitly redirect it via an env var per test because
    of exactly this). Calling ``expanduser`` lazily here means every test
    already gets a hermetic store for free from the existing per-test ``HOME``
    fixture (``conftest._isolate_config``), no additional test-only env
    plumbing required.
    """
    return os.path.expanduser(os.path.join("~", ".gaia", "pr-watch", _STATE_FILENAME))


def _store_path() -> str:
    return (
        os.environ.get(_ENV_STORE_PATH)
        or os.environ.get(_LEGACY_ENV_STORE_PATH)
        or _default_store_path()
    )


def _lock_path() -> str:
    return _store_path() + ".lock"


def _lock_timeout_seconds() -> float:
    raw = os.environ.get(_ENV_LOCK_TIMEOUT_SECONDS, "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_LOCK_TIMEOUT_SECONDS
    except ValueError:
        return _DEFAULT_LOCK_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_LOCK_TIMEOUT_SECONDS


class DeferredStoreLockTimeout(DeferralError):
    """The shared store's lock could not be acquired within its deadline.

    Raised, never swallowed: a write that could not be serialized against
    concurrent writers is not a write that happened. This repo's own
    postmortem (BOU-2567) is four bugs that each came from an "unknown" or
    "could not determine" collapsing into a definite, wrong answer -- a
    timed-out lock acquisition must not become "proceeded anyway".
    """


@contextmanager
def _locked():
    """Hold an exclusive, non-blocking-polled, TIME-BOUNDED lock around a
    read-modify-write transaction on the shared store.

    Mirrors ``ownership.BoundedLockClaimStore._bounded_lock``: an unbounded
    ``fcntl.flock(LOCK_EX)`` (``session_ledger``'s writer lock, and the
    coordinator's own ``JsonlClaimStore``) blocks forever behind a stale or
    abandoned holder — acceptable for a long-lived daemon, but NOT for an
    interactive CLI command (``complete --defer``/``--sweep-p2``), which must
    fail loudly within a bounded time rather than hang. So this polls a
    non-blocking ``flock`` attempt against a wall-clock deadline and raises
    :class:`DeferredStoreLockTimeout` instead of waiting past it.

    Deliberately NOT used by any read path (:func:`_load` and its callers —
    ``deferred_threads_for_pr``, ``is_thread_deferred``, ``thread_state``,
    ``followup_ticket_for_pr``): :func:`_save` writes via ``mkstemp`` +
    ``os.replace`` (atomic rename), so a concurrent reader always sees either
    the pre- or post-write state, never a torn one — the same precedent as
    ``session_ledger.read``, which is also lock-free while its writers
    (``append``/``prune``) take the lock. Only the load-mutate-save
    transaction needs serializing; adding lock contention to reads would cost
    every ``check``/``stop-gate`` tick for no correctness benefit.
    """
    path = _lock_path()
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        timeout = _lock_timeout_seconds()
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DeferredStoreLockTimeout(
                        f"could not acquire the deferred-review store lock "
                        f"within {timeout}s ({path}); another process is "
                        "holding it. The deferral was NOT recorded — retry "
                        "once the contending process releases it."
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


@functools.lru_cache(maxsize=256)
def _cached_repo_slug(cwd: str) -> str:
    """Memoized ``(owner/name)`` for ``cwd`` — a git-remote shell-out per call
    would otherwise fire once per THREAD (every ``is_thread_deferred`` call in
    a list comprehension over a PR's threads), not once per PR. Safe to cache
    for a CLI-process lifetime: the repo a given worktree path belongs to does
    not change mid-process. Bounded (not unbounded) so a long-lived process
    (the dashboard, the loop) touching many worktrees over time cannot grow
    this without limit; eviction only costs a re-shell-out, never a wrong
    answer.
    """
    from ._common import _repo_slug  # noqa: PLC0415

    return _repo_slug(cwd)


def _load() -> dict:
    try:
        with open(_store_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    """Best-effort atomic write — mirrors the other ``pr-watch.*.json`` stores.

    A failed write must not raise into a caller mid-CLI-command; the caller's
    own return code communicates success/failure of the overall operation.
    (Propagating that failure signal out of ``_save`` itself — so a full disk
    or read-only filesystem can't make ``defer_thread`` silently report success
    with nothing persisted — is BOU-2567 PR #122 review P2 #1, tracked in the
    follow-up ticket rather than fixed here.)
    """
    path = _store_path()
    try:
        parent = os.path.dirname(path)
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".deferred-reviews.")
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


def _pr_key(cwd: str, pr_number: int) -> str:
    """The record key: ``(repo_slug, pr_number)`` — a PR identity, never a
    worktree identity. Two different worktrees of the SAME repo (or the
    repository root, or a torn-down worktree's last-known path) all resolve to
    the same key, which is the whole point (BOU-2567 PR #122 review, P1 #1).
    An undetectable repo (e.g. a bare non-git ``cwd``) falls back to an empty
    slug rather than raising — callers that can't resolve a repo still get a
    consistent (if unscoped) bucket instead of an exception.
    """
    repo_slug = _cached_repo_slug(cwd)
    return f"{repo_slug}#{pr_number}"


def _pr_record(state: dict, key: str, pr_number: int) -> dict:
    prs = state.setdefault("prs", {})
    return prs.setdefault(key, {"pr": pr_number, "deferred": [], "followup_ticket": None})


def deferred_threads_for_pr(cwd: str, pr_number: int) -> dict[str, dict]:
    """``thread_id -> deferral record`` for every thread deferred on this PR."""
    state = _load()
    key = _pr_key(cwd, pr_number)
    record = state.get("prs", {}).get(key, {})
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
    state = _load()
    key = _pr_key(cwd, pr_number)
    record = state.get("prs", {}).get(key, {})
    return record.get("followup_ticket") or None


def set_followup_ticket(cwd: str, pr_number: int, ticket: str) -> None:
    if not is_valid_ticket(ticket):
        raise DeferralError(f"invalid follow-up ticket {ticket!r}")
    # Locked (BOU-2567 PR #122 review round 3): the load-mutate-save
    # transaction must be serialized against concurrent writers the same as
    # defer_thread's — see _locked()'s docstring.
    with _locked():
        state = _load()
        key = _pr_key(cwd, pr_number)
        _pr_record(state, key, pr_number)["followup_ticket"] = ticket.strip()
        _save(state)


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

    ``cwd`` identifies WHICH repo/PR this call is about (via the repo slug);
    it does not affect where the record is stored — see the module docstring.
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
    # Locked (BOU-2567 PR #122 review round 3, P1): the whole load-mutate-save
    # transaction is the critical section, not just the save — two concurrent
    # callers must never both load the same pre-mutation snapshot. Raises
    # DeferredStoreLockTimeout (a DeferralError) on a stale/abandoned lock
    # rather than hang or silently proceed unlocked; propagates straight to
    # the CLI, which must treat it exactly like any other DeferralError
    # (nonzero exit, no false "deferred thread ..." success message).
    with _locked():
        state = _load()
        key = _pr_key(cwd, pr_number)
        pr_rec = _pr_record(state, key, pr_number)
        pr_rec["deferred"] = [
            d for d in pr_rec["deferred"] if d.get("thread_id") != thread_id
        ] + [record.to_dict()]
        _save(state)
    return record
