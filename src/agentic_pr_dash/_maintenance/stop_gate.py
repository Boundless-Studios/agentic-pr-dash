"""Stop-gate state helpers."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time as _time

from agentic_pr_dash.config import load as load_config
from ._common import _current_branch, _env_int
from .markers import _read_marker, _prune_stale_marker, _read_session_marker, _marker_provenance, _write_arm_marker

# BOU-2567 PR #122 review, P1 #3: `_check_worktree`'s clean-check text ends in
# "(deferred: N)" (see worktree_check._check_worktree) whenever a PR has
# deferred-but-not-yet-resolved threads. `_stop_gate_impl` otherwise discards
# ordinary code==0 text while walking owned worktrees, so the stop-gate
# surface itself emitted no deferred count at all -- a stated behavior
# ("the gate distinguishes deferred from unresolved in its output") that did
# not hold at THIS layer specifically, even though `check` already reported it.
_DEFERRED_COUNT_RE = re.compile(r"\(deferred:\s*(\d+)\)")


def _extract_deferred_count(text: str) -> int:
    match = _DEFERRED_COUNT_RE.search(text)
    return int(match.group(1)) if match else 0


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
        # Preserve the cheap branch/HEAD identity across later fingerprint
        # updates in the same stop-gate tick. Without this, the clean-path
        # writes below would erase it and force a full probe on every stop.
        if "checkout_identity" not in state:
            previous = _load_stop_state(cwd)
            if "checkout_identity" in previous:
                state = {**state, "checkout_identity": previous["checkout_identity"]}
        path = _stop_state_path(cwd)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError:
        pass


def _pr_head_cache_path(cwd: str) -> str:
    return str(load_config(cwd).state_dir_for(cwd) / "pr-watch.pr-head-cache.json")


def _load_pr_head_cache(cwd: str) -> dict:
    try:
        with open(_pr_head_cache_path(cwd), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_pr_head_cache(cwd: str, data: dict) -> None:
    try:
        path = _pr_head_cache_path(cwd)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def _local_head_sha(cwd: str) -> str:
    """Local HEAD commit sha for the checkout at ``cwd``, or "" on failure.

    Purely local ``git`` — no network — so it is cheap enough to call every
    tick for every owned worktree, and is the cache key BOU-2556's
    across-firings per-PR cache uses: an owned worktree's local head IS the
    PR's head once pushed, so an unchanged local head is strong (if not
    airtight — an unpushed local commit changes it too, which only ever
    causes an unnecessary-but-safe re-check) evidence nothing changed
    remotely either.
    """
    import subprocess  # noqa: PLC0415
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _probe_checkout_identity(worktree: str, timeout: float) -> list[str]:
    """Read branch and HEAD in one subprocess bounded by the caller's budget."""
    import subprocess  # noqa: PLC0415
    try:
        result = subprocess.run(
            [
                "git", "-C", worktree, "status", "--porcelain=v2", "--branch",
                "--untracked-files=no",
            ],
            capture_output=True, text=True, timeout=max(0.001, timeout), check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return [worktree, "", ""]
    fields = {}
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("# branch."):
                key, _, value = line[2:].partition(" ")
                fields[key] = value
    return [worktree, fields.get("branch.head", ""), fields.get("branch.oid", "")]


def _bounded_checkout_identity(
    worktrees: list[str], *, deadline: float | None, probe=None
) -> tuple[list[list[str]], bool]:
    """Collect checkout identities without crossing the shared stop deadline."""
    if probe is None:
        probe = _probe_checkout_identity
    identities: list[list[str]] = []
    for worktree in sorted(worktrees):
        remaining = 5.0 if deadline is None else deadline - _time.monotonic()
        if remaining <= 0:
            return identities, False
        identities.append(probe(worktree, min(5.0, remaining)))
    return identities, True


def _persist_unknown_binding_state(
    cwd: str, *, now: float, worktrees: list[str]
) -> None:
    """Make an unobservable binding ineligible for the clean-stop shortcut."""
    fingerprint = "current-pr-unknown:" + ",".join(sorted(worktrees))
    state = _load_stop_state(cwd)
    count = int(state.get("count", 0)) + 1 if state.get("fingerprint") == fingerprint else 1
    _save_stop_state(cwd, {"ts": now, "fingerprint": fingerprint, "count": count})


def _unknown_binding_fingerprint(worktrees: list[str]) -> str:
    """Stable fingerprint component for current bindings that were unobservable."""
    return "|current-pr-unknown:" + ",".join(sorted(worktrees))


def _cached_clean_binding_matches(
    cached_entry: dict | None,
    local_sha: str,
    binding,
    *,
    now: float,
    interval: float,
) -> bool:
    """Never reuse a remote-clean result without a fresh mutable-state read."""
    return False


def _binding_matches_live_checkout(binding, branch: str, head_sha: str) -> bool:
    """Return whether a prefetched PR binding still names this checkout."""
    def normalized(value: str) -> str:
        return "HEAD" if value in {"HEAD", "(detached)"} else value

    return bool(
        binding is not None
        and normalized(binding.branch) == normalized(branch)
        and binding.head_sha == head_sha
    )


def _unknown_binding_blocks_stop(
    worktree: str, provenance_for: dict[str, str]
) -> bool:
    """Return whether an unknown current binding belongs to this stop gate."""
    return provenance_for.get(worktree, _marker_provenance(worktree)) != "adopted"


def _blocking_unknown_worktrees(
    bindings: dict,
    ownership_conflicts: list[str],
    provenance_for: dict[str, str],
) -> list[str]:
    """Return unknown current bindings that belong to this stop gate."""
    candidates = [
        worktree for worktree, binding in bindings.items() if binding.unknown
    ]
    candidates.extend(
        worktree for worktree in ownership_conflicts if worktree not in candidates
    )
    return [
        worktree
        for worktree in candidates
        if _unknown_binding_blocks_stop(worktree, provenance_for)
    ]


def _revalidate_current_pr_binding(worktree: str, binding):
    """Fail closed when a prefetched binding no longer names the checkout."""
    if binding is None or not binding.resolved:
        return binding

    branch = _current_branch(worktree)
    head_sha = _local_head_sha(worktree)
    if _binding_matches_live_checkout(binding, branch, head_sha):
        return binding

    from dataclasses import replace  # noqa: PLC0415

    return replace(
        binding,
        branch=branch,
        pr_number=None,
        head_sha=head_sha,
        unknown=True,
    )


def _durable_stop_gate_pid(pid: int | None) -> int:
    """Prefer an explicit owner pid; otherwise resolve the durable session pid."""
    if pid is not None:
        return int(pid)
    from .worktrees import _resolve_owner_pid  # noqa: PLC0415

    return _resolve_owner_pid()


def _fence_current_pr_rebindings(
    bindings: dict,
    *,
    session_id: str,
    pid: int,
    provenance_for: dict[str, str],
    deadline: float | None = None,
    arm=_write_arm_marker,
) -> tuple[dict, list[str]]:
    """Acquire replacement ownership before exposing a rebound PR in memory."""
    from dataclasses import replace  # noqa: PLC0415

    rebound = dict(bindings)
    conflicts: list[str] = []
    for worktree, binding in bindings.items():
        if (
            binding.pr_number is None
            or binding.stale_pr_number is None
            or binding.is_draft
        ):
            continue
        binding = _revalidate_current_pr_binding(worktree, binding)
        if binding.unknown:
            conflicts.append(worktree)
            rebound[worktree] = binding
            continue
        arm_kwargs = {
            "expected_branch": binding.branch,
            "expected_head_sha": binding.head_sha,
        }
        if deadline is not None:
            arm_kwargs["deadline"] = deadline
        if arm(
            worktree,
            session_id,
            pid,
            binding.pr_number,
            provenance_for.get(worktree, _marker_provenance(worktree)) or "armed",
            **arm_kwargs,
        ):
            continue
        conflicts.append(worktree)
        rebound[worktree] = replace(
            binding, pr_number=None, resolved=True, unknown=True
        )
    return rebound, conflicts


def _stop_fingerprint(pending: list[tuple[str, str]]) -> str:
    """Stable hash of the pending (worktree, prompt) set."""
    h = hashlib.sha256()
    for path, text in sorted(pending):
        stable_text = "\n".join(
            line for line in text.splitlines() if not line.startswith("OBSERVED_AT=")
        )
        h.update(path.encode("utf-8"))
        h.update(b"\0")
        h.update(stable_text.encode("utf-8"))
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
        "new PR), then re-stop:\n",
        # BOU-2490: this gate writes no code — it asks YOU to. But the detached
        # pr-maintenance-loop shares your worktree and DOES run an executor that
        # commits and pushes. Say so here, because a session that later finds
        # commits it did not write has otherwise no way to attribute them, and
        # concludes something "took over its work".
        "NOTE: this gate only asks — it never edits, commits or pushes. A detached "
        "pr-maintenance-loop may run an executor in this same worktree, which does. "
        "If you find commits you did not write, attribute them before reacting:\n"
        "  git log --format='%h %cn %s' -10   # committer `apd-loop-executor` ⇒ the loop\n"
        "Commit your own work promptly; the loop dispatches on unresolved feedback, "
        "so clearing the feedback is what stops it — not fighting the daemon.\n",
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

    Kept marker-only and single-argument on purpose (BOU-2223 Stage 2): several
    existing tests monkeypatch this function directly with a one-argument
    stub, and it is the specification. The claim-preferred merge lives one
    layer up, in :func:`_effective_pr_pairs`, which calls this function rather
    than replace it. ``_owned_open_pr_numbers`` itself is also left untouched
    at its one call site (the waiter-demand block) — several existing tests
    monkeypatch IT directly, decoupled from any real marker on disk, to force
    "no owned open PRs"/"exactly PR N" independent of ``pr_to_wts``; that
    decoupling predates Stage 2 (``pr_to_wts`` was already built from this same
    marker-only function while the waiter block used its own call), so it is
    left exactly as it was rather than folded into the claim-preferred flip.
    """
    pairs: list[tuple[str, int]] = []
    for wt in owned:
        marker = _read_marker(wt) or {}
        pr_raw = marker.get("pr", "")
        if str(pr_raw).isdigit():
            pairs.append((wt, int(pr_raw)))
    return pairs


def _effective_pr_pairs(
    owned: list[str],
    pr_for: dict[str, int],
    *,
    current_resolved: set[str] | None = None,
) -> list[tuple[str, int]]:
    """``(worktree, pr)`` pairs, preferring the claim-derived ``pr_for`` map
    (``ownership_resolution.resolve_owned``, BOU-2223 Stage 2) per worktree and
    falling back to the marker-derived :func:`_owned_open_pr_pairs` otherwise.

    A thin layer over the unmodified marker-only helper so its call signature
    — and every existing test that monkeypatches it with a single argument —
    stays exactly as it was before this flip.
    """
    marker_by_wt = dict(_owned_open_pr_pairs(owned))
    pairs: list[tuple[str, int]] = []
    for wt in owned:
        if current_resolved is not None and wt in current_resolved:
            # A positive current-branch lookup is authoritative, including a
            # definite "no open PR" answer. Do not fall back to a closed marker
            # or claim PR after a branch switch.
            pr = pr_for.get(wt)
        else:
            pr = pr_for.get(wt, marker_by_wt.get(wt))
        if pr is not None:
            pairs.append((wt, pr))
    return pairs


def _prefetch_owned_pr_state(
    pairs: list[tuple[str, int]], *, deadline: float | None = None
) -> None:
    """Batch-fetch review-thread + CI-rollup data for every owned PR in as few
    GraphQL round trips as possible (BOU-2556), priming
    ``github_api``'s per-process cache before the per-worktree loop runs.

    Grouped by repo — almost always exactly ONE for a single session's owned
    set — so N owned PRs in the same repo cost roughly one round trip instead
    of N serial "review threads + CI rollup" pairs. A repo with only one owned
    PR is skipped (no batching win over the plain per-PR call). Best-effort:
    any failure here just leaves the cache unprimed for that repo, and the
    per-worktree loop falls through to its normal (slower, serial, and
    independently correct) per-PR calls — this is purely a speed optimization,
    never a second source of truth for PR state.
    """
    from agentic_pr_dash import github_api  # noqa: PLC0415

    by_repo: dict[str, set[int]] = {}
    repo_cwd: dict[str, str] = {}
    for wt, pr in pairs:
        try:
            repo = github_api.repo_slug_for_prefetch(wt)
        except Exception:  # noqa: BLE001 - optimization only, never fail the gate
            repo = ""
        if not repo or "/" not in repo:
            continue
        by_repo.setdefault(repo, set()).add(pr)
        repo_cwd.setdefault(repo, wt)

    for repo, pr_numbers in by_repo.items():
        if len(pr_numbers) < 2:
            continue  # no batching win for a single PR in this repo
        if deadline is not None and _time.monotonic() >= deadline:
            return
        try:
            owner, name = repo.split("/", 1)
            entries = github_api.batch_fetch_pr_review_and_ci(
                owner, name, sorted(pr_numbers), cwd=repo_cwd[repo],
                deadline=deadline,
            )
            if entries:
                github_api.prime_pr_batch_cache(repo, entries)
        except Exception:  # noqa: BLE001 - optimization only, never fail the gate
            pass


def _build_budget_block(
    unknown_worktrees: list[str], *, checked_count: int, total: int,
    pr_for: dict[str, int],
) -> str:
    """Render the distinct "budget exhausted, N unchecked" block (BOU-2556).

    Deliberately separate from :func:`_build_stop_block`'s "fix + commit +
    push" framing — there is nothing confirmed broken here, only PRs this tick
    did not get to. Lets the operator (or the wrapping Stop-hook message) tell
    "the gate crashed" apart from "the gate ran out of time with N of M
    checked", which a bare subprocess-timeout failure cannot distinguish.
    """
    lines = [
        f"[pr-watch] BUDGET-UNKNOWN: stop-gate wall-clock budget exhausted "
        f"(checked {checked_count} of {total} owned PRs). The remaining "
        f"{len(unknown_worktrees)} PR(s) below are UNKNOWN this tick — NOT "
        f"confirmed clean, NOT a crash — just not yet examined:\n",
    ]
    for wt in unknown_worktrees:
        pr = pr_for.get(wt)
        pr_desc = f"#{pr}" if pr is not None else "(unresolved)"
        lines.append(f"  - {wt}: PR {pr_desc} not checked this tick")
    lines.append(
        "\nNo action needed for these specifically — re-stop shortly (or let "
        "the background waiter / maintenance loop catch them) rather than "
        "assuming they are clean."
    )
    return "\n".join(lines)


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
    from .worktree_check import _check_worktree, _use_current_pr_binding  # noqa: PLC0415
    from .worktrees import _owned_worktrees_across_roots, _reconcile_owned_across_roots, _detached_records_across_roots  # noqa: PLC0415
    from .waiter import _detached_loop_alive, _await_alive, _detached_pending_entry, _read_clean_exit_keys, _clean_exit_key  # noqa: PLC0415
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
    github_api.reset_required_check_observations()

    cwd = os.path.abspath(args.cwd)

    session_id = args.session_id or _read_session_marker(cwd)
    owner_pid = _durable_stop_gate_pid(getattr(args, "pid", None))

    # Reconcile FIRST: run list-owned-equivalent adoption with the durable owner
    # pid, bounded by a short shared budget for Stop-hook deadline safety. A PR
    # created mid-session whose arm hook was missed is adopted here (its marker is
    # written), so the passive owned-worktree collection below sees it. A FRESH
    # adoption must NOT stay hidden behind the clean-stop rate-limit, so it
    # bypasses the early-return for this one tick (BOU-1787). The adoption pass is
    # cheap on the common "everything already armed" stop: no candidate is
    # unmarked, so it never makes the gh call.
    newly_adopted: list[str] = []
    detached: list[dict] = []
    if session_id:
        budget = _env_int("STOP_RECONCILE_BUDGET", 8)
        deadline = time.monotonic() + budget if budget > 0 else None
        reconciled_owned, newly_adopted = _reconcile_owned_across_roots(
            session_id, cwd, owner_pid, deadline
        )
    else:
        reconciled_owned = [cwd]

    interval = _env_int("STOP_INTERVAL", 180)
    state = _load_stop_state(cwd)
    now = time.time()
    # A released fingerprint is still a pending observation whose durable
    # identity must be rechecked on every stop.  Treating it as clean here
    # would hide a newly published head (or restarted CI) until STOP_INTERVAL
    # expires.
    last_pending = bool(
        state.get("fingerprint") or state.get("released_fingerprint")
    )
    rate_limited = (
        interval > 0
        and not last_pending
        and (now - float(state.get("ts", 0) or 0)) < interval
    )
    gate_budget = _env_int("STOP_GATE_BUDGET", 60)
    gate_deadline = time.monotonic() + gate_budget if gate_budget > 0 else None
    checkout_identity, identity_complete = _bounded_checkout_identity(
        reconciled_owned, deadline=gate_deadline
    )
    if not identity_complete:
        _persist_unknown_binding_state(
            cwd, now=now, worktrees=list(reconciled_owned)
        )
        print(
            "[pr-watch] BUDGET-UNKNOWN: checkout identity probes exceeded the "
            "stop-gate budget; refusing to treat the owned PR set as clean.",
            file=sys.stderr,
        )
        return 2
    if (
        rate_limited
        and not newly_adopted
        and state.get("checkout_identity") == checkout_identity
    ):
        return 0
    _save_stop_state(cwd, {**state, "ts": now, "checkout_identity": checkout_identity})

    # BOU-2223 Stage 2: flip ownership reads onto the claim store, with the
    # marker as fallback whenever the two disagree. `resolve_owned` takes (or
    # is given) exactly ONE `ownership.snapshot()` for this whole call — the
    # store read is a full-file read under a bounded lock, and this path shares
    # the stop gate's ~108s fail-closed deadline (BOU-1953), so a per-worktree
    # read is not an option.
    from .ownership_resolution import (  # noqa: PLC0415
        claim_reads_enabled, resolve_owned, resolve_worktree,
    )

    # ONE snapshot for the whole tick: `resolve_owned` below and the per-worktree
    # `resolve_worktree` prune inside the loop both need one, and the store read is
    # a full-file read under a bounded lock. Taken only when claim reads are on, so
    # the kill-switch path keeps costing exactly zero store reads.
    resolution_snapshot = None
    if claim_reads_enabled():
        from agentic_pr_dash import ownership  # noqa: PLC0415
        resolution_snapshot = ownership.snapshot()

    resolution = None
    if session_id:
        marker_owned = _owned_worktrees_across_roots(session_id, cwd)
        resolution = resolve_owned(
            session_id, cwd, marker_owned, snap=resolution_snapshot
        )
        owned = resolution.worktrees
    else:
        owned = [cwd]
    pr_for = resolution.pr_for if resolution is not None else {}
    provenance_for = resolution.provenance_for if resolution is not None else {}

    from .ownership_resolution import resolve_current_prs  # noqa: PLC0415
    current_pr_bindings = resolve_current_prs(
        owned,
        session_id,
        kind="stop_gate_pr_watch_divergence",
        snap=resolution_snapshot,
        deadline=gate_deadline,
    )
    ownership_conflicts: list[str] = []
    if session_id:
        current_pr_bindings, ownership_conflicts = _fence_current_pr_rebindings(
            current_pr_bindings,
            session_id=session_id,
            pid=owner_pid,
            provenance_for=provenance_for,
            deadline=gate_deadline,
        )
    current_pr_for = {
        worktree: binding.pr_number
        for worktree, binding in current_pr_bindings.items()
        if binding.resolved and binding.pr_number is not None
    }
    current_resolved_worktrees = {
        worktree
        for worktree, binding in current_pr_bindings.items()
        if binding.resolved
    }
    current_unknown_worktrees = _blocking_unknown_worktrees(
        current_pr_bindings, ownership_conflicts, provenance_for
    )
    stale_current_pr_numbers = {
        binding.stale_pr_number
        for binding in current_pr_bindings.values()
        if binding.resolved and binding.stale_pr_number is not None
    }
    # BOU-2556: the per-worktree loop below used to pay a serial "review-thread
    # query + CI-rollup query" for EVERY owned PR — fine at one PR, a ~108s
    # Stop-hook timeout at seven (the incident this fixes). Prefetch all of
    # them in as few round trips as possible BEFORE the loop runs, grouped by
    # repo (almost always exactly one for a single session's owned set), and
    # prime `github_api`'s per-process cache with the results; the loop below
    # is UNCHANGED — `_check_worktree`'s internal calls to
    # `get_review_threads`/`required_checks_pending` transparently become cache
    # hits. Best-effort: a prefetch failure just leaves the cache unprimed and
    # the loop falls back to its original per-PR calls (never a wrong answer,
    # only a slower one).
    github_api.clear_pr_batch_cache()
    effective_pr_pairs = _effective_pr_pairs(
        owned,
        {**pr_for, **current_pr_for},
        current_resolved=current_resolved_worktrees,
    )
    # Exact per-PR observations must not consume a pre-loop snapshot: reviews
    # and CI can change without changing the local checkout identity. The
    # aggregate prefetch remains available to dashboard/orchestrator callers,
    # but the stop gate intentionally performs current per-PR reads.

    # BOU-2556: give the per-worktree loop below a wall-clock budget so a
    # session owning many PRs degrades gracefully instead of blowing the
    # Stop-hook's own ~108s hard timeout wholesale (which reads to the caller
    # as a bare "subprocess failed" with ZERO detail on what was actually
    # checked). Once exceeded, every REMAINING owned worktree is left UNCHECKED
    # this tick — reported as unknown, not silently dropped and never folded
    # into "clean" (the same "unknown is not clean" invariant as
    # `gh_state_unknown` / `WorktreeOwnership.unknown` elsewhere in this
    # package). Default (60s) is chosen with real headroom under the observed
    # ~108s external timeout so this budget — not that outer one — is what
    # actually governs completion.
    checked_count = 0
    unknown_worktrees: list[str] = []

    # BOU-2556: per-PR cache across FIRINGS (separate stop-gate subprocesses),
    # keyed on each owned worktree's local head sha — distinct from, and
    # deliberately reusing, `interval` (STOP_INTERVAL) above rather than
    # inventing a second cache-lifetime knob. STOP_INTERVAL already skips the
    # WHOLE gate for up to `interval` seconds when the LAST tick found nothing
    # pending; this cache extends that exact tolerance to the per-PR case,
    # which STOP_INTERVAL cannot help with once ANY owned PR is pending (the
    # whole-gate skip is disabled the moment `last_pending` is true, so a
    # session with one blocked PR among many re-pays every OTHER PR's query on
    # every single stop without this). Only ever short-circuits a CLEAN (code
    # 0) verdict for an UNCHANGED head sha within `interval` seconds — a cached
    # PENDING or unobservable result is never trusted stale, so real work
    # always keeps re-surfacing and an outage is never papered over.
    pr_head_cache = {
        wt: entry for wt, entry in _load_pr_head_cache(cwd).items() if wt in owned
    }
    pr_head_cache_dirty = False

    pending: list[tuple[str, str]] = []
    # An owned PR whose check was UNOBSERVABLE (code 2: gh could not resolve
    # the PR / its blockers) or warn-only-deferred (exit 0 but the PR HAS
    # blockers a foreign owner is servicing) must keep the stop gate
    # fail-closed: neither state may feed the BOU-1962 marker-skip early
    # return below (codex PR #75 review, round 2).
    check_unobservable = False
    check_warn_only = False
    from .worktree_check import DRAFT_PR_MARKER, WARN_ONLY_MARKER  # noqa: PLC0415
    adopted_pending: list[tuple[str, str]] = []
    draft_worktrees: set[str] = set()
    # BOU-2450: `pr_for` (below, from `resolve_owned`) is a claim-derived
    # snapshot taken ONCE at the top of this call — before this very loop runs
    # and can PRUNE a worktree's marker/claim on discovering (via a fresh
    # per-worktree probe) that its recorded PR already merged/closed. Without
    # tracking what got pruned THIS tick, the waiter-demand block further down
    # still finds the stale PR number in `pr_for` and reports "you own open PR
    # #N" (or demands a waiter for it) for a PR this same tick just confirmed
    # is no longer open.
    pruned_pr_keys: set[tuple[str, int]] = set()
    # BOU-2567: accumulated separately from `pending` -- a deferred thread is
    # never a blocker (see `_extract_deferred_count`'s docstring above), but
    # the count must still be visible at this surface, not merely at `check`'s.
    total_deferred = 0
    for worktree in owned:
        live_identity, identity_complete = _bounded_checkout_identity(
            [worktree], deadline=gate_deadline
        )
        if not identity_complete:
            unknown_worktrees.append(worktree)
            continue
        _, live_branch, local_sha = live_identity[0]
        cached_entry = pr_head_cache.get(worktree)
        binding = current_pr_bindings.get(worktree)
        if (
            binding is not None
            and binding.resolved
            and not _binding_matches_live_checkout(binding, live_branch, local_sha)
        ):
            from dataclasses import replace  # noqa: PLC0415

            binding = replace(
                binding,
                branch=live_branch,
                pr_number=None,
                head_sha=local_sha,
                unknown=True,
            )
            current_pr_bindings[worktree] = binding
            current_pr_for.pop(worktree, None)
            is_adopted = not _unknown_binding_blocks_stop(worktree, provenance_for)
            # A replacement binding that changed underneath an adopted
            # worktree is informational maintenance-loop scope, not a blocker
            # for this session. Drop the prefetched stale pair as well so the
            # later waiter-coverage pass cannot resurrect the old PR.
            if is_adopted:
                effective_pr_pairs = [
                    (path, pr)
                    for path, pr in effective_pr_pairs
                    if path != worktree
                ]
            if not is_adopted and worktree not in current_unknown_worktrees:
                current_unknown_worktrees.append(worktree)
        if _cached_clean_binding_matches(
            cached_entry, local_sha, binding, now=now, interval=interval
        ):
            code, text = 0, cached_entry.get("text", "nothing pending")
            checked_count += 1
        elif gate_deadline is not None and time.monotonic() >= gate_deadline:
            # BOU-2556: budget exhausted — every worktree from here on is
            # UNCHECKED this tick, not confirmed clean. Recorded distinctly so
            # the blocking message can say "checked K of M, budget exhausted"
            # instead of silently treating an unexamined PR as fine.
            #
            # BOU-2567 (PR #122 review round 4): deliberately does NOT touch
            # `total_deferred` here, and `continue`s before the extraction
            # below ever runs. A budget-unknown PR's deferred count is
            # genuinely UNKNOWN this tick — we never called `_check_worktree`
            # for it — not zero and not "whatever it was last time". Deferred
            # (a real finding, tracked, non-blocking) and budget-unknown (we
            # never looked, fail closed) are different facts with OPPOSITE
            # gate policies; folding one into the other here would be exactly
            # the "unknown collapsing into a definite answer" class of bug
            # this package has already shipped four fixes for.
            unknown_worktrees.append(worktree)
            continue
        else:
            with _use_current_pr_binding(binding):
                code, text = _check_worktree(worktree, session_id, claim=False)
            checked_count += 1
            pr_head_cache[worktree] = {
                "head_sha": local_sha,
                "branch": binding.branch if binding is not None else None,
                "pr_number": binding.pr_number if binding is not None else None,
                "is_draft": bool(binding.is_draft) if binding is not None else False,
                "checked_at": now,
                "code": code,
                "text": text,
            }
            pr_head_cache_dirty = True
        # BOU-2567: accumulate from WHICHEVER branch above produced `text` —
        # the cached-clean short-circuit's `text` is the SAME string
        # `_check_worktree` returned when it was first computed (the cache
        # entry stores the full text, deferred suffix included), so a
        # deferred-only PR's count correctly survives being served from the
        # BOU-2556 head-sha cache. The budget-exhausted branch above
        # `continue`s before this line, so it never contributes one.
        total_deferred += _extract_deferred_count(text)
        if code == 10:
            # An ADOPTED worktree is one auto-adoption handed us: this session
            # never armed it and has no context on the work. Its blockers are
            # reported, not enforced — the maintenance loop services them. Making
            # them blocking wedges a session on another epic's PR, and the "fix +
            # push to the existing branch" instruction can clobber a live sibling
            # session mid-edit (BOU-2221; hit with #2650 and #2653).
            if provenance_for.get(worktree, _marker_provenance(worktree)) == "adopted":
                adopted_pending.append((worktree, text))
            else:
                pending.append((worktree, text))
        elif code == 2:
            check_unobservable = True
        elif code == 0 and text.rstrip().endswith(WARN_ONLY_MARKER):
            check_warn_only = True
        elif code == 0 and text == DRAFT_PR_MARKER:
            draft_worktrees.add(worktree)
        elif code == 0 and session_id:
            # Claim-first (BOU-2223 Stage 4). This used to key off a raw marker
            # read, which silently stopped firing once marker writes were retired:
            # a claim-only worktree has no marker, so a merged/closed PR would
            # never release its claim and the claim-derived view would keep
            # reporting an owner for a dead PR forever.
            # NB: never rebind `owned` here — it is the list this loop iterates
            # and is still needed by _effective_pr_pairs / _owned_open_pr_numbers
            # below.
            pruned = resolve_worktree(
                worktree, kind="prune_divergence", snap=resolution_snapshot
            )
            if pruned.pr_number is not None:
                actually_pruned = _prune_stale_marker(
                    worktree, {"pr": str(pruned.pr_number)}, session_id
                )
                # Only when `_prune_stale_marker` ACTUALLY pruned (it no-ops
                # unless its own fresh `_pr_open_state` probe confirms
                # merged/closed) does this tick know the PR is dead — a
                # still-open PR must never be excluded from the waiter-demand
                # set below.
                if actually_pruned:
                    from ._common import _repo_slug as _slug  # noqa: PLC0415
                    pruned_pr_keys.add((_slug(worktree), pruned.pr_number))

    if pr_head_cache_dirty:
        _save_pr_head_cache(cwd, pr_head_cache)

    if session_id:
        # Stop gate: fail closed. An unresolvable gh probe counts as a blocker
        # exactly like a real one — see _record_has_blockers.
        detached = [r for r in _detached_records_across_roots(session_id, cwd)
                    if _record_has_blockers(r, unknown_state_blocks=True)]
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
    # `effective_pr_pairs` was already computed above (BOU-2556 prefetch seed);
    # reused here unchanged rather than recomputed.
    for wt, pr in effective_pr_pairs:
        if wt in draft_worktrees:
            continue
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

    # Adopted PRs never block, but they must not vanish either: say what was
    # inherited and who is actually handling it (BOU-2221).
    if adopted_pending:
        summaries = ", ".join(
            f"#{_extract_pr_number(text) or '?'}" for _wt, text in adopted_pending
        )
        print(
            f"[pr-watch] FYI: adopted PR(s) {summaries} have pending work. These were "
            f"auto-adopted, not armed by this session — the maintenance loop owns them "
            f"and they are NOT blocking your stop.",
            file=sys.stderr,
        )

    if current_unknown_worktrees:
        print(
            "[pr-watch] current PR ownership could not be observed for "
            f"{len(current_unknown_worktrees)} owned worktree(s); refusing to "
            "treat stale PR state as clean. Retry when GitHub state is observable.",
            file=sys.stderr,
        )
        _persist_unknown_binding_state(
            cwd, now=now, worktrees=current_unknown_worktrees
        )
        if not pending:
            return 2

    if not pending and not unknown_worktrees:
        # BOU-2567: surface the deferred count on every otherwise-clean path
        # through this branch (idle, escalation, waiter-demand, clean-exit) —
        # `check`'s own text already carries it (worktree_check._check_worktree),
        # but this surface silently dropped it. Non-blocking: printed, not
        # counted toward any of the return-2 paths below.
        #
        # Gated on `not unknown_worktrees` too (BOU-2556, PR #122 review round
        # 4): a deferred thread and a budget-unknown PR are different facts
        # with opposite gate policies. This print only runs once the gate has
        # decided the tick is ACTUALLY clean — never merged into, or printed
        # alongside, the budget-unknown blocking path below, which is reached
        # instead whenever unknown_worktrees is non-empty regardless of
        # total_deferred.
        if total_deferred:
            print(
                f"[pr-watch] {total_deferred} review thread(s) deferred "
                "(tracked by follow-up ticket(s)) — not blocking.",
                file=sys.stderr,
            )
        if (not getattr(args, "no_waiter", False)) and session_id:
            from agentic_pr_dash import loop as _loop_mod  # noqa: PLC0415
            # A PR is loop-covered only if covered in EVERY repo that owns it; if
            # any repo's instance is uncovered/escalated, force the waiter.
            # Marker-derived numbers UNIONed with the claim-derived ones. The
            # marker half stays a direct call so the several existing tests that
            # monkeypatch `_owned_open_pr_numbers` with a one-argument stub keep
            # controlling it (BOU-2223 Stage 2 left it deliberately marker-only).
            # The union is what Stage 4 needs: once the marker stops being
            # written this set would otherwise collapse to the detached PRs
            # alone, and a session owning a live open PR would never be told to
            # start a waiter — arriving review comments and red CI would stop
            # waking it. That is a fail-OPEN regression on a fail-closed path.
            # Use the effective current/claim/marker pairs here rather than
            # only ``current_pr_for``. A claim-only worktree without a usable
            # local branch (for example a detached maintenance record) has no
            # current binding, but it still needs coverage. When a branch was
            # positively resolved, ``_effective_pr_pairs`` has already removed
            # any stale PR and therefore remains safe for rebinds.
            claim_open_pr_numbers = {
                pr for wt, pr in effective_pr_pairs if wt not in draft_worktrees
            }
            draft_pr_numbers = {
                pr for wt, pr in effective_pr_pairs if wt in draft_worktrees
            }
            non_draft_pr_numbers = {
                pr for wt, pr in effective_pr_pairs if wt not in draft_worktrees
            }
            exclusively_draft_pr_numbers = draft_pr_numbers - non_draft_pr_numbers
            marker_open_pr_numbers = (
                _owned_open_pr_numbers(owned)
                - stale_current_pr_numbers
                - exclusively_draft_pr_numbers
            )
            owned_pr_numbers = (
                marker_open_pr_numbers | claim_open_pr_numbers
            )
            pairs_by_number: dict[int, set[tuple[str, int]]] = {}
            from ._common import _repo_slug as _slug  # noqa: PLC0415
            for wt, pr in effective_pr_pairs:
                pairs_by_number.setdefault(pr, set()).add((_slug(wt), pr))
            fully_pruned_numbers = {
                pr for pr, keys in pairs_by_number.items()
                if keys and keys <= pruned_pr_keys
            }
            owned_pr_numbers -= fully_pruned_numbers

            def _live_wts_for(n: int) -> list[str]:
                """Worktrees for PR ``n`` that were NOT pruned this tick.

                A number is only dropped above when EVERY repo carrying it was
                pruned. For a partially-pruned number — repo A's #N closed while
                repo B's #N is still open — the stale repo-A worktree survives in
                `_wts_for(n)`, is naturally uncovered by any loop, and so makes
                `all(...)` false and demands a waiter for an already-closed PR.
                Coverage must be judged only on the worktrees still in play.
                """
                return [
                    wt for wt in _wts_for(n)
                    if (_slug(wt), n) not in pruned_pr_keys
                ]

            worktree_prs = {
                n for n in owned_pr_numbers
                if not all(_loop_mod._loop_covers_pr(wt, n) for wt in _live_wts_for(n))
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
                escalation_state = json.dumps(
                    escalated_open, sort_keys=True, separators=(",", ":")
                )
                fingerprint = "escalated:" + hashlib.sha256(
                    escalation_state.encode("utf-8")
                ).hexdigest()
                if (
                    state.get("released_fingerprint") == fingerprint
                    and state.get("released_session_id") == session_id
                ):
                    _save_stop_state(cwd, {**state, "ts": now})
                    return 0
                count = int(state.get("count", 0)) + 1 if state.get("fingerprint") == fingerprint else 1
                _save_stop_state(cwd, {"ts": now, "fingerprint": fingerprint, "count": count})
                threshold = _env_int("STOP_LOOP_THRESHOLD", 3)
                if count < threshold:
                    print(escalation_text, file=sys.stderr)
                    return 2
                _save_stop_state(
                    cwd,
                    {
                        "ts": now,
                        "released_fingerprint": fingerprint,
                        "released_session_id": session_id,
                    },
                )
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
                verified_clean = _read_clean_exit_keys(session_id)
                ci_running = False
                # A blocker-status read that failed DURING the _check_worktree
                # pass above (get_ci_checks / its REST fallback unobservable)
                # means this stop's "nothing pending" was computed blind —
                # capture it BEFORE the scoped reset below or the marker-skip
                # would treat the blind result as clean (codex PR #75 review,
                # round 5).
                check_probe_failed = github_api.checks_probe_failure_seen()
                # Scope the probe-failure flag to the observations that feed
                # THIS clean-exit decision: reset BEFORE the detached-record
                # read below, so a failed/truncated CI probe while building
                # those records (required_checks_pending inside
                # _detached_pr_records) is still visible at the final
                # checks_probe_failure_seen() gate — a detached PR has no
                # worktree in pr_to_wts to re-probe, so that read is its only
                # observation (codex PR #75 review, round 4).
                github_api.reset_checks_probe_failure_seen()
                # Repo-qualified identities for every open PR the demand would
                # cover — the SAME number in two maintenance repos must stay
                # distinct in both the marker-coverage and CI-watch reads
                # (codex PR #75 review, round 3).
                open_detached_records = [
                    r for r in _detached_records_across_roots(session_id, cwd)
                    if r.get("state") not in ("merged", "closed", "draft", "unknown")
                ]
                from ._common import _repo_slug as _slug  # noqa: PLC0415
                open_keys = {
                    _clean_exit_key(_slug(wt), n)
                    for n, wts in pr_to_wts.items()
                    if n in open_prs
                    for wt in wts
                } | {
                    _clean_exit_key(r.get("repo", ""), r["pr"])
                    for r in open_detached_records
                    if r["pr"] in open_prs
                }
                ci_pending_identities = {
                    f"{r.get('repo', '')}#{r['pr']}"
                    for r in open_detached_records
                    if r.get("ci_watch_pending")
                }
                for n in sorted(open_prs):
                    for wt in pr_to_wts.get(n, ()):
                        pending_observation = (
                            github_api.observed_required_checks_pending(n, wt)
                        )
                        if pending_observation is None:
                            pending_observation = github_api.required_checks_pending(
                                n, wt
                            )
                        if pending_observation:
                            ci_pending_identities.add(f"{_slug(wt)}#{n}")
                ci_running = bool(ci_pending_identities)
                if (
                    verified_clean
                    and open_keys
                    and open_keys <= verified_clean
                    # Fail closed: an unobservable or warn-only-deferred check
                    # this pass means "nothing pending" was NOT established for
                    # every owned PR — a CI-only probe below cannot stand in
                    # for the blocked/unresolved-thread signal (codex PR #75
                    # review, round 2). Same for a failed blocker-status read
                    # during the check pass (round 5).
                    and not check_unobservable
                    and not check_warn_only
                    and not check_probe_failed
                ):
                    if (
                        not ci_running
                        and not github_api.checks_probe_failure_seen()
                        and not github_api.rate_limit_seen()
                    ):
                        _save_stop_state(cwd, {"ts": now})
                        return 0
                waiter_identities = {
                    f"{_slug(wt)}#{n}@{_local_head_sha(wt) or 'unknown'}"
                    for n in open_prs
                    for wt in _live_wts_for(n)
                } | {
                    f"{r.get('repo', '')}#{r['pr']}@{r.get('head_sha') or 'unknown'}"
                    for r in open_detached_records
                    if r["pr"] in open_prs
                }
                fingerprint = "need-waiter:" + ",".join(sorted(waiter_identities))
                fingerprint += "|ci-pending:" + ",".join(
                    sorted(ci_pending_identities)
                )
                if (
                    state.get("released_fingerprint") == fingerprint
                    and state.get("released_session_id") == session_id
                ):
                    _save_stop_state(cwd, {**state, "ts": now})
                    return 0
                count = int(state.get("count", 0)) + 1 if state.get("fingerprint") == fingerprint else 1
                _save_stop_state(cwd, {"ts": now, "fingerprint": fingerprint, "count": count})
                threshold = _env_int("STOP_LOOP_THRESHOLD", 3)
                if count < threshold:
                    print(_build_waiter_block(open_prs, cwd, session_id), file=sys.stderr)
                    return 2
                _save_stop_state(
                    cwd,
                    {
                        "ts": now,
                        "released_fingerprint": fingerprint,
                        "released_session_id": session_id,
                    },
                )
                return 0
        _save_stop_state(cwd, {"ts": now})
        return 0

    # BOU-2556: an unknown (budget-unchecked) worktree folds into the SAME
    # fingerprint/retry-count/threshold machinery as a confirmed pending one —
    # a persistent budget shortfall on the SAME owned set should also release
    # the gate after STOP_LOOP_THRESHOLD reps, exactly like persistent real
    # pending work. Sorted so the fingerprint is stable regardless of dict/set
    # iteration order.
    fingerprint = _stop_fingerprint(pending)
    from ._common import _repo_slug as _slug  # noqa: PLC0415
    pending_heads = {
        f"{_slug(path)}#{_extract_pr_number(text)}@"
        f"{github_api.published_pr_head_sha(int(_extract_pr_number(text)), path) or 'unknown'}"
        for path, text in pending
        if not path.startswith("(no worktree)")
        and _extract_pr_number(text).isdigit()
    } | {
        f"{record.get('repo', '')}#{record['pr']}@{record.get('head_sha') or 'unknown'}"
        for record in detached
    }
    if pending_heads:
        fingerprint += "|heads:" + ",".join(sorted(pending_heads))
    if escalated_owned:
        escalation_state = json.dumps(escalated_owned, sort_keys=True, separators=(",", ":"))
        fingerprint += "|escalated:" + hashlib.sha256(
            escalation_state.encode("utf-8")
        ).hexdigest()
    if unknown_worktrees:
        fingerprint += "|budget-unknown:" + ",".join(sorted(unknown_worktrees))
    if current_unknown_worktrees:
        fingerprint += _unknown_binding_fingerprint(current_unknown_worktrees)
    released_until = float(state.get("released_until", 0) or 0)
    if (
        state.get("released_fingerprint") == fingerprint
        and state.get("released_session_id") == session_id
        and (
            not unknown_worktrees and not current_unknown_worktrees
            or now < released_until
        )
    ):
        _save_stop_state(cwd, {**state, "ts": now})
        return 0
    count = int(state.get("count", 0)) + 1 if state.get("fingerprint") == fingerprint else 1
    _save_stop_state(cwd, {"ts": now, "fingerprint": fingerprint, "count": count})

    # An escalated PR almost always still has blockers, so it lands in `pending`
    # — surface its escalation explanation (consecutive-failure count + last
    # executor error) ABOVE the generic pending prompt (codex PR #50 review).
    if escalated_owned:
        print(_build_escalation_block(escalated_owned), file=sys.stderr)

    # BOU-2556: pending (confirmed blockers) and unknown_worktrees (budget
    # ran out before they were even examined) are DELIBERATELY rendered as two
    # separate blocks, never merged into one "fix this" instruction set — an
    # unknown PR has nothing confirmed to fix, so telling the operator to
    # "commit + push" it would be actively misleading (item 4 of BOU-2556:
    # a bare subprocess-timeout failure could not make this distinction at
    # all; this gate now can, every time it runs to completion).
    verbose_text = _build_stop_block(pending) if pending else ""
    if unknown_worktrees:
        budget_text = _build_budget_block(
            unknown_worktrees, checked_count=checked_count,
            total=len(owned), pr_for=pr_for,
        )
        verbose_text = f"{verbose_text}\n\n{budget_text}" if verbose_text else budget_text
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
        if pending:
            concise = _build_concise_stop_block(pending, payload_path)
            if unknown_worktrees:
                concise += (
                    f"\n[pr-watch] BUDGET-UNKNOWN: {len(unknown_worktrees)} additional "
                    f"owned PR(s) were NOT checked this tick (checked {checked_count} "
                    f"of {len(owned)}) — see {payload_path}."
                )
        else:
            concise = (
                f"[pr-watch] BUDGET-UNKNOWN: stop-gate budget exhausted — checked "
                f"{checked_count} of {len(owned)} owned PRs; {len(unknown_worktrees)} "
                f"PR(s) UNKNOWN this tick (not confirmed clean). Full detail: "
                f"{payload_path}."
            )
        print(concise, file=sys.stderr)
    else:
        print(verbose_text, file=sys.stderr)

    threshold = _env_int("STOP_LOOP_THRESHOLD", 3)
    if count >= threshold:
        print(
            f"[pr-watch] Same pending/unknown PR state seen {count}× with no "
            f"progress — releasing the stop gate so you can ask the user or "
            f"take a safe action. This exact state stays suppressed until the "
            f"PR head or actionable review/CI state changes.",
            file=sys.stderr,
        )
        released_state: dict[str, object] = {
            "ts": now,
            "released_fingerprint": fingerprint,
            "released_session_id": session_id,
        }
        if unknown_worktrees or current_unknown_worktrees:
            # Unknown is not an exact actionable observation. Suppress only for
            # one bounded cache window, then force another attempt so changed
            # review/CI state on an unchecked PR cannot remain hidden forever.
            released_state["released_until"] = now + max(interval, 1)
        _save_stop_state(cwd, released_state)
        return 0

    if pending:
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
    else:
        print(
            "[pr-watch] Nothing confirmed broken — the PR(s) above are simply "
            "unchecked this tick. Try stopping again shortly so the gate can "
            "examine them within its budget.",
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


def _record_has_blockers(record: dict, *, unknown_state_blocks: bool) -> bool:
    """Does this detached-PR record represent work the CALLER must act on?

    ``record["gh_state_unknown"]`` (set by ``reconcile._unknown_gh_state_record``
    whenever a `gh` probe could not resolve the PR's state at all) is a single
    FACT — "we could not determine this PR's state" — but the two callers of
    this shared predicate mean genuinely different things by "has blockers", so
    one boolean cannot answer both correctly. ``unknown_state_blocks`` is a
    required, explicit policy parameter (no default) precisely so neither call
    site can silently inherit the wrong one:

    * The stop gate (``unknown_state_blocks=True``) must fail CLOSED (P1,
      reconcile-prs used to report every PR as blocker-free during a transient
      gh outage): a session must not idle on a PR whose state could not be
      verified, so an unresolvable probe counts as a blocker exactly like a
      real one.
    * The `await` waiter (``unknown_state_blocks=False``) must NOT treat an
      unresolvable probe as actionable feedback. Before this parameter existed,
      an unknown-state record reached this predicate, returned True, and made
      the waiter ``return 10`` ("Feedback arrived on PR(s) you own — address it
      now") on a transient `gh` outage for a condition that was never actually
      observed — instead of recovering on a later tick once `gh` responds
      again, which is what the waiter's OWN ``unknown_detached`` guard
      (``maintenance_check._cmd_await``) already exists to do. Routing the
      record through this predicate as if it were confirmed feedback bypassed
      that guard entirely (PR #119 review, https://github.com/Boundless-Studios/agentic-pr-dash/pull/119#discussion_r3654167852).
    """
    return bool(
        record["unresolved_threads"]
        or record["ci_failing"]
        or record.get("changes_requested")
        or record.get("merge_conflict")
        or (unknown_state_blocks and record.get("gh_state_unknown"))
    )
