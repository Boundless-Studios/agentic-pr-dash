"""Runtime-agnostic PR maintenance check CLI — stateless, read-only worker.

Subcommands:
  check    — resolve branch→PR, compute live blockers, print prompt, exit 10
             if work exists, else exit 0. READ-ONLY: writes no files.
  complete — re-fetch unresolved review threads from GitHub and resolve them
             statelessly (no ledger required). Best-effort close the bead.

Run via:  agentic-pr-dash <subcommand> [args]

Module-level imports are stdlib only; heavy deps are deferred into subcommand
functions so that ``--help`` works without the project venv's heavy deps.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # noqa: F401  — kept for mc.subprocess patch seam used by tests
import sys
import time

from .config import load as load_config

# ---------------------------------------------------------------------------
# Re-exports from maintenance subpackage (facade — keeps mc.X patchable by tests)
# ---------------------------------------------------------------------------

# _common
from ._maintenance._common import (  # noqa: F401, E402
    _parse_iso,
    _env_int,
    _fix_lease_seconds,
    _pid_alive,
    _resolve_owner_pid,
    _current_branch,
    _repo_slug,
)

# pr_state
from ._maintenance.pr_state import (  # noqa: F401, E402
    _GH_UNAVAILABLE,
    _gh_unavailable_message,
    _resolve_pr_for_branch,
    _resolve_pr_by_number,
    _pr_draft_status,
    _pr_draft_status_detailed,
    _pr_head_branch,
    _pr_head_branch_detailed,
    _gh_pr_list_json,
    _resolve_open_pr_for_branch,
    _list_my_open_prs,
    _unresolved_review_threads,
    _deferred_review_threads,
    pr_has_unresolved_review_threads,
    _pr_open_state,
    _unpack_pr_open_state,
    _thread_is_p1,
)

# deferred_review (BOU-2567)
from ._maintenance import deferred_review  # noqa: F401, E402

# markers
from ._maintenance.markers import (  # noqa: F401, E402
    _HEARTBEAT_TTL_SECONDS,
    _DEFAULT_FIX_LEASE_SECONDS,
    _DEFAULT_HEARTBEAT_MIN_INTERVAL_SECONDS,
    _marker_path,
    _read_marker,
    _heartbeat_ttl_seconds,
    _heartbeat_fresh,
    _fix_lease_active,
    _live_foreign_owner,
    _marker_live_foreign_pid,
    _heartbeat_min_interval_seconds,
    _touch_owner_heartbeat,
    _write_arm_marker,
    _marker_session_id,
    _read_session_marker,
    _prune_stale_marker,
    _session_is_live,
    _claim_pr,
)

# worktrees
from ._maintenance.worktrees import (  # noqa: F401, E402
    _iter_worktrees_with_branch,
    _iter_worktree_paths,
    _resolve_maintenance_roots,
    _maint_roots_for,
    _owned_worktrees_across_roots,
    _detached_records_across_roots,
    _self_pid_chain,
    _live_independent_owner_paths,
    _collect_owned_worktrees,
    _collect_stop_gate_worktrees,
    _worktree_is_for_entry,
)

# stop_gate
from ._maintenance.stop_gate import (  # noqa: F401, E402
    _stop_state_path,
    _load_stop_state,
    _save_stop_state,
    _stop_fingerprint,
    _extract_pr_number,
    _build_stop_block,
    _owned_open_pr_numbers,
    _owned_open_pr_pairs,
    _build_waiter_block,
    _stop_gate_impl,
    _record_has_blockers,
    _read_escalation_marker,
    _build_escalation_block,
)

# completion
from ._maintenance.completion import (  # noqa: F401, E402
    _commit_subject,
    _completion_reply_body,
    _mark_maintenance_complete,
    _review_comments_from_threads,
    _candidate_file_refs,
    _ref_matches_touched,
    _thread_points_elsewhere,
    _thread_elsewhere_refs,
    _thread_completion_evidence,
    _spans_intersect_line,
    _ANCHOR_CONTEXT_LINES,
    _FILE_REF_RE,
    _MODULE_REF_STOPWORDS,
)

# reconcile
from ._maintenance.reconcile import (  # noqa: F401, E402
    _adopt_orphan_prs,
    _detached_pr_records,
    _owned_pr_records,
    _owned_pr_records_all_roots,
)

# waiter
from ._maintenance.waiter import (  # noqa: F401, E402
    _await_pidfile,
    _read_await_pidfile,
    _write_await_pidfile,
    _remove_await_pidfile,
    _read_requested_roots,
    _update_await_coverage,
    _await_alive,
    _detached_loop_alive,
    _detached_pending_entry,
    _write_clean_exit_marker,
    _clear_clean_exit_marker,
    _read_clean_exit_keys,
    _clean_exit_key,
)


# ---------------------------------------------------------------------------
# worktree_check
# ---------------------------------------------------------------------------
from ._maintenance.worktree_check import _check_worktree, WARN_ONLY_MARKER  # noqa: F401, E402


# ---------------------------------------------------------------------------
# CLI functions (stay in maintenance_check.py)
# ---------------------------------------------------------------------------


def _collect_await_watch_pending(owned: list[str], cwd: str, session_id: str) -> bool:
    """True when any owned non-draft worktree PR is still watch-pending this tick.

    Resolves the PR for each owned worktree and calls
    ``maintenance.watch_pending_for_pr`` to determine whether required CI is
    still running (and there are no current actionable blockers). Used by
    ``_cmd_await`` to decide whether to stay alive past ``--max-wait`` when
    CI checks are in-flight.
    """
    from agentic_pr_dash import maintenance  # noqa: PLC0415
    for worktree in owned:
        pr = _resolve_pr_for_branch(worktree)
        if pr is _GH_UNAVAILABLE or pr is None:
            continue
        if pr.is_draft:
            continue
        # Populate ci_watch_pending (best-effort)
        from agentic_pr_dash import github_api  # noqa: PLC0415
        pr.ci_watch_pending = github_api.required_checks_pending(pr.number, worktree)
        if maintenance.watch_pending_for_pr(pr):
            return True
    return False


def _observe_finalization(args, policy, ledger):
    """Read one fail-closed PR/CI/review snapshot for ``finalize``."""

    from agentic_pr_dash import github_api  # noqa: PLC0415
    from agentic_pr_dash._maintenance.review_settlement import (  # noqa: PLC0415
        evaluate_pr_snapshot,
    )

    cwd = os.path.abspath(args.cwd)
    if args.pr is not None:
        pr = _resolve_pr_by_number(args.pr, cwd, force=True)
    else:
        pr = _resolve_pr_for_branch(cwd, force=True)
    if pr is _GH_UNAVAILABLE:
        raise RuntimeError(_gh_unavailable_message(cwd))
    if pr is None:
        raise RuntimeError("no open PR for this branch")

    repository = pr.repo or _repo_slug(cwd)
    pr = pr.model_copy(update={"repo": repository}, deep=True)
    github_api.reset_checks_probe_failure_seen()
    pr.ci_watch_pending = github_api.required_checks_pending(pr.number, cwd)
    if github_api.checks_probe_failure_seen():
        raise RuntimeError(
            "required CI status is unobservable; refusing to synthesize green"
        )
    threads = github_api.get_review_threads(pr.number, cwd, strict=True)
    review_submissions = github_api.get_review_submissions(
        pr.number,
        pr.latest_commit_sha,
        cwd,
        strict=True,
    )
    deferrals = deferred_review.deferred_threads_for_pr(cwd, pr.number)
    return evaluate_pr_snapshot(
        pr=pr,
        policy=policy,
        ledger=ledger,
        threads=threads,
        deferrals=deferrals,
        review_submissions=review_submissions,
    )


def _cmd_finalize(args: argparse.Namespace) -> int:
    """Require review settlement and two stable, fully green PR observations."""

    from pathlib import Path  # noqa: PLC0415

    from agent_review_coordinator import ReviewLedger, ReviewPolicy  # noqa: PLC0415
    from agentic_pr_dash._maintenance.review_settlement import (  # noqa: PLC0415
        combine_clean_observations,
    )

    try:
        policy = ReviewPolicy.from_yaml(
            Path(args.policy).read_text(encoding="utf-8")
        )
        ledger_path = Path(
            args.ledger
            or os.path.join(
                os.path.abspath(args.cwd),
                ".agentic-pr-dash",
                "review-ledger.json",
            )
        )
        if not ledger_path.is_file():
            cwd = os.path.abspath(args.cwd)
            if args.pr is not None:
                pr = _resolve_pr_by_number(args.pr, cwd, force=True)
            else:
                pr = _resolve_pr_for_branch(cwd, force=True)
            if pr is _GH_UNAVAILABLE:
                raise RuntimeError(_gh_unavailable_message(cwd))
            if pr is None:
                if args.json:
                    print(
                        json.dumps(
                            {
                                "settled": True,
                                "reason": "no_open_pr",
                            }
                        )
                    )
                else:
                    print("no open PR for this branch")
                return 0
            missing = {
                "settled": False,
                "blockers": ["review_ledger_missing"],
                "ledger": str(ledger_path),
            }
            if args.json:
                print(json.dumps(missing))
            else:
                print(
                    "PR not settled: review_ledger_missing "
                    f"({ledger_path})"
                )
            return 10
        ledger = ReviewLedger.model_validate_json(
            ledger_path.read_text(encoding="utf-8")
        )
        first = _observe_finalization(args, policy, ledger)
        if first.clean:
            time.sleep(args.stabilization_seconds)
            second = _observe_finalization(args, policy, ledger)
        else:
            second = None
        report = combine_clean_observations(first, second)
    except (OSError, RuntimeError, ValueError) as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "settled": False,
                        "observation_error": str(exc),
                    }
                )
            )
        else:
            print(f"finalization observation failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.model_dump(mode="json"), sort_keys=True))
    elif report.settled:
        print(
            f"PR review and CI settled at {report.head_sha} "
            f"after {report.observations} stable observations"
        )
    else:
        work = [
            *report.blockers,
            *report.review.required_actions,
            *report.review.missing_slots,
        ]
        print("PR not settled: " + ", ".join(work))
    return 0 if report.settled else 10


def _await_watch_pending_this_tick(
    owned: list[str],
    detached_records: list[dict],
    cwd: str,
    session_id: str,
) -> bool:
    """Watch-pending across BOTH live worktrees and detached (ledger-only) PRs.

    A session can own a PR solely through the ledger, so ``owned`` may be empty
    while its required CI is still running (codex PR #50 review). Shared by the
    BOU-1962 clean-state early exit and the ``--max-wait`` deadline path so both
    apply the same definition of "required CI still running".
    """
    detached_watch_pending = any(
        r.get("ci_watch_pending")
        and r.get("state") not in ("merged", "closed", "draft", "unknown")
        for r in detached_records
    )
    return detached_watch_pending or _collect_await_watch_pending(
        owned, cwd, session_id
    )


def _owned_pr_pairs_for_await(owned: list[str]) -> list[tuple[str, int]]:
    """``(worktree, pr)`` pairs for the waiter, claim-first with marker fallback.

    ``_owned_open_pr_pairs`` reads ``pr-watch.armed`` and nothing else. Stage 4 of
    BOU-2223 retired that file's WRITER, so on a current install it answers "this
    session owns no PR anywhere" — and a marker-only reader does not fail, it
    silently returns an empty set (BOU-2294). Here that emptiness fed the
    clean-exit verified set, which is what tells the stop gate which PRs a waiter
    already vouched for.

    Union, never intersection, per the Stage-2/3 precedent: the marker seam is
    left exactly as it is (several tests monkeypatch it directly and it is the
    specification) and the live claim is added alongside it.
    """
    from ._maintenance.ownership_resolution import resolve_worktree  # noqa: PLC0415
    from agentic_pr_dash import ownership  # noqa: PLC0415

    pairs = dict(_owned_open_pr_pairs(owned))
    missing = [wt for wt in owned if wt not in pairs]
    if missing:
        # ONE store read for every worktree still unresolved, never one each.
        snap = ownership.snapshot()
        for wt in missing:
            pr = resolve_worktree(wt, kind="waiter_divergence", snap=snap).pr_number
            if pr is not None:
                pairs[wt] = pr
    return list(pairs.items())


def _marker_pr_still_current(worktree: str, pr_number: int) -> bool:
    """True iff ``worktree``'s CURRENT branch still resolves to open PR
    ``pr_number``.

    The clean-exit verified set may only contain PRs this tick actually
    observed. The armed marker names the PR the worktree owned when it was
    armed — if the worktree has since been repointed to another branch,
    ``_check_worktree`` verified THAT branch's PR (or none), not the marker's,
    and the ledger still treats the matching marker as proof the worktree
    covers the PR, so it isn't probed as detached either. Recording the marker
    PR as verified-clean would then let the stop gate's marker coverage
    suppress the waiter while the original PR's feedback goes unobserved
    (codex PR #75 review, round 5). Resolution failures return False — fail
    closed: unverifiable is not verified.
    """
    branch = _current_branch(worktree)
    if not branch:
        return False
    resolved = _resolve_open_pr_for_branch(worktree, branch)
    return resolved is not None and resolved[0] == pr_number


def _cmd_check(args: argparse.Namespace) -> int:
    code, text = _check_worktree(args.cwd, args.session_id or "")
    print(text)
    return code


def _arm_gh_probe_budget_seconds() -> float:
    """Wall-clock budget (BOU-2477) shared by BOTH of `arm`'s gh probes.

    Default keeps real headroom under arm's documented ~10s Stop-hook
    preflight budget. ``AGENTIC_PR_DASH_ARM_GH_PROBE_BUDGET_SECONDS``
    overrides for tests/tuning; a non-positive or unparseable value disables
    the shared deadline (falls back to `_gh_pr_view_field`'s own
    attempt-count bound, matching pre-BOU-2477 behavior).
    """
    raw = os.environ.get("AGENTIC_PR_DASH_ARM_GH_PROBE_BUDGET_SECONDS", "").strip()
    if not raw:
        return 8.0
    try:
        value = float(raw)
    except ValueError:
        return 8.0
    return value if value > 0 else 0.0


def _cmd_arm(args: argparse.Namespace) -> int:
    """Explicitly register a worktree's open non-draft PR under a session."""
    cwd = os.path.abspath(args.cwd)
    session_id = args.session_id
    pid = args.pid if args.pid is not None else _resolve_owner_pid()

    pr_number = args.pr
    if pr_number is None:
        explicit_branch = getattr(args, "branch", None)
        if explicit_branch:
            explicit_branch = explicit_branch.split(":", 1)[-1]
        current_branch = _current_branch(cwd)
        if explicit_branch and explicit_branch != current_branch:
            print(
                f"branch {explicit_branch} is not checked out in {cwd}; not arming"
            )
            return 0
        branch = explicit_branch or current_branch
        if not branch:
            print("could not resolve branch; nothing to arm")
            return 0
        resolved = _resolve_open_pr_for_branch(cwd, branch)
        if resolved is None:
            print("no open PR for this branch; nothing to arm")
            return 0
        pr_number, is_draft = resolved
        if is_draft:
            print(f"PR #{pr_number} is a draft; not arming")
            return 0
    else:
        # BOU-2406: an INDETERMINATE gh result is not the same as "there is
        # nothing to arm". This used to print "gh unavailable" and return 0, so
        # the caller was told arming succeeded while no arm marker and no
        # session-ledger entry were written. That silence then propagates: with
        # no ledger entry, `_await_anchors` cannot discover the worktree, and the
        # feedback waiter exits `unbound` having watched nothing — a PR left with
        # zero coverage behind two successful-looking steps.
        #
        # The quiet paths below ("no open PR", "is a draft") are genuine
        # not-applicable cases and still return 0. Only "we could not find out"
        # is now loud and non-zero.
        # BOU-2477: ONE shared deadline for both probes below, so the total —
        # not each probe independently — stays inside the Stop-hook budget.
        _budget = _arm_gh_probe_budget_seconds()
        _deadline = time.monotonic() + _budget if _budget > 0 else None
        status, why = _pr_draft_status_detailed(cwd, int(pr_number), deadline=_deadline)
        if status is None:
            print(
                f"could not determine whether PR #{pr_number} is a draft; not arming.\n"
                f"  cause: {why}",
                file=sys.stderr,
            )
            return 1
        if status:
            print(f"PR #{pr_number} is a draft; not arming")
            return 0
        head_branch, why = _pr_head_branch_detailed(cwd, int(pr_number), deadline=_deadline)
        if head_branch is None:
            print(
                f"could not determine PR #{pr_number}'s head branch; not arming.\n"
                f"  cause: {why}",
                file=sys.stderr,
            )
            return 1
        if head_branch != _current_branch(cwd):
            print(
                f"PR #{pr_number} (head {head_branch}) is not checked out in {cwd}; not arming"
            )
            return 0

    if _write_arm_marker(cwd, session_id, int(pid), int(pr_number)):
        print(f"armed PR #{pr_number} for session {session_id} in {cwd}")
        return 0
    # BOU-2409: "could not write arm marker" reads as a filesystem problem —
    # the real cause (once marker writes are retired, BOU-2223 Stage 4) is
    # almost always a fenced ownership claim a DIFFERENT, live session holds.
    # Name that holder instead of the generic message, so the operator does
    # not go chasing a disk/permissions issue that does not exist.
    print(_arm_refusal_message(cwd, int(pr_number)), file=sys.stderr)
    return 1


def _arm_refusal_message(cwd: str, pr_number: int) -> str:
    """Explain why `arm` could not write, naming the current claim holder when
    one is resolvable (BOU-2409). Falls back to the generic message when the
    claim store can't be read or holds no claim for this PR — a write failure
    for some OTHER reason (e.g. a genuinely unwritable state dir) still needs
    to say something, and a stale/absent claim is not evidence of what the
    real cause was.
    """
    try:
        from agentic_pr_dash import ownership  # noqa: PLC0415
        repo = _repo_slug(cwd)
        snap = ownership.snapshot()
        # `claim_for` returns the recorded claim regardless of status or
        # liveness, so a released or dead-owner claim would otherwise be
        # reported as the CURRENT holder — hiding the real cause when the arm
        # failed for an unrelated reason (an unwritable state dir, say).
        # `live_owner_for` applies the marker-parity liveness rule, so a stale
        # claim falls through to the generic message instead.
        claim = snap.claim_for(repo, pr_number) if snap.known() else None
        live = snap.live_owner_for(repo, pr_number) if snap.known() else None
        if claim is not None and live is not None:
            provenance = (claim.owner.metadata or {}).get("provenance", "unknown")
            return (
                f"PR #{pr_number} is claimed by session {claim.owner.session_id} "
                f"(pid {claim.owner.pid}, provenance={provenance}, "
                f"lease expires {claim.lease_expires_at.isoformat()}); not arming "
                f"in {cwd}"
            )
    except Exception:  # noqa: BLE001 — a diagnostic must never crash the CLI
        pass
    return f"could not write arm marker in {cwd}"


def _cmd_list_owned(args: argparse.Namespace) -> int:
    """Print worktree paths this session owns — markered OR reconciled-and-adopted."""
    anchor = os.path.abspath(os.path.expanduser(args.cwd))
    seen: set[str] = set()
    anchor_failed = False
    resolved = _resolve_maintenance_roots(args.cwd)
    roots = resolved if anchor in resolved else [anchor, *resolved]
    for root in roots:
        try:
            probe = subprocess.run(
                ["git", "-C", root, "worktree", "list", "--porcelain"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            print(f"list-owned: worktree probe failed/timed out: {root}", file=sys.stderr)
            if root == anchor:
                anchor_failed = True
            continue
        if probe.returncode != 0:
            print(f"list-owned: not a git worktree: {root}", file=sys.stderr)
            if root == anchor:
                anchor_failed = True
            continue
        if os.path.abspath(root) == anchor:
            root_owned = _collect_owned_worktrees(args.session_id, root, args.pid)
        else:
            root_owned = _collect_owned_worktrees(
                args.session_id,
                root,
                args.pid,
                adopt_unmarked=False,
                adopt_dead_markered=True,
            )
        for path in root_owned:
            if path not in seen:
                seen.add(path)
                print(path)
    return 3 if anchor_failed else 0


def _complete_resolve_target_pr(args: argparse.Namespace, cwd: str):
    """Shared PR resolution for the --defer / --sweep-p2 verbs.

    Same force=True freshness contract as the normal complete flow (see
    _cmd_complete): these verbs are about to mutate GitHub state (reply +
    persist a deferral), so a stale cached snapshot is a correctness risk.
    """
    pr_number_arg = args.pr
    if pr_number_arg is not None:
        return _resolve_pr_by_number(int(pr_number_arg), cwd, force=True)
    return _resolve_pr_for_branch(cwd, force=True)


def _cmd_complete_defer(args: argparse.Namespace) -> int:
    """BOU-2567: defer ONE review thread — ``complete --defer <thread-id>``.

    Persists an individually evaluated P2 deferral and posts a reply on the
    thread naming the disposition. P1 findings cannot be deferred. A rationale
    is required; a pre-existing tracker ticket is optional. Deliberately never calls
    ``resolve_review_thread`` — resolving is exactly the wrong move (BOU-2567):
    it erases the deferral and looks identical to "actually fixed". The thread
    stays open on GitHub; every consumer excludes it via the persisted record.
    """
    from . import github_api  # noqa: PLC0415
    from .models import ReviewComment  # noqa: PLC0415

    cwd = os.path.abspath(args.cwd)
    pr = _complete_resolve_target_pr(args, cwd)
    if pr is _GH_UNAVAILABLE:
        print(_gh_unavailable_message(cwd))
        return 2
    if pr is None:
        print("no open PR for this branch")
        return 0

    thread_id = args.defer
    threads = github_api.get_review_threads(pr.number, cwd)
    thread = next((t for t in threads if t.node_id == thread_id), None)
    if thread is None:
        print(
            f"error: thread {thread_id!r} not found among PR #{pr.number}'s "
            "review threads (already resolved, or not on this PR?)",
            file=sys.stderr,
        )
        return 1

    supplied_severity = (args.severity or "").strip().upper()
    if _thread_is_p1(thread) or supplied_severity == "P1":
        print(
            f"error: thread {thread_id!r} is P1; P1 findings must be addressed "
            "before settlement and cannot be deferred",
            file=sys.stderr,
        )
        return 1
    if supplied_severity != "P2":
        print("error: --defer requires --severity P2", file=sys.stderr)
        return 1
    reason = (args.reason or "").strip()
    if not reason:
        print(
            "error: P2 deferral requires --reason with the evaluation rationale",
            file=sys.stderr,
        )
        return 1

    try:
        record = deferred_review.defer_thread(
            cwd, pr.number,
            thread_id=thread_id,
            comment_id=thread.top.database_id,
            severity=args.severity or "",
            ticket=args.ticket or "",
            reason=reason,
            deferred_by=args.session_id or "",
        )
    except deferred_review.DeferralError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    ticket_text = f" Tracked as {record.ticket}." if record.ticket else ""
    body = (
        f"Deferred ({record.severity}) after evaluation.{ticket_text}"
        + f" Reason: {record.reason}"
        + " This finding is real but does not warrant implementation in this PR; "
        "it is excluded "
        "from review/CI automation and will not be dispatched against, but "
        "stays open here for human visibility."
    )
    stub = ReviewComment(
        id=thread.top.database_id, author=thread.top.author, body=thread.top.body,
        path=thread.top.path, line=thread.top.line, created_at=thread.top.created_at,
        is_inline=True, thread_id=thread.node_id,
    )
    if not github_api.reply_to_review_comment(pr.number, stub, body, cwd):
        print(
            f"warning: deferral recorded but the GitHub reply failed for "
            f"thread {thread_id}; re-run --defer to retry the reply",
            file=sys.stderr,
        )

    ticket_suffix = f", tracked as {record.ticket}" if record.ticket else ""
    print(f"deferred thread {thread_id} as {record.severity}{ticket_suffix}")
    return 0


def _cmd_complete_sweep_p2(args: argparse.Namespace) -> int:
    """Refuse bulk P2 deferral; every P2 requires an individual evaluation."""

    print(
        "error: --sweep-p2 is disabled; evaluate each P2 individually with "
        "--defer THREAD --severity P2 --reason TEXT",
        file=sys.stderr,
    )
    return 1


def _cmd_complete(args: argparse.Namespace) -> int:
    if getattr(args, "defer", None):
        return _cmd_complete_defer(args)
    if getattr(args, "sweep_p2", False):
        return _cmd_complete_sweep_p2(args)

    from . import github_api, maintenance  # noqa: PLC0415
    from .github_api import COMPLETE_MARKER  # noqa: PLC0415
    from .models import ReviewComment  # noqa: PLC0415

    cwd = os.path.abspath(args.cwd)
    pr_number_arg = args.pr

    # force=True: this path is about to act on `pr` (resolve threads, reply,
    # post the completion marker) — bypass the shared snapshot cache
    # (BOU-1923 Bucket 2) so a stale merge/draft state can't misdirect a
    # mutation. The freshness cost here is one extra `gh` call per completion
    # run, not per Stop-hook/poll-tick, so it does not reintroduce the
    # fan-out the cache exists to collapse.
    if pr_number_arg is not None:
        pr = _resolve_pr_by_number(int(pr_number_arg), cwd, force=True)
    else:
        pr = _resolve_pr_for_branch(cwd, force=True)

    if pr is _GH_UNAVAILABLE:
        print(_gh_unavailable_message(cwd))
        return 2
    if pr is None:
        print("no open PR for this branch")
        return 0

    resolved_pr_number = pr.number
    head_sha = pr.latest_commit_sha
    head_date = pr.latest_commit_date
    api_head_sha = pr.latest_commit_sha

    local_head_sha, local_head_date = github_api.get_local_pr_head(pr.branch, cwd)
    local_is_fresh = bool(local_head_sha) and (
        not api_head_sha
        or github_api._is_ancestor(api_head_sha, local_head_sha, cwd)
    )
    if local_is_fresh:
        head_sha = local_head_sha
        if local_head_date:
            head_date = local_head_date

    baseline = args.baseline or ""
    new_commits = github_api.get_new_pr_commits(
        resolved_pr_number, baseline, head_sha, cwd, pr_branch=pr.branch,
        api_head_sha=api_head_sha)
    # BOU-2417: None means the commit range could not be determined (gh outage /
    # unusable payload), NOT "no commits landed since the baseline". Treating
    # the two alike is how an outage gets to look like a fix that never landed,
    # so every thread reads unaddressed and `complete` closes the bead on
    # evidence it never actually had. Fail closed: gather no file evidence, and
    # keep the bead open under the existing "unknown" blocker idiom below.
    commit_range_unknown = new_commits is None
    if commit_range_unknown:
        new_commits = []
        print(
            "warning: could not determine commits since baseline "
            f"{baseline or '(none)'} for PR #{resolved_pr_number}; treating the "
            "range as UNKNOWN rather than empty",
            file=sys.stderr,
        )
    touched: set[str] = set()
    commits_by_file: dict[str, list[tuple[str, str]]] = {}
    for sha, msg in new_commits:
        try:
            files = github_api.get_commit_changed_files(sha, cwd)
        except Exception:  # noqa: BLE001
            files = []
        for changed in files:
            touched.add(changed)
            commits_by_file.setdefault(changed, []).append((sha, msg))

    # BOU-2095: per-anchor-path base..head hunk spans, resolved lazily and
    # cached (several threads often share one file). ``None`` = diff
    # unavailable = no evidence.
    spans_by_path: dict[str, list[tuple[int, int, int, int]] | None] = {}

    def _spans_for(path: str) -> list[tuple[int, int, int, int]] | None:
        if path not in spans_by_path:
            spans_by_path[path] = github_api.get_changed_line_spans(
                baseline, head_sha, path, cwd)
        return spans_by_path[path]

    # BOU-2095 P1 (PR #78 review): every unresolved thread this run does NOT
    # resolve — including `is_outdated` ones that the downstream
    # blocker/pending filters would otherwise hide — must survive as a
    # `review_comments` blocker below, or `complete` closes the bead while
    # deliberately-left-open feedback still needs manual confirmation.
    left_unresolved: list[str] = []

    threads = github_api.get_review_threads(resolved_pr_number, cwd)
    deferred_count = 0
    for thread in threads:
        if thread.is_resolved:
            continue
        # BOU-2567: a deliberately-deferred thread is never auto-resolved by
        # `complete` — its disposition (ticket + reason) was already settled
        # at defer time, and re-running the evidence gate against it would
        # either duplicate that decision or (worse) silently resolve it,
        # erasing the deferral GitHub still shows as open. It also must not
        # count toward the `review_comments` blocker — the whole point of
        # deferral is that this PR does not stay open waiting on it.
        if deferred_review.is_thread_deferred(cwd, resolved_pr_number, thread.node_id):
            deferred_count += 1
            continue
        path = thread.top.path
        addressed = (
            bool(new_commits)
            and head_date > thread.top.created_at
            and (path is None or path in touched)
        )
        if not addressed:
            left_unresolved.append(thread.node_id)
            # BOU-2408: say WHY. This was the only one of the three
            # leave-open paths that stayed silent, so `complete` printed
            # "blockers remain: review_comments" with nothing on stderr and
            # read as a tool failure rather than a deliberate refusal. The
            # refusal is usually correct — commonly the fix landed in a test
            # file while the thread is anchored on the module under test — but
            # an agent cannot act on a reason it is never told.
            if not new_commits:
                reason = (
                    f"no new commits in {baseline or '<no baseline>'}..HEAD "
                    "(nothing was pushed after the baseline, so there is no "
                    "fix to evidence)"
                )
            elif head_date <= thread.top.created_at:
                reason = (
                    f"the thread ({thread.top.created_at}) is newer than the "
                    f"completing head ({head_date}) — it is feedback ON the fix, "
                    "not feedback the fix addresses"
                )
            else:
                reason = (
                    f"anchored file {path or '<none>'} was not touched by the "
                    f"fixing commits (they touched: "
                    f"{', '.join(sorted(touched)) or '<nothing>'})"
                )
            print(
                f"info: leaving thread {thread.node_id} open — {reason}; "
                "resolve it manually if the feedback is genuinely addressed",
                file=sys.stderr,
            )
            continue
        # BOU-2095: "anchor file touched" is necessary but NOT sufficient.
        # Require positive per-thread evidence — the completing commits changed
        # the thread's anchored hunk, or a completion reply already targets it.
        # GitHub's "outdated" flag (pure line drift) is explicitly NOT evidence:
        # it used to widen resolution here and silently closed live feedback.
        spans = _spans_for(path) if path is not None else None
        evidence = _thread_completion_evidence(thread, spans)
        if evidence is None:
            left_unresolved.append(thread.node_id)
            anchor_line = (
                thread.top.line if thread.top.line is not None
                else thread.top.original_line
            )
            print(
                f"info: leaving thread {thread.node_id} open — anchored file "
                f"{path or '<none>'} was touched by the fixing commits, but the "
                f"thread's anchored line "
                f"({anchor_line if anchor_line is not None else '<none>'}) was "
                f"not safely evidenced in {baseline or '<no baseline>'}..HEAD "
                f"(outdated={thread.is_outdated}); file-touch/line-drift alone "
                f"must not auto-resolve — needs manual confirmation; reviewer "
                f"feedback after a terminal completion marker is always treated "
                f"as reopened regardless of later hunk inference",
                file=sys.stderr,
            )
            continue
        elsewhere = _thread_elsewhere_refs(thread.top.body, path, touched)
        if elsewhere:
            left_unresolved.append(thread.node_id)
            # BOU-1641: a thread anchored on file A whose real ask is an
            # untouched file B must not auto-resolve just because A was
            # addressed. BOU-2095 removed the former "anchor touched AND
            # outdated" override (BOU-1748): outdated is line drift, not
            # evidence, and a cross-file ask stays ambiguous even when the
            # anchored hunk changed — leave it open for a human.
            print(
                f"info: leaving thread {thread.node_id} open — body references "
                f"{', '.join(elsewhere)} (not touched by the fixing commits); "
                f"a cross-file ask cannot be confirmed by anchor edits alone "
                f"(anchor={path or '<none>'}, outdated={thread.is_outdated}); "
                f"ambiguous resolution, needs manual confirmation",
                file=sys.stderr,
            )
            continue
        try:
            stub = ReviewComment(
                id=thread.top.database_id,
                author=thread.top.author,
                body=thread.top.body,
                path=path,
                line=thread.top.line,
                created_at=thread.top.created_at,
                is_inline=True,
                thread_id=thread.node_id,
            )
            # Reply first so the reviewer sees a response even when GitHub
            # rejects the subsequent resolve mutation. A prior terminal marker
            # is retry evidence, so do not post it again on the next run.
            if evidence != "reply":
                body = _completion_reply_body(
                    COMPLETE_MARKER, path, commits_by_file, new_commits)
                if not github_api.reply_to_review_comment(
                        resolved_pr_number, stub, body, cwd):
                    left_unresolved.append(thread.node_id)
                    print(
                        f"warning: could not reply to thread {thread.node_id}; "
                        "leaving open for retry",
                        file=sys.stderr,
                    )
                    continue
            if not github_api.resolve_review_thread(thread.node_id, cwd):
                left_unresolved.append(thread.node_id)
                print(
                    f"warning: could not resolve thread {thread.node_id}; leaving open for retry",
                    file=sys.stderr,
                )
                continue
        except Exception as exc:  # noqa: BLE001
            # Count any completion error as left open. A spurious blocker
            # self-heals on the next `complete` run; a silently-dropped thread
            # does not.
            left_unresolved.append(thread.node_id)
            print(f"warning: error completing thread {thread.node_id}: {exc}", file=sys.stderr)

    # force=True: re-resolve post-mutation state (threads were just
    # resolved/replied-to above) — a cached pre-mutation snapshot would report
    # stale "remaining blockers" (BOU-1923).
    fresh = _resolve_pr_by_number(resolved_pr_number, cwd, force=True)
    if fresh is _GH_UNAVAILABLE or fresh is None:
        remaining = ["unknown"]
    else:
        # BOU-2041: completion must clear the *terminal-clean* bar, not merely
        # the dispatch bar. `blockers_for_pr` answers "what can an executor fix
        # right now" and is silent on two states that still deny completion:
        # required CI that has not concluded, and a head that moved while this
        # run was gathering its evidence. Thread resolution above was evidenced
        # against `head_sha`; if the live head is no longer that sha, every
        # signal here reads clean only because it was measured against the
        # superseded head, so completion must re-evaluate rather than close.
        remaining = maintenance.terminal_clean_blockers(
            fresh, validated_head=head_sha)

    # BOU-2095 P1 (PR #78 review): `blockers_for_pr` sees only the comments the
    # scan path picked, and that path skips `is_outdated` threads — exactly the
    # ones the evidence gate above deliberately leaves open on pure line drift.
    # Without this, `complete` reports "no blockers remain" and closes the bead
    # while intentionally-kept-open feedback still needs manual confirmation.
    # BOU-2417: an indeterminate commit range means the addressed-evidence above
    # was gathered against nothing. Never close the bead on it.
    if commit_range_unknown and "unknown" not in remaining:
        remaining.append("unknown")
        print(
            "info: commit range since baseline was indeterminate; keeping the "
            "bead open under the 'unknown' blocker",
            file=sys.stderr,
        )

    if left_unresolved and "review_comments" not in remaining:
        remaining.append("review_comments")
        print(
            f"info: {len(left_unresolved)} review thread(s) remain unresolved "
            f"({', '.join(left_unresolved)}); keeping review_comments blocker",
            file=sys.stderr,
        )

    _mark_maintenance_complete(maintenance, cwd, resolved_pr_number)

    deferred_suffix = f"; deferred: {deferred_count}" if deferred_count else ""

    if remaining:
        print(
            f"completed (bead left open; blockers remain: "
            f"{', '.join(remaining)}{deferred_suffix})"
        )
        return 0

    branch = pr.branch
    if branch:
        try:
            from .config import load as _load_config  # noqa: PLC0415
            from .tracker import get_tracker  # noqa: PLC0415
            tracker = get_tracker(_load_config(cwd))
            task_id = tracker.find_task(pr=resolved_pr_number, branch=branch, cwd=cwd)
            if task_id:
                tracker.close_task(task_id, cwd=cwd)
        except Exception:  # noqa: BLE001
            pass

    print(f"completed (bead closed; no blockers remain{deferred_suffix})")
    return 0


def _cmd_stop_gate(args: argparse.Namespace) -> int:
    policy = getattr(args, "policy", None)
    ledger = getattr(args, "ledger", None)
    if ledger and not policy:
        print(
            "error: --ledger requires --policy",
            file=sys.stderr,
        )
        return 2
    try:
        legacy_result = _stop_gate_impl(args)
    except Exception as exc:  # noqa: BLE001
        # BOU-2556 (item 4): a genuine internal crash is fail-OPEN by design
        # (a buggy gate must not wedge every session that hits it) — but that
        # used to be completely SILENT, indistinguishable from a real clean
        # pass. `_stop_gate_impl` now carries its own wall-clock budget (see
        # STOP_GATE_BUDGET), so a Stop-hook wrapper's "subprocess timed out"
        # failure should no longer happen for a healthy owned set; if this
        # except DOES fire, it means the gate itself broke, not that it ran
        # out of time — say so explicitly rather than returning 0 with zero
        # information, so the operator isn't left guessing which of the two
        # actually happened.
        print(
            f"[pr-watch] stop-gate CRASHED internally ({type(exc).__name__}: {exc}) "
            f"— this is a bug in the gate itself, NOT a per-PR budget shortfall (a "
            f"budget shortfall blocks with its own distinct partial-completion "
            f"summary instead). Failing OPEN (exit 0) so a broken gate cannot wedge "
            f"every session, but this should be reported.",
            file=sys.stderr,
        )
        return 0
    if legacy_result != 0:
        return legacy_result
    if policy:
        # The hook protocol uses 2 for "block this stop"; finalize uses 10 for
        # ordinary work remaining. Both observation errors and unsettled work
        # must fail closed here.
        return 0 if _cmd_finalize(args) == 0 else 2
    return 0


def _cmd_hold_worktree(args: argparse.Namespace) -> int:
    """Claim exclusive WRITE access to a worktree (BOU-2590).

    Exit 0 when held, 3 when a LIVE actor already holds it (the caller stands
    down), 1 on error. 3 rather than 1 so "someone else is writing" — a normal,
    expected outcome — is distinguishable from a real failure.
    """
    from ._maintenance import mutation_lock  # noqa: PLC0415

    cwd = os.path.abspath(args.cwd)
    try:
        owner = mutation_lock.acquire(
            cwd,
            actor="session",
            session_id=getattr(args, "session_id", "") or "",
            pid=getattr(args, "pid", None),
        )
    except mutation_lock.WorktreeBusy as busy:
        print(str(busy), file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"could not take the worktree write lock: {exc}", file=sys.stderr)
        return 1
    print(
        f"holding write lock on {cwd} as {owner.describe()}; the maintenance "
        "loop's executor will stand down until it is released"
    )
    return 0


def _cmd_release_worktree(args: argparse.Namespace) -> int:
    """Release this worktree's write lock. Exit 0 whether or not one was held.

    Idempotent on purpose: a caller unwinding after a crash should not have to
    know whether it still holds the lock, and a failed release is not a reason
    to fail the caller's own cleanup path.
    """
    from ._maintenance import mutation_lock  # noqa: PLC0415

    cwd = os.path.abspath(args.cwd)
    owner = mutation_lock.read_owner(cwd)
    if owner is None:
        print(f"no write lock held on {cwd}")
        return 0
    if mutation_lock.release(cwd, owner):
        print(f"released write lock on {cwd}")
    else:
        print(f"write lock on {cwd} was already gone or is held by another token")
    return 0


def _cmd_reconcile_prs(args: argparse.Namespace) -> int:
    import json as _json  # noqa: PLC0415
    records = _owned_pr_records_all_roots(args.session_id, os.path.abspath(args.cwd),
                                          args.pid, adopt_orphans=args.adopt_orphans)
    for r in records:
        print(_json.dumps(r))
    return 0


def _await_anchors(session_id: str, cwd: str) -> list[str]:
    """Repo anchors this session's SINGLE waiter must poll (BOU-1924, PR #61 P1).

    The waiter pidfile is session-scoped, so ``_await_alive`` claims coverage for
    the session in EVERY repo — but the poll loop only spans ``_maint_roots_for``
    of one cwd. If the session owns a PR in a repo that is neither the launch
    cwd's repo nor in its ``maintenance_repo_roots``, that PR's feedback would be
    silently unwatched while its own stop-gate is suppressed. Make the single
    waiter honest by ALSO anchoring at every still-present worktree the session's
    ledger references, so its coverage matches its session-wide claim."""
    anchors = [cwd]
    seen = {os.path.abspath(cwd)}
    if not session_id:
        return anchors
    try:
        from agentic_pr_dash import session_ledger  # noqa: PLC0415
        for e in session_ledger.read(session_id):
            wt = os.path.abspath(e.worktree) if e.worktree else ""
            if wt and wt not in seen and os.path.isdir(wt):
                seen.add(wt)
                anchors.append(wt)
    except Exception:  # noqa: BLE001 - a bad ledger must not stop the waiter
        pass
    for requested in _read_requested_roots(session_id):
        if requested not in seen and os.path.isdir(requested):
            seen.add(requested)
            anchors.append(requested)
    return anchors


def _publishable_anchors(anchors: list[str], cwd: str, snap=None) -> list[str]:
    """Anchors minus the adopted worktrees this waiter will never service.

    ``_await_anchors`` is a POLL list: it deliberately spans every worktree the
    session ledger references so the single waiter visits every repo it owns a
    PR in. It is NOT a coverage list. Publishing it verbatim re-advertises the
    adopted paths that ``watched_owned`` was filtered to exclude — and since
    ``_update_await_coverage`` REPLACES ``covered_roots``, one unfiltered
    publication anywhere in the tick is enough for ``_await_alive`` to tell the
    machine-wide loop "a wake-capable waiter covers this", for a PR whose
    blockers this waiter intentionally ignores (BOU-2430). The loop then defers
    forever and nobody services it.

    ``cwd`` is kept unconditionally, even when it resolves as adopted: the
    waiter's own launch root is what the single-instance guard in
    ``_run_await_loop`` probes through ``_await_alive``, so dropping it would
    let a second poller start for the same session on every tick. The loop's
    per-PR probes pass the PR's own worktree path, not this cwd, so the
    adopted-deferral hole this closes stays closed for them.
    """
    from ._maintenance.ownership_resolution import (  # noqa: PLC0415
        resolve_worktree,
    )
    keep = os.path.abspath(cwd)
    out: list[str] = []
    for path in anchors:
        if os.path.abspath(path) != keep:
            try:
                provenance = resolve_worktree(
                    path, kind="await_adopted_scope", snap=snap
                ).provenance
            except Exception:  # noqa: BLE001 - never let coverage crash the waiter
                provenance = None
            if provenance == "adopted":
                continue
        out.append(path)
    return out


# Exit code -> (machine-readable outcome, human reason) (BOU-1877). An exit code
# alone can't distinguish "another waiter/loop is actively covering this session"
# (3) from "idle: nothing pending, nothing to watch" (0) — so supervising
# sessions parse the outcome. These are DISTINCT outcomes: `deferred_to_loop`
# means real coverage handed off; `idle` means no coverage was needed (#62).
#
# 4 (`unbound`) is the loud version of "we watched nothing" (BOU-2294). An empty
# watch set used to exit 0 and read as a verdict — the waiter reported the PRs it
# owns as clean while it had in fact bound to no PR at all, so a check that went
# red afterwards woke nobody. A wrong answer that looks right is worse than no
# answer, so this exits NON-ZERO and says it reached no verdict.
#
# 4 was chosen against the callers rather than picked freely: 10 is the harness's
# wake signal, 0 means clean/idle, 3 means deferred, and 1 is the crash path in
# `_cmd_await`. Nothing keys coverage off the exit code — the stop gate's
# coverage logic and `run_post_push_watch` both go through `_await_alive`'s
# pidfile — so a new non-zero code cannot silence a wake or fake coverage.
_AWAIT_UNBOUND = 4

_AWAIT_OUTCOMES: dict[int, tuple[str, str]] = {
    10: ("woke", "feedback arrived on an owned PR"),
    3: ("deferred_to_loop", "another waiter already covers this session"),
    _AWAIT_UNBOUND: ("unbound", "watched nothing — no verdict on any PR"),
    0: ("idle", "no pending feedback and nothing to watch (idle / max-wait elapsed)"),
}


def _emit_await_outcome(outcome: str, *, pr: int | None = None, reason: str = "") -> None:
    """Emit exactly one machine-readable JSON outcome line to stderr (BOU-1877).

    ``outcome`` is one of ``woke`` | ``deferred_to_loop`` | ``idle`` | ``error``.
    Written to stderr (not stdout) so the exit-10 stop-block on stdout stays clean
    for the harness. Best-effort — never raises out of a process already exiting.
    """
    try:
        line = json.dumps(
            {"outcome": outcome, "pr": pr, "reason": reason},
            separators=(",", ":"),
        )
        print(line, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — outcome reporting must never mask the real exit
        pass


def _unbound_exit(session_id: str, why: str) -> int:
    """Leave the waiter with NO verdict, loudly (BOU-2294).

    Also drops any clean-exit marker this session left behind: that marker is what
    tells the stop gate "a waiter already verified these PRs, don't demand another
    one". Keeping it while we cannot even resolve what we own would let a stale
    verdict keep suppressing coverage.
    """
    _clear_clean_exit_marker(session_id)
    print(
        f"[pr-watch] waiter watched NOTHING ({why}) — exiting with NO verdict. "
        f"This is not a clean bill of health: no PR was observed, so nothing here "
        f"will wake the session.",
        file=sys.stderr,
    )
    return _AWAIT_UNBOUND


def _cmd_await(args: argparse.Namespace) -> int:
    """Background feedback waiter — structured-outcome wrapper (BOU-1877).

    Runs the poll loop and emits exactly one outcome line on every exit path,
    including an unexpected crash (e.g. a deep helper hitting a deleted cwd,
    BOU-1905): the waiter must never die with a bare traceback that a supervisor
    cannot tell apart from a clean deferral.
    """
    try:
        code = _run_await_loop(args)
    except Exception as exc:  # noqa: BLE001 — KeyboardInterrupt/SystemExit still propagate
        _emit_await_outcome("error", reason=f"{type(exc).__name__}: {exc}")
        return 1
    outcome, reason = _AWAIT_OUTCOMES.get(code, ("idle", f"exit {code}"))
    _emit_await_outcome(outcome, reason=reason)
    return code


def _run_await_loop(args: argparse.Namespace) -> int:
    """Background feedback waiter — poll owned PRs, exit 10 when work arrives."""
    cwd = os.path.abspath(args.cwd)
    session_id = args.session_id or _read_session_marker(cwd)
    owner_pid = args.owner_pid if args.owner_pid else _resolve_owner_pid()

    # Single-instance guard — dual-read (new session-scoped + legacy per-worktree)
    # so an in-flight pre-BOU-1924 waiter for this session isn't missed, which
    # would launch a SECOND poller for the same session (PR #61 review, P2).
    from ._maintenance.waiter import _await_alive  # noqa: PLC0415
    if _await_alive(cwd, session_id):
        print("[pr-watch] waiter already running for this session", file=sys.stderr)
        return 3

    _update_await_coverage(
        cwd,
        session_id,
        _publishable_anchors(_await_anchors(session_id, cwd), cwd),
    )
    now = time.time()
    if args.max_wait == 0:
        deadline: float | None = now
    elif args.max_wait > 0:
        deadline = now + args.max_wait
    else:
        deadline = None
    from agentic_pr_dash import github_api  # noqa: PLC0415
    try:
        while True:
            if not _pid_alive(str(owner_pid)):
                return 0

            # Reset the per-tick rate-limit observation BEFORE any gh call this
            # tick. rate_limit_seen() below then reflects a quota wall hit by ANY
            # call this tick (list / ci / review-threads / watch-pending), and is
            # cleared each tick so a stale earlier rate-limit can't wedge the
            # waiter alive on a later observable tick (BOU-1921 #62).
            github_api.reset_rate_limit_seen()
            # Same per-tick reset for the CI-watch probe failure flag: the
            # clean exit below requires a POSITIVE terminal-CI observation
            # this tick (codex PR #75 review).
            github_api.reset_checks_probe_failure_seen()

            # Span every repo anchor the session owns PRs in (not just cwd's
            # maintenance roots), so the single session-scoped waiter honestly
            # covers all owned PRs (PR #61 review, P1). Dedup owned worktrees and
            # detached records across anchors.
            anchors = _await_anchors(session_id, cwd)
            # ONE ownership snapshot per tick, shared by the anchor filter and
            # the per-worktree provenance loop below — resolving per worktree is
            # a full store read each time and the Stop hook's ~108s budget is
            # fail-closed.
            from agentic_pr_dash import ownership as _ownership_mod  # noqa: PLC0415
            _await_snap = _ownership_mod.snapshot()
            covered_anchors = _publishable_anchors(anchors, cwd, _await_snap)
            _update_await_coverage(cwd, session_id, covered_anchors)
            owned: list[str] = []
            if session_id:
                _seen_wt: set[str] = set()
                for anchor in anchors:
                    for wt in _owned_worktrees_across_roots(session_id, anchor):
                        if wt not in _seen_wt:
                            _seen_wt.add(wt)
                            owned.append(wt)
            else:
                owned = [cwd]

            pending: list[tuple[str, str]] = []
            adopted_worktrees: set[str] = set()
            gh_unobservable = False
            warn_only_deferral = False
            # BOU-2430: an ADOPTED worktree is one auto-adoption handed this
            # session — it never armed the PR and the maintenance loop, not
            # this session, is responsible for it. The stop gate already
            # excludes adopted worktrees from its blocking set and tells the
            # operator so ("NOT blocking your stop"); the waiter it then
            # prescribes must honor the SAME exclusion, or the gate's own
            # message is unsatisfiable — it demands a waiter whose very first
            # tick reports "feedback arrived, address it now" on the PR it
            # just called non-blocking. One scope, both places.
            from ._maintenance.ownership_resolution import (  # noqa: PLC0415
                resolve_worktree as _resolve_worktree_ownership,
            )
            for worktree in owned:
                # Provenance is resolved for EVERY owned worktree, not just the
                # code==10 ones. Deciding it inside a single branch left adopted
                # worktrees in the waiter's effective scope on every other
                # outcome: a code-2 adopted worktree would raise the global
                # `gh_unobservable` flag and block a clean exit for unrelated
                # armed PRs, and a code-0 adopted PR with running CI would stay
                # in `watched_owned` and keep the waiter alive indefinitely —
                # for work the maintenance loop, not this session, owns.
                provenance = _resolve_worktree_ownership(
                    worktree, kind="await_adopted_scope", snap=_await_snap
                ).provenance
                is_adopted = provenance == "adopted"
                if is_adopted:
                    adopted_worktrees.add(worktree)
                code, text = _check_worktree(worktree, session_id, claim=False)
                if code == 10:
                    if is_adopted:
                        # Reported via the same adopted-work FYI as the stop
                        # gate, never as blocking feedback for THIS waiter.
                        print(
                            f"[pr-watch] FYI: adopted worktree {worktree} has "
                            "pending work — auto-adopted, not armed by this "
                            "session, so the maintenance loop owns it and it "
                            "does not wake this waiter.",
                            file=sys.stderr,
                        )
                    else:
                        pending.append((worktree, text))
                elif code == 2 and not is_adopted:
                    # gh could not resolve this worktree's PR this tick — its
                    # state is UNOBSERVABLE, so this tick must not conclude
                    # "clean" (suppresses the BOU-1962 early exit below).
                    gh_unobservable = True
                elif code == 0 and not is_adopted and text.rstrip().endswith(
                    WARN_ONLY_MARKER
                ):
                    # Warn-only deferral: the PR HAS blockers but a live foreign
                    # owner / active coordinator claim is responsible, so no fix
                    # is dispatched here. The PR is explicitly NOT clean — this
                    # tick must not clean-exit on it (codex PR #75 review).
                    warn_only_deferral = True
                if not is_adopted:
                    _touch_owner_heartbeat(worktree, session_id, code == 10)

            # Publish only the scope this waiter will actually service. If an
            # adopted worktree is advertised here, the machine-wide loop sees
            # this live waiter as wake-capable coverage and defers forever even
            # though the waiter intentionally ignores that worktree's blockers.
            watched_owned = [wt for wt in owned if wt not in adopted_worktrees]
            _update_await_coverage(
                cwd, session_id, [*covered_anchors, *watched_owned]
            )

            _detached_this_tick: list[dict] = []
            if session_id:
                _seen_d: set[tuple] = set()
                for anchor in anchors:
                    for r in _detached_records_across_roots(session_id, anchor):
                        key = (r.get("repo", ""), r["pr"], r.get("branch", ""))
                        if key in _seen_d:
                            continue
                        _seen_d.add(key)
                        _detached_this_tick.append(r)
                for r in _detached_this_tick:
                    # Waiter: an unresolvable gh probe is NOT actionable
                    # feedback — it must not exit 10 ("Feedback arrived") on a
                    # transient outage. `unknown_detached` below already keeps
                    # this tick from concluding "clean" for the same record;
                    # this just stops it from being wrongly treated as
                    # CONFIRMED feedback (PR #119 review).
                    if _record_has_blockers(r, unknown_state_blocks=False):
                        pending.append(_detached_pending_entry(r))

            if pending:
                # Feedback arrived — any earlier clean-exit verdict is stale.
                _clear_clean_exit_marker(session_id)
                print(_build_stop_block(pending))
                print(
                    "[pr-watch] Feedback arrived on PR(s) you own — address it now "
                    "(commit + push to the EXISTING branch, then run the per-worktree "
                    "complete command above)."
                )
                return 10

            # "Is there anything at all for this tick to watch" is a THIRD
            # question, distinct from both "is it actionable feedback" (the
            # `pending` check above) and "is it confirmed clean" (the
            # verified-set filter below). `_detached_pr_records` already prunes
            # merged/closed/draft records before they ever reach
            # `_detached_this_tick`, so every survivor is either a confirmed
            # "open" PR or one whose `gh` probe returned "unknown" this tick —
            # BOTH represent real, still-tracked work the session owns, so
            # BOTH must count as "something to watch" here. Excluding
            # "unknown" from this specific check (as a marker record it once
            # shared with the "confirmed clean" question) let a session that
            # owns ONLY a detached, currently-unresolvable PR fall through to
            # `_unbound_exit` before ever reaching the `unknown_detached` guard
            # a few lines down that exists specifically to keep the waiter
            # alive through exactly this case (P1, PR #121 review — the same
            # "unknown collapsed into a falsy default" defect this whole
            # branch is about, this time on the "nothing owned" exit instead
            # of the "feedback" or "clean" ones).
            has_any_detached = bool(_detached_this_tick)
            if (
                not owned
                and not has_any_detached
                and not github_api.rate_limit_seen()
                and not getattr(args, "keep_alive_without_prs", False)
            ):
                # Nothing resolved to watch. That is NOT "everything is clean" —
                # ownership resolution may simply have come up empty for a session
                # that does own an open PR (BOU-2294). Say so loudly instead of
                # exiting 0 next to a clean-looking message.
                return _unbound_exit(session_id, "no owned worktree and no "
                                     "detached PR resolved for this session")

            # BOU-1962: clean-state early exit. `pending` is empty here (the
            # return-10 above fired otherwise), so no owned or detached PR has
            # an actionable blocker. If required CI is also TERMINAL (not
            # watch-pending — the same probe the deadline path uses, so the
            # BOU-1789 stay-alive-while-CI-runs behavior is preserved) and PR
            # state was actually observable this tick (no gh outage, no rate
            # limit), the watched PRs are CLEAN: exit 0 now instead of sleeping
            # until the deadline — or forever with `--max-wait < 0`. The
            # detached pr-maintenance-loop remains the durable backstop.
            # A detached record whose state read failed ("unknown", a
            # non-rate-limit gh error — the rate-limit case is caught by
            # rate_limit_seen()) is an UNOBSERVED possibly-open PR: its
            # review/merge/CI state was never seen this tick, so the tick must
            # not conclude "clean" (codex PR #75 review, round 5).
            unknown_detached = any(
                r.get("state") == "unknown" for r in _detached_this_tick
            )

            watch_pending: bool | None = None
            if not getattr(args, "keep_alive_without_prs", False):
                watch_pending = _await_watch_pending_this_tick(
                    watched_owned, _detached_this_tick, cwd, session_id
                )
                if (
                    not watch_pending
                    and not gh_unobservable
                    and not unknown_detached
                    and not warn_only_deferral
                    and not github_api.rate_limit_seen()
                    # "CI terminal" must be a POSITIVE observation: a failed
                    # watch-pending probe returns False fail-safe, which must
                    # not read as terminal (codex PR #75 review).
                    and not github_api.checks_probe_failure_seen()
                ):
                    # Record WHICH open PRs were verified clean so the stop
                    # gate doesn't re-demand a waiter that would immediately
                    # clean-exit again (codex PR #75 review). Keys are
                    # repo-qualified so the same PR number in two maintenance
                    # repos stays distinct (round 3). Only PRs whose worktree
                    # still RESOLVES to them were actually observed by this
                    # tick's checks — a stale marker PR must not be recorded
                    # verified (round 5).
                    verified = {
                        _clean_exit_key(_repo_slug(wt), n)
                        for wt, n in _owned_pr_pairs_for_await(watched_owned)
                        if _marker_pr_still_current(wt, n)
                    } | {
                        _clean_exit_key(r.get("repo", ""), r["pr"])
                        for r in _detached_this_tick
                        if r.get("state") not in ("merged", "closed", "draft", "unknown")
                    }
                    if not verified:
                        # Owned worktrees exist but none of them resolved to an
                        # open PR this tick, so "clean" would be a verdict on the
                        # empty set — the exact false green BOU-2294 is about.
                        # Refuse to record or announce one.
                        return _unbound_exit(
                            session_id,
                            "no owned worktree resolved to an open PR",
                        )
                    _write_clean_exit_marker(session_id, verified)
                    print(
                        "[pr-watch] all watched PRs are clean (no pending "
                        "feedback, required CI terminal); waiter exiting."
                    )
                    return 0

            if deadline is not None and time.time() >= deadline:
                # Reuse this tick's watch-pending probe when the clean-exit
                # path already ran it (identical inputs within the tick);
                # compute it here otherwise (keep_alive_without_prs sessions).
                if watch_pending is None:
                    watch_pending = _await_watch_pending_this_tick(
                        watched_owned, _detached_this_tick, cwd, session_id
                    )
                # Read the flag AFTER the watch-pending probe so a quota wall hit
                # by THAT call is captured too (a PR with running CI must not lose
                # its waiter on a rate-limited tick) (BOU-1921 #62).
                if github_api.rate_limit_seen():
                    # Rate-limited: we could not observe PR state — do not
                    # conclude "no feedback". Stay alive and keep polling; gh
                    # backs off on rate-limit (github_api._run) and typically
                    # recovers within a tick or two (BOU-1921).
                    print(
                        "[pr-watch] waiter max-wait reached but GitHub was "
                        "rate-limited this tick — staying alive until PR state "
                        "can be observed.",
                        file=sys.stderr,
                    )
                elif not watch_pending:
                    print(
                        "[pr-watch] waiter max-wait reached with no feedback; "
                        "will re-arm on next stop."
                    )
                    return 0
                else:
                    print(
                        "[pr-watch] waiter max-wait reached but required CI is still "
                        "running — staying alive until CI completes or fails.",
                        file=sys.stderr,
                    )

            time.sleep(max(args.interval, 1))
    finally:
        _remove_await_pidfile(cwd, session_id)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentic-pr-dash",
        description="PR maintenance check — stateless read-only check and completion.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- check ---
    check_p = subparsers.add_parser(
        "check",
        help="Resolve branch→PR, compute blockers, print prompt. Read-only (no writes).",
    )
    check_p.add_argument(
        "--cwd", default=".", help="Worktree root (default: current directory)."
    )
    check_p.add_argument(
        "--session-id",
        default="",
        metavar="ID",
        help="Caller's session id (for the ownership gate). check DEFERS (exit 0) "
        "when the worktree's pr-watch.armed marker names a live session OTHER "
        "than this one — so a detached loop never fights a live in-session owner. "
        "Pass your own id to exclude yourself; omit to defer to any live owner.",
    )

    # --- finalize ---
    finalize_p = subparsers.add_parser(
        "finalize",
        help="Require provider-neutral review settlement and stable green PR/CI state.",
    )
    finalize_p.add_argument(
        "--cwd",
        default=".",
        help="Worktree root (default: current directory).",
    )
    finalize_p.add_argument(
        "--pr",
        type=int,
        default=None,
        metavar="N",
        help="PR number (default: resolve from current branch).",
    )
    finalize_p.add_argument(
        "--session-id",
        default="",
        metavar="ID",
        help="Caller session id, accepted for hook/agent command parity.",
    )
    finalize_p.add_argument(
        "--policy",
        required=True,
        metavar="PATH",
        help="Provider-neutral review policy YAML.",
    )
    finalize_p.add_argument(
        "--ledger",
        default=None,
        metavar="PATH",
        help="Coordinator review ledger JSON (default: "
        ".agentic-pr-dash/review-ledger.json below --cwd).",
    )
    finalize_p.add_argument(
        "--stabilization-seconds",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="Delay between the two required clean observations (default: 30).",
    )
    finalize_p.add_argument(
        "--json",
        action="store_true",
        help="Print the machine-readable finalization report.",
    )

    # --- complete ---
    complete_p = subparsers.add_parser(
        "complete",
        help="Re-fetch unresolved review threads from GitHub and resolve them statelessly.",
    )
    complete_p.add_argument(
        "--cwd", default=".", help="Worktree root (default: current directory)."
    )
    complete_p.add_argument(
        "--pr",
        type=int,
        default=None,
        metavar="N",
        help="PR number (default: resolve from current branch).",
    )
    complete_p.add_argument(
        "--baseline",
        default="",
        metavar="SHA",
        help="PR head SHA captured BEFORE the agent ran; only commits after it "
        "count as the fix when deciding which review threads to resolve.",
    )
    complete_p.add_argument(
        "--defer",
        default=None,
        metavar="THREAD_ID",
        help="BOU-2567: defer ONE review thread (its GraphQL node id, e.g. "
        "PRRT_...) instead of running the normal complete flow. Persists a "
        "deferred-state record keyed by thread id and posts a reply naming the "
        "evaluation reason. The thread stays UNRESOLVED on GitHub (deferral is "
        "the opposite of erasing the finding) but is excluded from every "
        "consumer's blocker computation from then on. Requires --severity P2 "
        "and --reason; --ticket is optional. P1 cannot be deferred.",
    )
    complete_p.add_argument(
        "--sweep-p2",
        action="store_true",
        default=False,
        help="Deprecated and disabled: every P2 requires individual evaluation.",
    )
    complete_p.add_argument(
        "--severity",
        default=None,
        choices=["P1", "P2"],
        help="Severity of the thread being deferred with --defer.",
    )
    complete_p.add_argument(
        "--ticket",
        default=None,
        metavar="BOU-N",
        help="Optional existing follow-up ticket for --defer. This command never "
        "creates tracker work.",
    )
    complete_p.add_argument(
        "--reason",
        default="",
        metavar="TEXT",
        help="Required P2 evaluation rationale. P1 findings cannot be deferred.",
    )
    complete_p.add_argument(
        "--session-id",
        default="",
        metavar="ID",
        help="Caller's session id, recorded as deferred_by on --defer/--sweep-p2.",
    )

    # --- list-owned ---
    list_owned_p = subparsers.add_parser(
        "list-owned",
        help="Print worktree paths whose pr-watch.armed marker matches --session-id.",
    )
    list_owned_p.add_argument(
        "--session-id",
        required=True,
        metavar="ID",
        help="Owning session id. Only worktrees whose marker carries a matching "
        "session_id= line are printed (one path per line).",
    )
    list_owned_p.add_argument(
        "--cwd",
        default=".",
        help="Directory to run `git worktree list` from (default: current directory).",
    )
    list_owned_p.add_argument(
        "--pid",
        type=int,
        default=None,
        metavar="PID",
        help="Owner pid stamped into markers ADOPTED by reconciliation (default: "
        "os.getppid() — the Claude session that ran this CLI via a shell). The "
        "pid lets the detached loop / other sessions defer while this owner is alive.",
    )

    # --- arm ---
    arm_p = subparsers.add_parser(
        "arm",
        help="Stamp a worktree's open non-draft PR with a pr-watch.armed marker for a session.",
    )
    arm_p.add_argument(
        "--cwd", default=".", help="Worktree root to arm (default: current directory)."
    )
    arm_p.add_argument(
        "--session-id",
        required=True,
        metavar="ID",
        help="Owning session id stamped into the marker (the parent orchestrator's id).",
    )
    arm_p.add_argument(
        "--pid",
        type=int,
        default=None,
        metavar="PID",
        help="Owner pid stamped into the marker (default: os.getppid()).",
    )
    arm_p.add_argument(
        "--pr",
        type=int,
        default=None,
        metavar="N",
        help="PR number to arm. When omitted, resolves the worktree branch's open "
        "@me PR via gh and refuses to arm a draft or a branch with no open PR.",
    )
    arm_p.add_argument(
        "--branch",
        default=None,
        metavar="BRANCH",
        help="Head branch whose open PR to arm (e.g. `gh pr create --head <branch>` "
        "from a sibling worktree). Ignored when --pr is given; when both are "
        "omitted the worktree's current branch is resolved.",
    )

    # --- stop-gate ---
    stop_gate_p = subparsers.add_parser(
        "stop-gate",
        help="Stop-hook gate: block (exit 2 + stderr prompt) when owned PRs have "
        "pending review/CI work, else exit 0. Time-rate-limited + loop-broken.",
    )
    stop_gate_p.add_argument(
        "--cwd", default=".", help="Worktree root (default: current directory)."
    )
    stop_gate_p.add_argument(
        "--session-id",
        default="",
        metavar="ID",
        help="Owning session id. Scopes the check to this session's worktrees; "
        "falls back to pr-watch.session, then to the cwd only.",
    )
    stop_gate_p.add_argument(
        "--pid",
        type=int,
        default=None,
        metavar="PID",
        help="Owner pid for marker adoption (default: os.getppid()).",
    )
    stop_gate_p.add_argument(
        "--no-waiter",
        action="store_true",
        default=False,
        help="Skip the waiter-enforcement branch (for codex/non-interactive callers "
        "that have no background-task wake channel).",
    )
    stop_gate_p.add_argument(
        "--pr",
        type=int,
        default=None,
        metavar="N",
        help="PR number for canonical finalization (default: current branch).",
    )
    stop_gate_p.add_argument(
        "--policy",
        default=None,
        metavar="PATH",
        help="Review policy YAML; requires --ledger and enables canonical finalization.",
    )
    stop_gate_p.add_argument(
        "--ledger",
        default=None,
        metavar="PATH",
        help="Coordinator review ledger JSON; requires --policy.",
    )
    stop_gate_p.add_argument(
        "--stabilization-seconds",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="Canonical finalization delay between clean observations.",
    )
    stop_gate_p.add_argument(
        "--json",
        action="store_true",
        help="Print canonical finalization as machine-readable JSON.",
    )

    # --- reconcile-prs ---
    reconcile_p = subparsers.add_parser(
        "reconcile-prs",
        help="List every PR this session owns — live-worktree AND detached (ledger) "
             "PRs whose worktree was removed — with live review-thread/CI state, "
             "severity-first. Prunes merged/closed PRs (BOU-1587).",
    )
    reconcile_p.add_argument("--session-id", required=True, metavar="ID")
    reconcile_p.add_argument("--cwd", default=".")
    reconcile_p.add_argument("--pid", type=int, default=None, metavar="PID")
    reconcile_p.add_argument("--adopt-orphans", action="store_true",
                             help="Also claim PRs orphaned by DEAD sessions (Component G).")

    # --- hold-worktree / release-worktree (BOU-2590) ---
    hold_p = subparsers.add_parser(
        "hold-worktree",
        help="Claim exclusive WRITE access to a worktree so the maintenance "
             "loop's executor stands down instead of editing underneath you.",
    )
    hold_p.add_argument("--cwd", default=".")
    hold_p.add_argument("--session-id", default="", metavar="ID")
    hold_p.add_argument(
        "--pid",
        type=int,
        default=None,
        metavar="PID",
        help="Holder pid whose liveness keeps the lock (default: this process). "
             "Pass the SESSION's pid — a lock held by an exited helper is stale "
             "the moment it returns.",
    )

    release_p = subparsers.add_parser(
        "release-worktree",
        help="Release this worktree's write lock (only the current holder can).",
    )
    release_p.add_argument("--cwd", default=".")

    # --- await ---
    await_p = subparsers.add_parser(
        "await",
        help="Background feedback waiter: poll owned PRs and exit 10 when work arrives.",
    )
    await_p.add_argument(
        "--cwd", default=".", help="Launch cwd — state dir is resolved from here."
    )
    await_p.add_argument(
        "--session-id",
        default="",
        metavar="ID",
        help="Owning session id (falls back to pr-watch.session).",
    )
    await_p.add_argument(
        "--owner-pid",
        type=int,
        default=0,
        metavar="PID",
        help="Owning session pid; waiter exits 0 when it dies (default: "
        "walk ancestors for nearest claude/codex process).",
    )
    await_p.add_argument(
        "--interval",
        type=int,
        default=150,
        metavar="SECONDS",
        help="Poll interval in seconds (default: 150).",
    )
    await_p.add_argument(
        "--max-wait",
        type=int,
        default=21600,
        metavar="SECONDS",
        help="Maximum total wait seconds; 0 = one tick then exit (default: 21600).",
    )
    await_p.add_argument(
        "--keep-alive-without-prs",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    await_p.add_argument(
        "--pr",
        type=int,
        default=None,
        metavar="N",
        help="Accepted for symmetry with `arm --pr N` and ignored: `await` "
        "always resolves the PR(s) it watches from the state dir at --cwd, "
        "never from a single --pr value (BOU-2538).",
    )

    args = parser.parse_args(argv)

    if args.command == "check":
        return _cmd_check(args)
    if args.command == "finalize":
        return _cmd_finalize(args)
    if args.command == "complete":
        return _cmd_complete(args)
    if args.command == "hold-worktree":
        return _cmd_hold_worktree(args)
    if args.command == "release-worktree":
        return _cmd_release_worktree(args)
    if args.command == "list-owned":
        return _cmd_list_owned(args)
    if args.command == "arm":
        return _cmd_arm(args)
    if args.command == "stop-gate":
        return _cmd_stop_gate(args)
    if args.command == "reconcile-prs":
        return _cmd_reconcile_prs(args)
    if args.command == "await":
        return _cmd_await(args)
    # Unreachable (subparsers required=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
