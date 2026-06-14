"""Shared session branch-drift detection.

A PR-watch coordination helper used by Stop / pre-completion QA gates to stand
down when the current session started on a *different* non-primary branch in the
*same* worktree. This guards the multi-session-per-worktree case (e.g. a
worktree-console relaunch or a sibling agent) so a gate does not push, commit, or
PR sibling-owned work.

The logic reads ``session_registry`` events (the package owns reading — no JSONL
fallback) and looks for the most-recent ``started`` event for any of the
candidate session ids. Drift is reported only when that started event:

  * has a non-empty branch that differs from ``current_branch``,
  * the started branch is not a primary branch (main/master), and
  * the started worktree resolves to the same path as ``worktree``.
"""

from __future__ import annotations

from pathlib import Path

from . import session_registry

DEFAULT_PRIMARY_BRANCHES: tuple[str, ...] = ("main", "master")


def branch_drift_message(
    session_ids: list[str],
    current_branch: str,
    worktree: Path,
    *,
    primary_branches: tuple[str, ...] = DEFAULT_PRIMARY_BRANCHES,
) -> str | None:
    """Return a stand-down message when the session drifted branches, else None.

    ``session_ids`` are the candidate session ids to look up (e.g. the Stop
    payload's ``session_id`` plus a launcher-provided session id). ``worktree``
    is the current checkout root. ``primary_branches`` are branches that never
    count as drift (a session that started on main and moved to a feature branch
    is doing real work, not sibling work).
    """
    candidate_ids = [str(value).strip() for value in session_ids if str(value).strip()]
    candidate_ids = list(dict.fromkeys(candidate_ids))
    if not candidate_ids or not current_branch:
        return None

    events = session_registry.read_events()

    started_event: dict | None = None
    candidate_set = set(candidate_ids)
    for event in reversed(events):
        if str(event.get("session_id") or "") not in candidate_set:
            continue
        if str(event.get("event") or "") != "started":
            continue
        started_event = event
        break
    if started_event is None:
        return None

    started_branch = str(started_event.get("branch") or "").strip()
    primary = set(primary_branches)
    if (
        not started_branch
        or started_branch == current_branch
        or started_branch in primary
    ):
        return None

    started_worktree = str(started_event.get("worktree_path") or "").strip()
    if not started_worktree:
        return None
    try:
        registered_worktree = Path(started_worktree).expanduser().resolve()
        current_worktree = Path(worktree).resolve()
    except OSError:
        return None
    if registered_worktree != current_worktree:
        return None

    return (
        "Session branch drift detected: this session started on "
        f"'{started_branch}' but the checkout is now on '{current_branch}'. "
        "Standing down so the QA gate does not ask this session to commit, "
        "push, or PR sibling-owned work."
    )
