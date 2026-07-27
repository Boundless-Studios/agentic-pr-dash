"""Reconciliation — orphan adoption and detached/owned PR records."""
from __future__ import annotations

import os

from ._common import _resolve_owner_pid, _repo_slug, _current_branch
from .pr_state import _pr_open_state, _unpack_pr_open_state, _thread_is_p1
from agentic_pr_dash import ownership
from .markers import (
    _claim_pr,
    _live_foreign_owner,
    _live_pr_owner_record,
    _marker_session_id,
    _read_marker,
    _session_is_live,
    _write_arm_marker,
    release_ownership_claims as _release_ownership_claims,
)
from .ownership_resolution import resolve_worktree
from .worktrees import (
    _iter_worktree_paths,
    _live_independent_owner_paths,
    _maint_roots_for,
    _worktree_is_for_entry,
    _collect_owned_worktrees,
)


def _unknown_gh_state_record(
    pr: int, url: str, branch: str, *, worktree_present: bool, repo: str | None = None,
) -> dict:
    """A forced-pending record for a PR whose gh state could not be resolved.

    ``_pr_open_state`` returns its ``unavailable`` sentinel — ``state="unknown"``,
    every other field falsy/empty — whenever the ``gh pr view`` probe fails for
    ANY reason (auth lapse, secondary rate limit, timeout, network blip). Before
    this fix, that sentinel flowed straight into a JSON record and was
    INDISTINGUISHABLE, field-for-field, from "verified: this PR has no
    blockers" (``unresolved_threads: 0``, ``ci_failing: false``,
    ``changes_requested: false``, ``merge_conflict: false``) — a transient gh
    outage silently reported every affected PR as clean while GitHub showed
    real unresolved threads. Fail closed instead: mark the record unmistakably
    unknown (``gh_state_unknown: True``) and force ``p1: True`` so no consumer —
    human operator or the stop gate's ``_record_has_blockers`` — can mistake it
    for a resolved PR. Deliberately skips the review-threads/CI-watch probes:
    the PR-state probe already failed, so a second gh call's result (success or
    coincidental empty-on-failure) must not be trusted to build a clean-looking
    record around this one.
    """
    record = {
        "pr": pr, "url": url or f"(pr {pr})", "branch": branch,
        "worktree_present": worktree_present, "unresolved_threads": 0,
        "ci_failing": False, "failing_checks": [], "ci_watch_pending": False,
        "changes_requested": False, "review_decision": "",
        "merge_conflict": False, "merge_state": "", "mergeable": "",
        "p1": True, "state": "unknown", "gh_state_unknown": True,
    }
    if repo is not None:
        record["repo"] = repo
    return record


def _adopt_orphan_prs(session_id: str, cwd: str, pid: int | None):
    """Claim PRs orphaned by DEAD sessions and adopt them into THIS session's ledger."""
    from agentic_pr_dash import session_ledger, github_api  # noqa: PLC0415

    eff_pid = pid if pid is not None else _resolve_owner_pid()
    present = set(_iter_worktree_paths(cwd))
    target_repo = _repo_slug(cwd)
    # One store read for the whole pass (BOU-2223 Stage 3).
    snap = ownership.snapshot()
    adopted = []
    for other in session_ledger.list_session_ids():
        if other == session_id:
            continue
        if _session_is_live(other, cwd):
            continue
        for e in session_ledger.read(other, repo=target_repo):
            abs_wt = os.path.abspath(e.worktree) if e.worktree else ""
            wt_present = bool(abs_wt) and abs_wt in present and _worktree_is_for_entry(abs_wt, e)
            if wt_present:
                # A present worktree used to be skipped outright on the theory
                # the marker scan covers it — but the marker scan never takes
                # over a worktree markered by ANOTHER session (BOU-1953), so a
                # dead session's PR whose worktree still exists fell through
                # every path and its blockers sat unserviced (BOU-2184). Skip
                # only when a live owner (or this session) actually covers it.
                owned = resolve_worktree(
                    abs_wt, kind="reconcile_divergence", snap=snap
                )
                if owned.unknown:
                    # Fail closed (P1 adoption-theft defect): the claim store
                    # could not be read this tick and no local marker exists —
                    # we genuinely do not know whether a live session covers
                    # this worktree, so this entry must NOT be treated as a
                    # dead session's orphan. Skip it; a later tick with a
                    # healthy store resolves ownership correctly.
                    continue
                marker_sid = _marker_session_id(abs_wt)
                # Union (BOU-2223 Stage 3): ours by EITHER source means covered.
                if marker_sid == session_id or owned.owned_by(session_id):
                    continue
                if _live_foreign_owner(abs_wt, session_id):
                    continue
                # A live foreign CLAIM covers it too — union with the marker
                # checks, never a replacement.
                if any(sid != session_id for sid in owned.claim_session_ids):
                    continue
                if marker_sid and _session_is_live(marker_sid, abs_wt):
                    continue
                # The marker checks above only see THIS worktree's marker
                # (PR #82 review, P1): a LIVE session that repointed its
                # worktree elsewhere leaves a dead marker here while still
                # owning the same ``(repo, pr)`` in the durable ledger.
                # Re-stamping the marker below would make the adoption look
                # self-owned to ``_check_worktree`` (which then skips its
                # ledger fallback) → concurrent maintenance against a live
                # owner. Defer to any live durable owner instead.
                if _live_pr_owner_record(
                    e.pr, e.repo or target_repo, session_id, abs_wt
                ) is not None:
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
            if wt_present:
                # Make the adoption durable: re-stamp the dead owner's marker so
                # subsequent list-owned/stop-gate scans see this session's
                # ownership of the still-present worktree (BOU-2184).
                _write_arm_marker(abs_wt, session_id, int(eff_pid), int(e.pr))
            adopted.append({
                "pr": e.pr, "url": url or f"(pr {e.pr})", "branch": e.branch,
                "worktree_present": wt_present, "unresolved_threads": len(unresolved),
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
    # One store read for the whole pass (BOU-2223 Stage 3).
    snap = ownership.snapshot()
    records: list[dict] = []
    prune: set[int] = set()
    for e in session_ledger.read(session_id, repo=target_repo, include_legacy=include_legacy):
        abs_wt = os.path.abspath(e.worktree) if e.worktree else ""
        if (
            abs_wt
            and abs_wt in independent_worktrees
            # Unowned by EITHER source — a live claim alone is enough to keep
            # this worktree out of the detached set (BOU-2223 Stage 3).
            and not _read_marker(abs_wt)
            and not resolve_worktree(
                abs_wt, kind="detached_divergence", snap=snap
            ).claim_session_ids
            and _current_branch(abs_wt) == e.branch
        ):
            continue
        if abs_wt and abs_wt in present_worktrees and _worktree_is_for_entry(abs_wt, e, snap=snap):
            continue
        state, url, has_fail, failing, review_decision, merge_state, mergeable = (
            _unpack_pr_open_state(_pr_open_state(e.pr, cwd))
        )
        if state in ("merged", "closed"):
            prune.add(e.pr)
            continue
        if state == "draft":
            continue
        if state == "unknown":
            records.append(_unknown_gh_state_record(
                e.pr, url, e.branch, worktree_present=False,
                repo=e.repo or target_repo,
            ))
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
            # Repo identity so a cross-anchor dedupe (await's _await_anchors span)
            # keeps the SAME pr number + branch in two different repos distinct,
            # instead of collapsing them to one ("", pr, branch) key and dropping
            # the second repo's blockers (PR #61 review, P2). Legacy repo-less rows
            # fall back to the anchor's repo.
            "repo": e.repo or target_repo,
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
        # Release the ownership claims too. This path retires merged/closed PRs
        # that have NO live worktree, so `_prune_stale_marker` — the only other
        # release site — never sees them: it is driven by a marker in a worktree
        # this one has already established is gone. Without this, a PR that merged
        # after its worktree was removed leaves a live claim that nothing ever
        # clears (BOU-2223 Stage 4).
        _release_ownership_claims(cwd, prune, session_id)
    return records


def _owned_pr_records(session_id: str, cwd: str, pid: int | None, adopt_orphans: bool,
                      include_legacy: bool = True, prune_legacy: bool = True,
                      adopt_unmarked: bool = True):
    """Union of live-worktree PRs and detached ledger PRs, each with live state.

    ``adopt_unmarked=False`` scans this root's worktrees read-only — already
    markered worktrees stay visible but unmarked open ``@me`` PRs are NOT armed.
    Sibling (non-anchor) roots in :func:`_owned_pr_records_all_roots` pass this so
    a cross-root scan never claims an unrelated idle PR merely because it belongs
    to ``@me`` (BOU-1814).
    """
    from agentic_pr_dash import github_api  # noqa: PLC0415

    records: dict[int, dict] = {}

    for rec in _detached_pr_records(session_id, cwd, include_legacy=include_legacy,
                                    prune_legacy=prune_legacy):
        records[rec["pr"]] = rec

    # Only thread the flags when DISABLING unmarked adoption (sibling roots).
    # The anchor path keeps the default call so its callers/stubs need no
    # signature change. Sibling roots swap missed-arm pickup for dead-owner
    # marker takeover so a dead sub-agent's armed PR still surfaces (BOU-2184).
    collect_kwargs = (
        {} if adopt_unmarked else {"adopt_unmarked": False, "adopt_dead_markered": True}
    )
    owned_snap = ownership.snapshot()
    for wt in _collect_owned_worktrees(session_id, cwd, pid, **collect_kwargs):
        # Claim-first, marker fallback (BOU-2223 Stage 3). The marker read stays
        # module-level: it is the freshest signal of which PR this worktree is
        # for right now, and it is the seam the existing tests inject through.
        marker = _read_marker(wt) or {}
        pr_raw = marker.get("pr")
        if pr_raw and str(pr_raw).isdigit():
            pr = int(pr_raw)
        else:
            pr = resolve_worktree(
                wt, kind="owned_records_divergence", snap=owned_snap
            ).pr_number
        if pr is None:
            continue
        if pr in records:
            records[pr]["worktree_present"] = True
            continue
        state, url, has_fail, failing, review_decision, merge_state, mergeable = (
            _unpack_pr_open_state(_pr_open_state(pr, wt))
        )
        if state in ("merged", "closed"):
            continue
        if state == "unknown":
            records[pr] = _unknown_gh_state_record(
                pr, url, _current_branch(wt), worktree_present=True,
            )
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
    anchor = os.path.abspath(os.path.expanduser(anchor_cwd))
    merged: dict[tuple[str, int], dict] = {}
    for root in roots:
        repo = _repo_slug(root)
        # Only the anchor/current root adopts unmarked open @me PRs; sibling
        # roots are scanned read-only so a cross-root scan can't arm pr-watch
        # for an idle PR that belongs to another session (BOU-1814).
        adopt_unmarked = os.path.abspath(root) == anchor
        for rec in _owned_pr_records(session_id, root, pid, adopt_orphans,
                                     include_legacy=True, prune_legacy=prune_legacy,
                                     adopt_unmarked=adopt_unmarked):
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
