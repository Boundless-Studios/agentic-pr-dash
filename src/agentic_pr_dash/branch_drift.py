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

    # Resolve the registry on the TARGET worktree's behalf — the hook/adapter
    # process may run from a different cwd than the worktree being gated, and
    # that worktree can point its registry elsewhere via agentic-pr-dash.toml.
    registry = session_registry.registry_path(str(worktree))
    events = session_registry.read_events(registry)

    # The newest `started` event per candidate id (session-id reuse across
    # relaunches means a later `started` supersedes an earlier one for THAT id).
    # We keep one started event per id rather than collapsing to a single global
    # newest, so a benign newer candidate cannot mask a drifting older candidate.
    candidate_set = set(candidate_ids)
    started_by_id: dict[str, dict] = {}
    transitions_by_id: dict[str, list[dict]] = {}
    for event in reversed(events):
        session_id = str(event.get("session_id") or "")
        if session_id not in candidate_set:
            continue
        if session_id in started_by_id:
            continue
        if event.get("event") == "branch_transition" and event.get("attributed") is True:
            transitions_by_id.setdefault(session_id, []).append(event)
            continue
        if str(event.get("event") or "") != "started":
            continue
        started_by_id[session_id] = event
        if len(started_by_id) == len(candidate_set):
            break
    if not started_by_id:
        return None

    primary = set(primary_branches)
    try:
        current_worktree = Path(worktree).resolve()
    except OSError:
        return None

    for started_event in started_by_id.values():
        started_branch = str(started_event.get("branch") or "").strip()
        session_id = str(started_event.get("session_id") or "")
        lineage_branch = started_branch
        for transition in reversed(transitions_by_id.get(session_id, [])):
            transition_worktree = str(transition.get("worktree_path") or "").strip()
            started_worktree = str(started_event.get("worktree_path") or "").strip()
            try:
                if Path(transition_worktree).expanduser().resolve() != Path(started_worktree).expanduser().resolve():
                    continue
            except OSError:
                continue
            if str(transition.get("from_branch") or "").strip() != lineage_branch:
                continue
            lineage_branch = str(transition.get("branch") or "").strip()
        started_branch = lineage_branch
        if (
            not started_branch
            or started_branch == current_branch
            or started_branch in primary
        ):
            continue

        started_worktree = str(started_event.get("worktree_path") or "").strip()
        if not started_worktree:
            continue
        try:
            registered_worktree = Path(started_worktree).expanduser().resolve()
        except OSError:
            continue
        if registered_worktree != current_worktree:
            continue

        return (
            "Session branch drift detected: this session started on "
            f"'{started_branch}' but the checkout is now on '{current_branch}'. "
            "Standing down so the QA gate does not ask this session to commit, "
            "push, or PR sibling-owned work."
        )

    return None
