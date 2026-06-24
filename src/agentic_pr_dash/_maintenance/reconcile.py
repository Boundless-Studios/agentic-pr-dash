"""Reconciliation — orphan adoption and detached/owned PR records."""
from __future__ import annotations

import os

from ._common import _resolve_owner_pid, _repo_slug, _current_branch
from .pr_state import _pr_open_state, _unpack_pr_open_state, _thread_is_p1
from .markers import _read_marker, _session_is_live, _claim_pr
from .worktrees import (
    _iter_worktree_paths,
    _live_independent_owner_paths,
    _maint_roots_for,
    _worktree_is_for_entry,
    _collect_owned_worktrees,
)


def _adopt_orphan_prs(session_id: str, cwd: str, pid: int | None):
    """Claim PRs orphaned by DEAD sessions and adopt them into THIS session's ledger."""
    from agentic_pr_dash import session_ledger, github_api  # noqa: PLC0415

    eff_pid = pid if pid is not None else _resolve_owner_pid()
    present = set(_iter_worktree_paths(cwd))
    target_repo = _repo_slug(cwd)
    adopted = []
    for other in session_ledger.list_session_ids():
        if other == session_id:
            continue
        if _session_is_live(other, cwd):
            continue
        for e in session_ledger.read(other, repo=target_repo):
            abs_wt = os.path.abspath(e.worktree) if e.worktree else ""
            if abs_wt and abs_wt in present and _worktree_is_for_entry(abs_wt, e):
                continue
            state, url, has_fail, failing, review_decision, merge_state, mergeable = (
                _unpack_pr_open_state(_pr_open_state(e.pr, cwd))
            )
            if state in ("merged", "closed", "unknown", "draft"):
                continue
            threads = github_api.get_review_threads(e.pr, cwd)
            unresolved = [t for t in threads if not t.is_resolved and not t.is_outdated]
            changes_requested = str(review_decision).upper() == "CHANGES_REQUESTED"
            merge_conflict = (
                str(merge_state).upper() == "DIRTY"
                or str(mergeable).upper() == "CONFLICTING"
            )
            if not unresolved and not has_fail and not changes_requested and not merge_conflict:
                continue
            if not _claim_pr(e.pr, session_id, int(eff_pid), e.repo):
                continue
            session_ledger.append(session_id, e.pr, e.branch, e.worktree,
                                  e.baseline_sha, repo=e.repo)
            adopted.append({
                "pr": e.pr, "url": url or f"(pr {e.pr})", "branch": e.branch,
                "worktree_present": False, "unresolved_threads": len(unresolved),
                "ci_failing": has_fail, "failing_checks": failing,
                "changes_requested": changes_requested,
                "review_decision": review_decision,
                "merge_conflict": merge_conflict,
                "merge_state": merge_state,
                "mergeable": mergeable,
                "p1": any(_thread_is_p1(t) for t in unresolved), "state": state,
                "adopted_from": other,
            })
    return adopted


def _detached_pr_records(session_id: str, cwd: str,
                         include_legacy: bool = True,
                         prune_legacy: bool = True) -> list[dict]:
    """Records for this session's ledger PRs whose worktree is GONE, with live GitHub state."""
    from agentic_pr_dash import session_ledger, github_api  # noqa: PLC0415

    present_worktrees = set(_iter_worktree_paths(cwd))
    independent_worktrees = _live_independent_owner_paths(present_worktrees, session_id)
    target_repo = _repo_slug(cwd)
    records: list[dict] = []
    prune: set[int] = set()
    for e in session_ledger.read(session_id, repo=target_repo, include_legacy=include_legacy):
        abs_wt = os.path.abspath(e.worktree) if e.worktree else ""
        if (
            abs_wt
            and abs_wt in independent_worktrees
            and not _read_marker(abs_wt)
            and _current_branch(abs_wt) == e.branch
        ):
            continue
        if abs_wt and abs_wt in present_worktrees and _worktree_is_for_entry(abs_wt, e):
            continue
        state, url, has_fail, failing, review_decision, merge_state, mergeable = (
            _unpack_pr_open_state(_pr_open_state(e.pr, cwd))
        )
        if state in ("merged", "closed"):
            prune.add(e.pr)
            continue
        if state == "draft":
            continue
        threads = github_api.get_review_threads(e.pr, cwd)
        unresolved = [t for t in threads if not t.is_resolved and not t.is_outdated]
        changes_requested = str(review_decision).upper() == "CHANGES_REQUESTED"
        merge_conflict = (
            str(merge_state).upper() == "DIRTY"
            or str(mergeable).upper() == "CONFLICTING"
        )
        records.append({
            "pr": e.pr, "url": url or f"(pr {e.pr})", "branch": e.branch,
            "worktree_present": False, "unresolved_threads": len(unresolved),
            "ci_failing": has_fail, "failing_checks": failing,
            # BOU-1789: required CI still running on a detached (no-worktree) PR
            # is a watch condition the await waiter must honour, since `owned`
            # may be empty for a ledger-only PR (codex PR #50 review).
            "ci_watch_pending": (
                not has_fail and github_api.required_checks_pending(e.pr, cwd)
            ),
            "changes_requested": changes_requested,
            "review_decision": review_decision,
            "merge_conflict": merge_conflict,
            "merge_state": merge_state,
            "mergeable": mergeable,
            "p1": any(_thread_is_p1(t) for t in unresolved), "state": state,
        })
    if prune:
        session_ledger.prune(session_id, prune, repo=target_repo,
                             include_legacy=(include_legacy and prune_legacy))
    return records


def _owned_pr_records(session_id: str, cwd: str, pid: int | None, adopt_orphans: bool,
                      include_legacy: bool = True, prune_legacy: bool = True):
    """Union of live-worktree PRs and detached ledger PRs, each with live state."""
    from agentic_pr_dash import github_api  # noqa: PLC0415

    records: dict[int, dict] = {}

    for rec in _detached_pr_records(session_id, cwd, include_legacy=include_legacy,
                                    prune_legacy=prune_legacy):
        records[rec["pr"]] = rec

    for wt in _collect_owned_worktrees(session_id, cwd, pid):
        marker = _read_marker(wt) or {}
        pr_raw = marker.get("pr")
        if not pr_raw or not str(pr_raw).isdigit():
            continue
        pr = int(pr_raw)
        if pr in records:
            records[pr]["worktree_present"] = True
            continue
        state, url, has_fail, failing, review_decision, merge_state, mergeable = (
            _unpack_pr_open_state(_pr_open_state(pr, wt))
        )
        if state in ("merged", "closed"):
            continue
        threads = github_api.get_review_threads(pr, wt)
        unresolved = [t for t in threads if not t.is_resolved and not t.is_outdated]
        changes_requested = str(review_decision).upper() == "CHANGES_REQUESTED"
        merge_conflict = (
            str(merge_state).upper() == "DIRTY"
            or str(mergeable).upper() == "CONFLICTING"
        )
        records[pr] = {
            "pr": pr, "url": url or f"(pr {pr})", "branch": _current_branch(wt),
            "worktree_present": True, "unresolved_threads": len(unresolved),
            "ci_failing": has_fail, "failing_checks": failing,
            "changes_requested": changes_requested,
            "review_decision": review_decision,
            "merge_conflict": merge_conflict,
            "merge_state": merge_state,
            "mergeable": mergeable,
            "p1": any(_thread_is_p1(t) for t in unresolved), "state": state,
        }

    if adopt_orphans:
        for rec in _adopt_orphan_prs(session_id, cwd, pid):
            records.setdefault(rec["pr"], rec)

    ordered = sorted(records.values(),
                     key=lambda r: (0 if r["p1"] else 1, -r["unresolved_threads"], r["pr"]))
    return ordered


def _owned_pr_records_all_roots(session_id: str, anchor_cwd: str, pid: int | None,
                                adopt_orphans: bool):
    """Owned-PR records across ``[anchor] + maintenance_repo_roots``, keyed by repo."""
    roots = _maint_roots_for(anchor_cwd)
    prune_legacy = len(roots) <= 1
    merged: dict[tuple[str, int], dict] = {}
    for root in roots:
        repo = _repo_slug(root)
        for rec in _owned_pr_records(session_id, root, pid, adopt_orphans,
                                     include_legacy=True, prune_legacy=prune_legacy):
            key = (repo, rec["pr"])
            existing = merged.get(key)
            if existing is None:
                rec = {**rec, "repo": repo}
                merged[key] = rec
            elif rec.get("worktree_present") and not existing.get("worktree_present"):
                existing["worktree_present"] = True
    ordered = sorted(merged.values(),
                     key=lambda r: (0 if r["p1"] else 1, -r["unresolved_threads"], r["pr"]))
    return ordered
