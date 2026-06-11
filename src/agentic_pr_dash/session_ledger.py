"""Durable, worktree-independent record of PRs a session has armed.

The pr-watch ownership model is worktree-derived: when a worktree is torn down
its ``.gaia/pr-watch.armed`` marker becomes unreachable and the PR drops from the
owned set (BOU-1587). This ledger persists the session->PR membership OUTSIDE any
worktree (under ``$HOME`` by default), so a PR is never silently dropped after
teardown. Pure file I/O -- no git, no gh.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

_DEFAULT_DIR = os.path.expanduser("~/.gaia/pr-watch/ledger")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@contextmanager
def _flock(lock_path: str):
    """Hold an exclusive advisory lock for the duration of a read-modify-write.

    Serializes concurrent `append`/`prune`/claim operations so two sub-agents
    arming different PRs for the same session can't both read the old file and
    clobber each other's entry (PR #16 review, P1).
    """
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass(frozen=True)
class LedgerEntry:
    pr: int
    branch: str
    worktree: str
    opened_at: str
    baseline_sha: str | None = None


def _dir() -> str:
    return os.environ.get("GAIA_PR_LEDGER_DIR", _DEFAULT_DIR)


def _safe_session(session_id: str) -> str:
    return _SAFE.sub("-", session_id).strip("-") or "unknown"


def ledger_path(session_id: str) -> str:
    return os.path.join(_dir(), f"session-{_safe_session(session_id)}.jsonl")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read(session_id: str) -> list[LedgerEntry]:
    path = ledger_path(session_id)
    out: dict[int, LedgerEntry] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    pr = int(d["pr"])
                except (ValueError, KeyError, TypeError):
                    continue
                out[pr] = LedgerEntry(
                    pr=pr,
                    branch=str(d.get("branch", "")),
                    worktree=str(d.get("worktree", "")),
                    opened_at=str(d.get("opened_at", "")),
                    baseline_sha=d.get("baseline_sha"),
                )
    except FileNotFoundError:
        return []
    return list(out.values())


def _write_all(session_id: str, entries: list[LedgerEntry]) -> None:
    path = ledger_path(session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".ledger.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps({
                    "pr": e.pr, "branch": e.branch, "worktree": e.worktree,
                    "opened_at": e.opened_at, "baseline_sha": e.baseline_sha,
                }) + "\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def append(session_id: str, pr: int, branch: str, worktree: str,
           baseline_sha: str | None = None) -> None:
    """Idempotent on ``pr`` -- re-arming the same PR overwrites its entry (last wins).

    The read-modify-write is serialized under an exclusive lock so concurrent
    appends from parallel sub-agents never drop each other's PR (PR #16 review).
    """
    with _flock(ledger_path(session_id) + ".lock"):
        entries = [e for e in read(session_id) if e.pr != int(pr)]
        entries.append(LedgerEntry(int(pr), branch, worktree, _now(), baseline_sha))
        _write_all(session_id, entries)


def prune(session_id: str, drop_prs: set[int]) -> None:
    drop = {int(p) for p in drop_prs}
    with _flock(ledger_path(session_id) + ".lock"):
        entries = [e for e in read(session_id) if e.pr not in drop]
        _write_all(session_id, entries)


def claim_lock(pr: int):
    """Exclusive lock for the read-decide-write of a PR claim (PR #16 review, P1)."""
    return _flock(claim_path(pr) + ".lock")


def _claim_dir() -> str:
    return os.environ.get(
        "GAIA_PR_CLAIM_DIR",
        os.path.join(os.path.dirname(_dir().rstrip("/")), "claims"))


def claim_path(pr: int) -> str:
    return os.path.join(_claim_dir(), f"pr-{int(pr)}.json")


def read_claim(pr: int) -> dict | None:
    try:
        with open(claim_path(pr), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return None


def write_claim(pr: int, session_id: str, pid: int) -> None:
    path = claim_path(pr)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = json.dumps({"pr": int(pr), "session_id": session_id,
                          "pid": int(pid), "claimed_at": _now()})
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".claim.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def list_session_ids() -> list[str]:
    try:
        names = os.listdir(_dir())
    except FileNotFoundError:
        return []
    out = []
    for n in names:
        if n.startswith("session-") and n.endswith(".jsonl"):
            out.append(n[len("session-"):-len(".jsonl")])
    return out
