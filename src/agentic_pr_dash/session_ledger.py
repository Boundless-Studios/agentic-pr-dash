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
    # GitHub ``owner/name`` the PR belongs to. One session can span multiple
    # repos via different ``--cwd`` checkouts, and PR numbers are per-repo, so the
    # ledger must scope by repo or a same-number PR in another repo would clobber
    # or mis-resolve it (PR #16 review round 2, P1). Empty == legacy entry written
    # before repo scoping; treated as "unknown repo" and never dropped on filter.
    repo: str = ""


def _dir() -> str:
    return os.environ.get("GAIA_PR_LEDGER_DIR", _DEFAULT_DIR)


def _safe_session(session_id: str) -> str:
    return _SAFE.sub("-", session_id).strip("-") or "unknown"


def ledger_path(session_id: str) -> str:
    return os.path.join(_dir(), f"session-{_safe_session(session_id)}.jsonl")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read(session_id: str, repo: str | None = None,
         include_legacy: bool = True) -> list[LedgerEntry]:
    """Entries for a session, deduplicated by ``(repo, pr)`` (last line wins).

    When ``repo`` is given, restrict the result to that repo PLUS any legacy
    entries with no recorded repo (which predate repo scoping and whose repo is
    therefore unknown — dropping them would silently stop monitoring them). The
    ``(repo, pr)`` dedup key means the SAME PR number in two repos no longer
    overwrites the other (PR #16 review round 2, P1).

    ``include_legacy=False`` excludes the repo-less legacy entries — used when
    iterating MULTIPLE roots so a legacy entry is processed against exactly one
    repo (the anchor) instead of being replayed against every sibling, which
    would let a same-number PR in one repo prune the legacy entry before the
    real repo is checked (codex PR #30 review, P2)."""
    path = ledger_path(session_id)
    out: dict[tuple[str, int], LedgerEntry] = {}
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
                entry_repo = str(d.get("repo", ""))
                out[(entry_repo, pr)] = LedgerEntry(
                    pr=pr,
                    branch=str(d.get("branch", "")),
                    worktree=str(d.get("worktree", "")),
                    opened_at=str(d.get("opened_at", "")),
                    baseline_sha=d.get("baseline_sha"),
                    repo=entry_repo,
                )
    except FileNotFoundError:
        return []
    entries = list(out.values())
    if repo is not None:
        # A row is "legacy" iff it has NO recorded repo. In strict mode
        # (include_legacy=False) legacy rows are excluded REGARDLESS of `repo` —
        # otherwise a strict read with an empty `repo` (undetectable remote) would
        # still match legacy rows via ``e.repo == repo == ""`` (codex PR #32, P2).
        if include_legacy:
            entries = [e for e in entries if e.repo == repo or not e.repo]
        else:
            entries = [e for e in entries if e.repo and e.repo == repo]
    return entries


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
                    "repo": e.repo,
                }) + "\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def append(session_id: str, pr: int, branch: str, worktree: str,
           baseline_sha: str | None = None, repo: str = "") -> None:
    """Idempotent on ``(repo, pr)`` -- re-arming the same PR in the same repo
    overwrites its entry (last wins). Two repos that both have PR #N keep
    SEPARATE entries (PR #16 review round 2, P1).

    The read-modify-write is serialized under an exclusive lock so concurrent
    appends from parallel sub-agents never drop each other's PR (PR #16 review).
    """
    repo = repo or ""
    with _flock(ledger_path(session_id) + ".lock"):
        entries = [e for e in read(session_id)
                   if not (e.pr == int(pr) and e.repo == repo)]
        entries.append(LedgerEntry(int(pr), branch, worktree, _now(),
                                   baseline_sha, repo))
        _write_all(session_id, entries)


def prune(session_id: str, drop_prs: set[int], repo: str | None = None,
          include_legacy: bool = True) -> None:
    """Drop ledger entries by PR number. When ``repo`` is given, only entries for
    that repo (plus legacy repo-less entries) are eligible — so pruning a
    merged/closed PR in repoA never drops a same-number PR in repoB (PR #16
    review round 2, P1).

    ``include_legacy=False`` makes legacy repo-less entries INeligible for this
    prune — used on non-anchor roots so a sibling's same-number closed PR can't
    drop a legacy entry that belongs to another repo (codex PR #30 review, P2)."""
    drop = {int(p) for p in drop_prs}
    with _flock(ledger_path(session_id) + ".lock"):
        entries = []
        for e in read(session_id):
            # A legacy (repo-less) row is eligible ONLY via include_legacy, never
            # via ``e.repo == repo`` — else a strict prune with an empty `repo`
            # would still drop legacy rows (codex PR #32, P2).
            if repo is None:
                eligible = True
            elif not e.repo:
                eligible = include_legacy
            else:
                eligible = e.repo == repo
            if eligible and e.pr in drop:
                continue
            entries.append(e)
        _write_all(session_id, entries)


def claim_lock(pr: int, repo: str = ""):
    """Exclusive lock for the read-decide-write of a PR claim (PR #16 review, P1)."""
    return _flock(claim_path(pr, repo) + ".lock")


def _claim_dir() -> str:
    return os.environ.get(
        "GAIA_PR_CLAIM_DIR",
        os.path.join(os.path.dirname(_dir().rstrip("/")), "claims"))


def claim_path(pr: int, repo: str = "") -> str:
    """Path of the exclusive-claim file for ``(repo, pr)``.

    The repo is folded into the filename so two repos with the same PR number get
    distinct claim files and can't steal each other's orphan (PR #16 review round
    2, P1). With no repo (legacy callers) the historical ``pr-<n>.json`` name is
    preserved for backward compatibility.
    """
    if repo:
        return os.path.join(_claim_dir(), f"pr-{_safe_session(repo)}-{int(pr)}.json")
    return os.path.join(_claim_dir(), f"pr-{int(pr)}.json")


def read_claim(pr: int, repo: str = "") -> dict | None:
    try:
        with open(claim_path(pr, repo), encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return None


def write_claim(pr: int, session_id: str, pid: int, repo: str = "") -> None:
    path = claim_path(pr, repo)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = json.dumps({"pr": int(pr), "session_id": session_id,
                          "pid": int(pid), "claimed_at": _now(), "repo": repo})
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
