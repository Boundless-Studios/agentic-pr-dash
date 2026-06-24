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


def _stop_gate_impl(args) -> int:
    from .worktree_check import _check_worktree  # noqa: PLC0415
    from .worktrees import _owned_worktrees_across_roots, _detached_records_across_roots  # noqa: PLC0415
    from .waiter import _detached_loop_alive, _await_alive, _detached_pending_entry  # noqa: PLC0415
    import time  # noqa: PLC0415
    import sys  # noqa: PLC0415
    cwd = os.path.abspath(args.cwd)

    interval = _env_int("STOP_INTERVAL", 180)
    state = _load_stop_state(cwd)
    now = time.time()
    last_pending = bool(state.get("fingerprint"))
    if interval > 0 and not last_pending and (now - float(state.get("ts", 0) or 0)) < interval:
        return 0
    _save_stop_state(cwd, {**state, "ts": now})

    session_id = args.session_id or _read_session_marker(cwd)
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

    if not pending:
        if (not getattr(args, "no_waiter", False)) and session_id:
            from agentic_pr_dash import loop as _loop_mod  # noqa: PLC0415
            # Map each owned PR to ITS OWN worktree cwd so the repo-scoped
            # health/escalation lookups (loop._loop_covers_pr / the escalation
            # marker) hit the PR's own repo — owned worktrees can span several
            # repos (maintenance_repo_roots) and using the anchor cwd would read
            # the wrong file (codex PR #50 review). Falls back to the anchor cwd
            # for PRs with no resolvable worktree (e.g. mocked/detached).
            pr_to_wt = {pr: wt for wt, pr in _owned_open_pr_pairs(owned)}
            worktree_prs = {
                n for n in _owned_open_pr_numbers(owned)
                if not _loop_mod._loop_covers_pr(pr_to_wt.get(n, cwd), n)
            }
            detached_prs = set()
            for _dr in _detached_records_across_roots(session_id, cwd):
                if _dr.get("state") not in ("merged", "closed", "draft", "unknown"):
                    detached_prs.add(_dr["pr"])
            open_prs = worktree_prs | detached_prs
            # Escalated PRs — surface as an exit-2 block (distinct from the
            # waiter prompt) before the normal waiter check. Read each PR's
            # marker from its own repo so a sibling-repo escalation isn't missed.
            escalated_owned: dict[int, dict] = {}
            for pr in open_prs:
                info = _read_escalation_marker(pr_to_wt.get(pr, cwd)).get(str(pr))
                if info is not None:
                    escalated_owned[pr] = info
            if escalated_owned and not _await_alive(cwd, session_id):
                escalation_text = _build_escalation_block(escalated_owned)
                fingerprint = "escalated:" + ",".join(str(n) for n in sorted(escalated_owned))
                count = int(state.get("count", 0)) + 1 if state.get("fingerprint") == fingerprint else 1
                _save_stop_state(cwd, {"ts": now, "fingerprint": fingerprint, "count": count})
                threshold = _env_int("STOP_LOOP_THRESHOLD", 3)
                if count < threshold:
                    print(escalation_text, file=sys.stderr)
                    return 2
                _save_stop_state(cwd, {"ts": now})
                return 0
            if open_prs and not _await_alive(cwd, session_id):
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

    print(_build_stop_block(pending), file=sys.stderr)

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
