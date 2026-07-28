"""Mutual exclusion for WRITES to a shared worktree (BOU-2590).

The ownership marker answers "who is responsible for this PR" and stops two
actors dispatching maintenance for the same work. It does NOT stop them
touching the same files: during gaia PR #2875 the detached loop's executor
committed the live session's in-progress changes and started a merge underneath
it, leaving ``MERGE_HEAD`` and a conflict for the session to untangle, with
authorship ambiguous.

Ownership and mutation are different questions. A session can legitimately own a
PR while the loop is the one allowed to write, and vice versa — but only ever
ONE of them may be writing at a time.

Design notes:

* **Atomic acquire.** ``O_CREAT | O_EXCL`` is the whole mutex; there is no
  read-then-write window for two actors to both pass.
* **Durable liveness, not a TTL.** A lock whose holder pid is gone is stale and
  is broken automatically. A timeout would either strand the worktree (too long)
  or hand it to a second writer mid-commit (too short), and "is the process
  alive" is the question actually being asked.
* **Fenced release.** Releasing takes the token from acquisition, so a stale
  holder that wakes up late cannot release the lock a new owner now holds — the
  same fencing rule the ownership claims use.
* **Reads are free.** Nothing here gates monitoring; only writers acquire.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from ._common import _pid_alive

_LOCK_FILENAME = "pr-watch.mutation-lock.json"


def _lock_path(worktree: str) -> str:
    return os.path.join(worktree, ".gaia", _LOCK_FILENAME)


@dataclass(frozen=True)
class MutationOwner:
    """Who currently holds the right to mutate a worktree."""

    actor: str
    pid: str
    session_id: str = ""
    acquired_at: float = 0.0
    #: Opaque fencing token; only the exact holder can release.
    token: str = ""

    def describe(self) -> str:
        who = self.actor or "unknown actor"
        if self.session_id:
            who = f"{who} (session {self.session_id[:8]})"
        return f"{who}, pid {self.pid}"


class WorktreeBusy(RuntimeError):
    """Raised when a LIVE actor already holds this worktree's mutation lock."""

    def __init__(self, owner: MutationOwner, worktree: str) -> None:
        self.owner = owner
        self.worktree = worktree
        super().__init__(
            f"{worktree} is being mutated by {owner.describe()} — standing down "
            "without touching git state (BOU-2590)"
        )


def read_owner(worktree: str) -> Optional[MutationOwner]:
    """Current holder, or None when the lock is absent/unreadable/corrupt.

    A corrupt lock file answers None deliberately: it names no probe-able
    holder, so refusing to write forever on account of it would strand the
    worktree with no way back short of manual deletion.
    """
    try:
        with open(_lock_path(worktree), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return MutationOwner(
        actor=str(data.get("actor", "")),
        pid=str(data.get("pid", "")),
        session_id=str(data.get("session_id", "")),
        acquired_at=float(data.get("acquired_at", 0.0) or 0.0),
        token=str(data.get("token", "")),
    )


def _write_lock(path: str, owner: MutationOwner) -> None:
    """Replace the lock file atomically (used only when breaking a stale one)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=f".{_LOCK_FILENAME}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "actor": owner.actor,
                    "pid": owner.pid,
                    "session_id": owner.session_id,
                    "acquired_at": owner.acquired_at,
                    "token": owner.token,
                },
                fh,
            )
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def acquire(
    worktree: str,
    *,
    actor: str,
    session_id: str = "",
    pid: Optional[int] = None,
) -> MutationOwner:
    """Take the worktree's mutation lock, or raise :class:`WorktreeBusy`.

    Re-entrant for the SAME pid: a holder that acquires again gets its existing
    lock back rather than deadlocking against itself.
    """
    path = _lock_path(worktree)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    me = MutationOwner(
        actor=actor,
        pid=str(pid if pid is not None else os.getpid()),
        session_id=session_id,
        acquired_at=time.time(),
        token=f"{os.getpid()}-{time.time_ns()}",
    )

    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            held = read_owner(worktree)
            if held is None:
                # Unreadable/corrupt: treat as stale and reclaim, rather than
                # stranding the worktree on a file naming no live holder.
                _write_lock(path, me)
                return me
            if held.pid == me.pid:
                return held  # re-entrant
            if _pid_alive(held.pid):
                raise WorktreeBusy(held, worktree)
            # Holder is gone — break the stale lock and retry the atomic create.
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "actor": me.actor,
                        "pid": me.pid,
                        "session_id": me.session_id,
                        "acquired_at": me.acquired_at,
                        "token": me.token,
                    },
                    fh,
                )
            return me

    held = read_owner(worktree)
    if held is not None and _pid_alive(held.pid):
        raise WorktreeBusy(held, worktree)
    _write_lock(path, me)
    return me


def release(worktree: str, owner: MutationOwner) -> bool:
    """Release the lock IFF ``owner`` still holds it. True when it was released.

    Fenced on the token: a stale holder waking up after its lock was broken must
    not release the lock its successor now holds.
    """
    held = read_owner(worktree)
    if held is None:
        return False
    if held.token != owner.token:
        return False
    try:
        os.unlink(_lock_path(worktree))
    except OSError:
        return False
    return True


@contextmanager
def mutation_lock(
    worktree: str,
    *,
    actor: str,
    session_id: str = "",
    pid: Optional[int] = None,
) -> Iterator[MutationOwner]:
    """Hold the worktree's mutation lock for the duration of a write."""
    owner = acquire(worktree, actor=actor, session_id=session_id, pid=pid)
    try:
        yield owner
    finally:
        release(worktree, owner)
