"""Stop-gate state helpers."""
from __future__ import annotations

import hashlib
import json
import os
import time as _time

from agentic_pr_dash.config import load as load_config
from ._common import _env_int
from .markers import _read_marker, _prune_stale_marker, _read_session_marker


def _stop_state_path(cwd: str) -> str:
    return str(load_config(cwd).state_dir_for(cwd) / "pr-watch.stop-loop.json")


def _load_stop_state(cwd: str) -> dict:
    try:
        with open(_stop_state_path(cwd), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_stop_state(cwd: str, state: dict) -> None:
    try:
        path = _stop_state_path(cwd)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError:
        pass


def _stop_fingerprint(pending: list[tuple[str, str]]) -> str:
    """Stable hash of the pending (worktree, prompt) set."""
    h = hashlib.sha256()
    for path, text in sorted(pending):
        h.update(path.encode("utf-8"))
        h.update(b"\0")
        h.update(text.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _extract_pr_number(text: str) -> str:
    """Pull the trailing PR_NUMBER=<n> the check appends, or '' if absent."""
    for line in reversed(text.splitlines()):
        if line.startswith("PR_NUMBER="):
            return line[len("PR_NUMBER="):].strip()
    return ""


def _extract_summary(text: str) -> str:
    """Pull the trailing SUMMARY=<...> the check/detached-entry appends, or '' if absent."""
    for line in reversed(text.splitlines()):
        if line.startswith("SUMMARY="):
            return line[len("SUMMARY="):].strip()
    return ""


def _write_stop_payload(cwd: str, content: str) -> str | None:
    """Write the full verbose stop-block to the payload file (BOU-1947).

    Returns the absolute path as a string, or None if the write failed.
    """
    try:
        path = load_config(cwd).state_dir_for(cwd) / "pr-watch.stop-payload.md"
        os.makedirs(path.parent, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return str(path)
    except OSError:
        return None


def _build_stop_block(pending: list[tuple[str, str]]) -> str:
    lines = [
        "[pr-watch] Open PR(s) you own have pending review/CI work. Address it "
        "before stopping — commit and push to the EXISTING branch (do not open a "
        "new PR), then re-stop:\n"
    ]
    for path, text in pending:
        lines.append(f"───── worktree: {path} ─────")
        lines.append(text)
        if path.startswith("(no worktree)"):
            lines.append("")
            continue
        pr_ref = _extract_pr_number(text) or "<N>"
        lines.append(
            f"FIRST, before changing anything, capture this worktree's pre-fix "
            f"baseline head: `gh pr view --json headRefOid -q .headRefOid` (run "
            f"from {path}). THEN fix + commit + push, and finally:\n"
            f"  agentic-pr-dash complete "
            f"--pr {pr_ref} --baseline <that-pre-fix-sha> --cwd {path}\n"
            f"(Passing the post-fix head as --baseline leaves threads unresolved.)"
        )
        lines.append("")
    return "\n".join(lines)


def _build_concise_stop_block(pending: list[tuple[str, str]], payload_path: str) -> str:
    """Render the concise stderr block: one summary line per pending entry plus
    a pointer to the full verbose payload file (BOU-1947)."""
    lines = [
        f"[pr-watch] Open PR(s) you own have pending review/CI work. Full detail "
        f"written to {payload_path}.\n"
    ]
    for _path, text in pending:
        lines.append(f"  - {_extract_summary(text) or text.splitlines()[0]}")
    lines.append("")
    lines.append(
        f"Full instructions (comment bodies, baseline capture, per-PR complete "
        f"commands): read {payload_path}"
    )
    return "\n".join(lines)


def _owned_open_pr_numbers(owned: list[str]) -> set[int]:
    """Collect PR numbers from the armed markers of owned worktrees."""
    return {pr for _wt, pr in _owned_open_pr_pairs(owned)}


def _owned_open_pr_pairs(owned: list[str]) -> list[tuple[str, int]]:
    """``(worktree, pr)`` pairs from the armed markers of owned worktrees.

    The worktree path is preserved so per-PR, repo-scoped lookups
    (``loop._loop_covers_pr`` / ``_read_escalation_marker``) hit the PR's OWN
    repo's health/escalation file — owned worktrees can span several repos
    (``maintenance_repo_roots``), and the repo-scoped state files would
    otherwise be read against the stop-gate anchor's repo (codex PR #50 review).
    """
    pairs: list[tuple[str, int]] = []
    for wt in owned:
        marker = _read_marker(wt) or {}
        pr_raw = marker.get("pr", "")
        if str(pr_raw).isdigit():
            pairs.append((wt, int(pr_raw)))
    return pairs


def _build_waiter_block(open_prs: set[int], cwd: str, session_id: str) -> str:
    """Render the spawn-waiter prompt."""
    pr_list = ", ".join(f"#{n}" for n in sorted(open_prs))
    cmd = load_config(cwd).await_command.format(cwd=cwd, session_id=session_id)
    return (
        f"[pr-watch] You own open PR(s) {pr_list} with no pending feedback right now. "
        f"Start the feedback waiter IN THE BACKGROUND (run_in_background / detached) so "
        f"arriving review comments wake this session, then stop again:\n"
        f"  {cmd}"
    )


# Explicit capability sentinel for cross-repo probes (gaia PR #2337 review).
# True iff ``_stop_gate_impl`` below runs list-owned reconciliation/adoption
# BEFORE its clean-stop rate-limit early-return (so a missed-arm or stale-marker
# PR is inspected on the same tick). A consumer (e.g. gaia's Stop-hook wrapper)
# checks THIS flag — not merely the presence of a helper like
# ``worktrees._reconcile_owned_across_roots`` — to decide whether the package
# already subsumes its own arm preflight. Keep it in lockstep with the wiring in
# ``_stop_gate_impl``: if that body ever stops reconciling before the rate-limit,
# this MUST become False.
RECONCILES_BEFORE_RATE_LIMIT = True


def _stop_gate_impl(args) -> int:
    from agentic_pr_dash import github_api  # noqa: PLC0415
    from .worktree_check import _check_worktree  # noqa: PLC0415
    from .worktrees import _owned_worktrees_across_roots, _reconcile_owned_across_roots, _detached_records_across_roots  # noqa: PLC0415
    from .waiter import _detached_loop_alive, _await_alive, _detached_pending_entry, _read_clean_exit_prs  # noqa: PLC0415
    import time  # noqa: PLC0415
    import sys  # noqa: PLC0415

    # BOU-1953: the stop-gate is a Stop-hook subprocess with a hard ~108s
    # deadline that fails CLOSED when exceeded. Every gh call it makes below
    # (across possibly several owned PRs) would otherwise each back off up to
    # `_GH_RATELIMIT_MAX_SLEEP_S` on a shared-quota rate-limit, easily blowing
    # the deadline. Disable backoff for this process so a rate-limited call
    # fails fast instead — the long-lived `await` waiter (a different process)
    # is unaffected and keeps backing off.
    github_api.set_rate_limit_backoff(False)

    cwd = os.path.abspath(args.cwd)

    session_id = args.session_id or _read_session_marker(cwd)

    # Reconcile FIRST: run list-owned-equivalent adoption with the durable owner
    # pid, bounded by a short shared budget for Stop-hook deadline safety. A PR
    # created mid-session whose arm hook was missed is adopted here (its marker is
    # written), so the passive owned-worktree collection below sees it. A FRESH
    # adoption must NOT stay hidden behind the clean-stop rate-limit, so it
    # bypasses the early-return for this one tick (BOU-1787). The adoption pass is
    # cheap on the common "everything already armed" stop: no candidate is
    # unmarked, so it never makes the gh call.
    newly_adopted: list[str] = []
    if session_id:
        budget = _env_int("STOP_RECONCILE_BUDGET", 8)
        deadline = time.monotonic() + budget if budget > 0 else None
        _owned, newly_adopted = _reconcile_owned_across_roots(
            session_id, cwd, getattr(args, "pid", None), deadline
        )

    interval = _env_int("STOP_INTERVAL", 180)
    state = _load_stop_state(cwd)
    now = time.time()
    last_pending = bool(state.get("fingerprint"))
    rate_limited = (
        interval > 0
        and not last_pending
        and (now - float(state.get("ts", 0) or 0)) < interval
    )
    if rate_limited and not newly_adopted:
        return 0
    _save_stop_state(cwd, {**state, "ts": now})

    if session_id:
        owned = _owned_worktrees_across_roots(session_id, cwd)
    else:
        owned = [cwd]

    pending: list[tuple[str, str]] = []
    for worktree in owned:
        code, text = _check_worktree(worktree, session_id, claim=False)
        if code == 10:
            pending.append((worktree, text))
        elif code == 0 and session_id:
            marker = _read_marker(worktree) or {}
            if str(marker.get("pr", "")).isdigit():
                _prune_stale_marker(worktree, marker, session_id)

    if session_id:
        detached = [r for r in _detached_records_across_roots(session_id, cwd)
                    if _record_has_blockers(r)]
        detached.sort(key=lambda r: (0 if r["p1"] else 1, -r["unresolved_threads"], r["pr"]))
        for r in detached:
            pending.append(_detached_pending_entry(r))

    # Map each owned PR to ALL of its worktree cwds so the repo-scoped
    # health/escalation lookups (loop._loop_covers_pr / the escalation marker)
    # hit the PR's own repo. Owned worktrees can span several repos
    # (maintenance_repo_roots) and the SAME PR number can exist in two repos, so
    # never collapse to a bare-number→single-worktree dict — keep a per-number
    # list and treat a PR as covered only when covered in EVERY repo that has it
    # (codex PR #50 review). Falls back to the anchor cwd for PRs with no
    # resolvable worktree (e.g. mocked/detached).
    pr_to_wts: dict[int, list[str]] = {}
    for wt, pr in _owned_open_pr_pairs(owned):
        pr_to_wts.setdefault(pr, []).append(wt)

    def _wts_for(pr: int) -> list[str]:
        return pr_to_wts.get(pr) or [cwd]

    # Escalation markers, read from each PR's OWN repo(s). Computed BEFORE the
    # pending split so an escalated PR that STILL has blockers (the usual state
    # after the loop failed to fix it → it lands in `pending`) surfaces the
    # escalation explanation + last executor error, not just the generic pending
    # prompt (codex PR #50 review).
    escalated_owned: dict[int, dict] = {}
    if session_id:
        for pr, wts in pr_to_wts.items():
            for wt in wts:
                info = _read_escalation_marker(wt).get(str(pr))
                if info is not None:
                    escalated_owned[pr] = info
                    break

    if not pending:
        if (not getattr(args, "no_waiter", False)) and session_id:
            from agentic_pr_dash import loop as _loop_mod  # noqa: PLC0415
            # A PR is loop-covered only if covered in EVERY repo that owns it; if
            # any repo's instance is uncovered/escalated, force the waiter.
            worktree_prs = {
                n for n in _owned_open_pr_numbers(owned)
                if not all(_loop_mod._loop_covers_pr(wt, n) for wt in _wts_for(n))
            }
            detached_prs = set()
            for _dr in _detached_records_across_roots(session_id, cwd):
                if _dr.get("state") not in ("merged", "closed", "draft", "unknown"):
                    detached_prs.add(_dr["pr"])
            open_prs = worktree_prs | detached_prs
            # Escalated PRs (that are still open + uncovered) — surface as an
            # exit-2 block distinct from the waiter prompt before the normal
            # waiter check.
            escalated_open = {pr: info for pr, info in escalated_owned.items() if pr in open_prs}
            if escalated_open and not _await_alive(cwd, session_id):
                escalation_text = _build_escalation_block(escalated_open)
                fingerprint = "escalated:" + ",".join(str(n) for n in sorted(escalated_open))
                count = int(state.get("count", 0)) + 1 if state.get("fingerprint") == fingerprint else 1
                _save_stop_state(cwd, {"ts": now, "fingerprint": fingerprint, "count": count})
                threshold = _env_int("STOP_LOOP_THRESHOLD", 3)
                if count < threshold:
                    print(escalation_text, file=sys.stderr)
                    return 2
                _save_stop_state(cwd, {"ts": now})
                return 0
            if open_prs and not _await_alive(cwd, session_id):
                # BOU-1962 (codex PR #75 review): when this session's waiter
                # already verified exactly these PRs clean and clean-exited, a
                # freshly demanded waiter would immediately clean-exit again —
                # the prompt would be unsatisfiable until STOP_LOOP_THRESHOLD.
                # This stop's own _check_worktree pass just found nothing
                # pending, so only re-demand a waiter that would actually STAY
                # alive: required CI running again (BOU-1789 watch-pending) or
                # CI state unobservable this tick (fail toward coverage).
                verified_clean = _read_clean_exit_prs(session_id)
                if verified_clean and open_prs <= verified_clean:
                    github_api.reset_checks_probe_failure_seen()
                    detached_watch = {
                        r["pr"]: bool(r.get("ci_watch_pending"))
                        for r in _detached_records_across_roots(session_id, cwd)
                        if r.get("state") not in ("merged", "closed", "draft", "unknown")
                    }
                    ci_running = False
                    for n in sorted(open_prs):
                        if n in pr_to_wts:
                            if any(github_api.required_checks_pending(n, wt)
                                   for wt in _wts_for(n)):
                                ci_running = True
                                break
                        elif detached_watch.get(n):
                            ci_running = True
                            break
                    if (
                        not ci_running
                        and not github_api.checks_probe_failure_seen()
                        and not github_api.rate_limit_seen()
                    ):
                        _save_stop_state(cwd, {"ts": now})
                        return 0
                fingerprint = "need-waiter:" + ",".join(str(n) for n in sorted(open_prs))
                count = int(state.get("count", 0)) + 1 if state.get("fingerprint") == fingerprint else 1
                _save_stop_state(cwd, {"ts": now, "fingerprint": fingerprint, "count": count})
                threshold = _env_int("STOP_LOOP_THRESHOLD", 3)
                if count < threshold:
                    print(_build_waiter_block(open_prs, cwd, session_id), file=sys.stderr)
                    return 2
                _save_stop_state(cwd, {"ts": now})
                return 0
        _save_stop_state(cwd, {"ts": now})
        return 0

    fingerprint = _stop_fingerprint(pending)
    count = int(state.get("count", 0)) + 1 if state.get("fingerprint") == fingerprint else 1
    _save_stop_state(cwd, {"ts": now, "fingerprint": fingerprint, "count": count})

    # An escalated PR almost always still has blockers, so it lands in `pending`
    # — surface its escalation explanation (consecutive-failure count + last
    # executor error) ABOVE the generic pending prompt (codex PR #50 review).
    if escalated_owned:
        print(_build_escalation_block(escalated_owned), file=sys.stderr)

    verbose_text = _build_stop_block(pending)
    # Write the full prompt (comment bodies, baseline-capture boilerplate, and
    # per-PR complete commands) to a payload file instead of stderr, and print
    # only a concise per-PR summary there — a stop-blocked agent otherwise reads
    # a wall of PR detail on every re-stop (BOU-1947). Fail-open to the verbose
    # block on stderr if the payload can't be written.
    try:
        payload_path = _write_stop_payload(cwd, verbose_text)
    except OSError:
        payload_path = None
    if payload_path is not None:
        print(_build_concise_stop_block(pending, payload_path), file=sys.stderr)
    else:
        print(verbose_text, file=sys.stderr)

    threshold = _env_int("STOP_LOOP_THRESHOLD", 3)
    if count >= threshold:
        print(
            f"[pr-watch] Same pending PR state seen {count}× with no progress — "
            f"releasing the stop gate so you can ask the user or take a safe "
            f"action. A later stop will re-enforce it.",
            file=sys.stderr,
        )
        _save_stop_state(cwd, {"ts": now})
        return 0

    if payload_path is not None:
        print(
            "[pr-watch] Read the payload file for full instructions, address the "
            "items (commit + push to each EXISTING branch), run the per-worktree "
            "`complete` command it shows, then try stopping again. If you cannot "
            "resolve an item yourself, tell the user.",
            file=sys.stderr,
        )
    else:
        print(
            "[pr-watch] Address the items above (commit + push to each EXISTING "
            "branch), run the per-worktree `complete` command shown in that section, "
            "then try stopping again. If you cannot resolve an item yourself, tell "
            "the user.",
            file=sys.stderr,
        )
    return 2


def _read_escalation_marker(cwd: str) -> dict:
    """Return the (repo-scoped) escalation marker dict, or {} if absent/corrupt.

    Routes through ``loop._escalated_marker_path`` so the reader and the loop's
    writer agree on the per-repo filename (keys stay bare PR numbers)."""
    from agentic_pr_dash import loop as _loop_mod  # noqa: PLC0415
    marker_path = _loop_mod._escalated_marker_path(cwd)
    try:
        with open(marker_path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _build_escalation_block(escalated_prs: dict[int, dict]) -> str:
    """Build the escalation block text for escaped PRs."""
    lines = [
        "[pr-watch] ESCALATION: The maintenance loop has repeatedly failed to fix "
        "PR(s) you own. Manual intervention is required:\n"
    ]
    for pr_num, info in sorted(escalated_prs.items()):
        streak = info.get("streak", "?")
        last_error = info.get("last_error", "unknown error")
        lines.append(f"  PR #{pr_num}: {streak} consecutive executor failures")
        lines.append(f"    Last error: {last_error[:200]}")
        lines.append("")
    lines.append(
        "Fix the PR manually or reconfigure the executor, then run the complete "
        "command for each PR above."
    )
    return "\n".join(lines)


def _record_has_blockers(record: dict) -> bool:
    return bool(
        record["unresolved_threads"]
        or record["ci_failing"]
        or record.get("changes_requested")
        or record.get("merge_conflict")
    )
