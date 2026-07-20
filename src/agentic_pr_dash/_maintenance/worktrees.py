"""Worktree discovery and ownership helpers."""
from __future__ import annotations

import os
import subprocess
import time

from agentic_pr_dash.config import load as load_config
from ._common import _resolve_owner_pid, _current_branch, _repo_slug
from .pr_state import _list_my_open_prs


def _iter_worktrees_with_branch(cwd: str):
    """Yield (path, branch) for non-bare, non-locked worktrees from `git worktree list`."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if result.returncode != 0:
        return

    path: str | None = None
    branch = ""
    bare = False
    locked = False
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
            branch = ""
            bare = False
            locked = False
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        elif line == "bare":
            bare = True
        elif line == "locked" or line.startswith("locked "):
            locked = True
        elif line == "":
            if path and not bare and not locked:
                yield path, branch
            path = None
            branch = ""
            bare = False
            locked = False
    if path and not bare and not locked:
        yield path, branch


def _iter_worktree_paths(cwd: str):
    """Yield abspaths of every worktree in this repo's pool (porcelain)."""
    try:
        out = subprocess.run(["git", "-C", cwd, "worktree", "list", "--porcelain"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return
    if out.returncode != 0:
        return
    for line in out.stdout.splitlines():
        if line.startswith("worktree "):
            yield os.path.abspath(line[len("worktree "):].strip())


def _resolve_maintenance_roots(anchor_cwd: str) -> list[str]:
    """``[anchor] + configured maintenance_repo_roots``, existing git repos only."""
    anchor = os.path.abspath(os.path.expanduser(anchor_cwd))
    cfg = load_config(anchor)
    out: list[str] = []
    seen: set[str] = set()
    for cand in (anchor, *getattr(cfg, "maintenance_repo_roots", ())):
        ab = os.path.abspath(os.path.expanduser(str(cand)))
        if ab in seen:
            continue
        try:
            probe = subprocess.run(
                ["git", "-C", ab, "worktree", "list", "--porcelain"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode != 0:
            continue
        seen.add(ab)
        out.append(ab)
    return out


def _maint_roots_for(anchor_cwd: str) -> list[str]:
    """Roots to service from ``anchor_cwd``, with the anchor ALWAYS included."""
    cwd = os.path.abspath(os.path.expanduser(anchor_cwd))
    resolved = _resolve_maintenance_roots(cwd)
    return resolved if cwd in resolved else [cwd, *resolved]


def _owned_worktrees_across_roots(session_id: str, anchor_cwd: str) -> list[str]:
    """Owned worktrees across ``[anchor] + maintenance_repo_roots`` (deduped)."""
    owned: list[str] = []
    seen: set[str] = set()
    for root in _maint_roots_for(anchor_cwd):
        for wt in _collect_stop_gate_worktrees(session_id, root):
            if wt not in seen:
                seen.add(wt)
                owned.append(wt)
    return owned


def _detached_records_across_roots(session_id: str, anchor_cwd: str) -> list[dict]:
    """Detached-ledger records across all roots, deduped by ``(root, pr)``."""
    from .reconcile import _detached_pr_records  # noqa: PLC0415
    roots = _maint_roots_for(anchor_cwd)
    prune_legacy = len(roots) <= 1
    records: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for root in roots:
        for r in _detached_pr_records(session_id, root, include_legacy=True,
                                      prune_legacy=prune_legacy):
            key = (root, r["pr"])
            if key in seen:
                continue
            seen.add(key)
            records.append(r)
    return records


def _self_pid_chain(max_depth: int = 16) -> set[int]:
    """PIDs of this process and its ancestors (toward init)."""
    pids: set[int] = set()
    pid = os.getpid()
    for _ in range(max_depth):
        if pid <= 1 or pid in pids:
            break
        pids.add(pid)
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True, text=True, timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            break
        parent = out.stdout.strip()
        if not parent.isdigit():
            break
        pid = int(parent)
    return pids


def _live_independent_owner_paths(paths, self_session_id: str) -> set[str]:
    """Subset of ``paths`` where a LIVE INDEPENDENT session is present."""
    from agentic_pr_dash import agents, session_registry  # noqa: PLC0415

    candidates = list(dict.fromkeys(os.path.abspath(p) for p in paths if p))
    if not candidates:
        return set()

    self_pids = _self_pid_chain()
    reg_of = {c: session_registry.registry_path(c) for c in candidates}
    clis_of = {c: set(load_config(c).discovery_names) for c in candidates}
    owned: set[str] = set()

    distinct_regs = {str(reg_of[c]): reg_of[c] for c in candidates}
    index_by_reg: dict[str, dict[str, list]] = {}
    for reg_str, reg_path in distinct_regs.items():
        summary = session_registry.summarize_sessions(path=reg_path)
        idx: dict[str, list] = {}
        for state in summary.sessions.values():
            if state.is_terminal:
                continue
            if state.launch_source in session_registry.DASHBOARD_LAUNCH_SOURCES:
                continue
            if not state.is_feature_pipeline:
                continue
            if self_session_id and state.session_id == self_session_id:
                continue
            if state.pid in self_pids:
                continue
            if not session_registry.pid_is_live(state.pid):
                continue
            if state.worktree_path:
                idx.setdefault(os.path.abspath(state.worktree_path), []).append(state)
        index_by_reg[reg_str] = idx
    for c in candidates:
        states = index_by_reg[str(reg_of[c])].get(c, [])
        if any(s.cli in clis_of[c] for s in states):
            owned.add(c)

    remaining = [p for p in candidates if p not in owned]
    if remaining:
        union_clis: set[str] = set()
        for c in remaining:
            union_clis |= clis_of[c]
        by_path = agents.discover_primary_feature_pipeline_agents(
            remaining, min_cpu=0.0, discovery_names=union_clis
        )
        for path, agent_list in by_path.items():
            abs_path = os.path.abspath(path)
            allow = clis_of.get(abs_path, union_clis)
            if any(a.pid not in self_pids and a.cli_name in allow for a in agent_list):
                owned.add(abs_path)

    return owned


def _marker_pr(worktree_path: str) -> str:
    """The PR number recorded in a worktree's pr-watch.armed marker ('' if none)."""
    from .markers import _read_marker  # noqa: PLC0415
    return str((_read_marker(worktree_path) or {}).get("pr", ""))


def _collect_owned_worktrees(
    session_id: str,
    cwd: str,
    pid: int | None,
    deadline: float | None = None,
    *,
    adopt_unmarked: bool = True,
    adopt_dead_markered: bool = False,
) -> list[str]:
    """Return the worktree paths this session owns — markered OR adopted.

    Two ownership-marker writes can happen here, both forms of reconciliation:
      * ADOPT a truly UNMARKERED worktree (no ``pr-watch.armed`` file at all)
        whose branch has an open non-draft ``@me`` PR with no live
        foreign/independent owner (a missed-arm pickup), and
      * REWRITE a marker we already own whose ``pr`` no longer matches the
        branch's current open non-draft PR (a superseded PR — preflight parity,
        BOU-1787 PR #2337 review).

    Adoption is gated on the worktree having NO existing marker at all — a
    worktree that already carries ANY other session's marker (live OR dead) is
    left untouched here (BOU-1953 root cause #3). ``git worktree list`` from an
    anchor cwd enumerates EVERY worktree sharing that repo, including sibling
    worktrees from unrelated epics/branches a past (now-dead) session armed for
    its own PR. Because ``@me`` PR lookups are keyed on GitHub identity, not
    session, those foreign-but-idle worktrees would otherwise match "branch has
    an open @me PR + no LIVE foreign owner" and get silently re-stamped with
    THIS session's id — over-adopting cross-epic PRs the session never armed or
    touched. Genuine orphan-PR recovery for dead sessions is the ledger-based
    ``_adopt_orphan_prs`` path (registry-gated via ``_session_is_live``), not
    this marker-adoption path; this path is reserved for the missed-arm case
    where no marker was ever written.

    ``adopt_unmarked=False`` disables the missed-arm pickup while keeping already
    markered worktrees visible. This lets multi-repo callers inspect explicit
    ownership in configured sibling roots without claiming unrelated idle PRs
    merely because they belong to ``@me``.

    ``adopt_dead_markered=True`` additionally takes over a worktree whose marker
    names a DEAD session (no fresh heartbeat, no active fix-lease, owner pid
    dead, session not live in the registry) when its branch has an open
    non-draft ``@me`` PR (BOU-2184). Cross-root scans need this: a sub-agent
    arms its own worktree in a sibling ``maintenance_repo_roots`` repo and then
    exits, leaving a foreign-but-dead marker that the BOU-1953 gate below makes
    permanently invisible to every other session — so the sibling repo's PR
    never surfaced in ``list-owned``/``reconcile-prs``. A LIVE foreign owner
    still always blocks takeover. Anchor roots keep this off: same-repo orphan
    recovery stays with the ledger-based ``_adopt_orphan_prs`` path.

    ``_list_my_open_prs`` (a ``gh`` call) is fetched lazily and at most once, and
    only when a candidate actually needs the branch→PR map (an unmarked
    adoptable worktree, or one of ours to staleness-check). ``deadline`` is an
    optional ``time.monotonic()`` budget: the gh probe is skipped once it passes,
    and its subprocess timeout is capped to the remaining budget, so a single
    slow root cannot blow the Stop-hook deadline mid-scan (BOU-1787 review).
    """
    from .markers import (  # noqa: PLC0415
        _live_foreign_owner,
        _live_pr_owner_record,
        _marker_session_id,
        _read_marker,
        _session_is_live,
        _write_arm_marker,
    )
    cwd = os.path.abspath(cwd)
    if not session_id:
        return []
    eff_pid = pid if pid is not None else _resolve_owner_pid()

    result: list[str] = []
    seen: set[str] = set()

    def _emit(path: str) -> None:
        if path not in seen:
            seen.add(path)
            result.append(path)

    candidates = list(_iter_worktrees_with_branch(cwd))

    anchor_abs = os.path.abspath(os.path.expanduser(cwd))

    def _provenance_for(worktree_path: str) -> str:
        """Distinguish a missed-arm pickup from a cross-epic sibling claim (BOU-2221).

        Adopting the session's OWN worktree is the BOU-1787 missed-arm case: the
        session opened this PR and the arm hook simply did not fire, so it really
        does own the work and the stop gate should keep blocking on it.

        Adopting a DIFFERENT worktree is not that. ``@me`` PR lookups key on GitHub
        identity, so every sibling worktree on this machine matches "open @me PR" —
        which is how a bou-2193 session ended up responsible for bou-2195's PRs,
        with the gate demanding it push fixes into a worktree another session was
        actively editing.
        """
        if os.path.abspath(worktree_path) == anchor_abs:
            return "armed"
        return "adopted"

    independent = _live_independent_owner_paths(
        [path for path, _branch in candidates], session_id
    )

    pr_map: dict | None = None  # lazy, budget-bounded gh fetch (at most once)
    repo_slug: str | None = None  # lazy repo detection (at most once)

    def _slug() -> str:
        nonlocal repo_slug
        if repo_slug is None:
            repo_slug = _repo_slug(cwd)
        return repo_slug

    def _branch_prs() -> dict:
        nonlocal pr_map
        if pr_map is None:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                pr_map = {}  # over budget — skip the gh probe entirely
            else:
                pr_map = _list_my_open_prs(
                    cwd, timeout=15 if remaining is None else min(15.0, remaining)
                ) or {}
        return pr_map

    for worktree_path, branch in candidates:
        abs_path = os.path.abspath(worktree_path)
        if abs_path in independent:
            continue
        if _marker_session_id(worktree_path) == session_id:
            # Already ours — but if the marker's PR is stale (the branch's open
            # PR changed since we armed), rewrite it so the Stop-gate re-checks
            # the CURRENT PR even inside the clean-stop window.
            cur = _branch_prs().get(branch)
            if cur is not None and not cur[1] and _marker_pr(worktree_path) != str(cur[0]):
                _write_arm_marker(worktree_path, session_id, int(eff_pid), int(cur[0]))
            _emit(worktree_path)
            continue
        marker = _read_marker(worktree_path)
        if marker:
            # Already carries SOME session's marker (foreign, live or dead —
            # ``_marker_session_id`` above already excluded the "already ours"
            # case). Only an explicit `arm` may claim an already-markered
            # worktree; a shared-repo `git worktree list` scan surfacing one of
            # "my" open GitHub PRs here is not evidence THIS session ever armed
            # it (BOU-1953 root cause #3 — no cross-epic adoption)...
            if not adopt_dead_markered:
                continue
            # ...EXCEPT when the caller opted into dead-owner takeover
            # (cross-root scans, BOU-2184): a marker whose owning session is
            # conclusively dead would otherwise hide this worktree's open
            # non-draft ``@me`` PR from every session forever.
            if _live_foreign_owner(worktree_path, session_id):
                continue
            owner_sid = marker.get("session_id", "")
            if owner_sid and _session_is_live(owner_sid, worktree_path):
                continue
            cur = _branch_prs().get(branch)
            if cur is None:
                continue
            number, is_draft = cur
            if is_draft:
                continue
            # A dead MARKER does not mean the PR is ownerless (PR #82 review,
            # P1): a LIVE session that repointed its worktree elsewhere leaves
            # a dead marker here while still owning this ``(repo, pr)`` in the
            # durable ledger. Overwriting the marker would make the takeover
            # look self-owned to ``_check_worktree`` (which then skips its
            # ``_live_pr_owner_record`` fallback) → concurrent maintenance
            # against a live owner. Defer to any live durable owner.
            if _live_pr_owner_record(
                int(number), _slug(), session_id, worktree_path
            ) is not None:
                continue
            # Dead-marker takeover: inherited, not armed by this session (BOU-2221).
            if _write_arm_marker(
                worktree_path, session_id, int(eff_pid), int(number),
                provenance=_provenance_for(worktree_path),
            ):
                _emit(worktree_path)
            continue
        if not adopt_unmarked:
            continue
        prs = _branch_prs()
        if not prs:
            continue
        pr = prs.get(branch)
        if pr is None:
            continue
        number, is_draft = pr
        if is_draft:
            continue
        if _live_foreign_owner(worktree_path, session_id):
            continue
        # Missed-arm pickup: this session never armed this PR and may know nothing
        # about the work in it, so mark it adopted (BOU-2221).
        if _write_arm_marker(
            worktree_path, session_id, int(eff_pid), int(number),
            provenance=_provenance_for(worktree_path),
        ):
            _emit(worktree_path)
    return result


def _reconcile_owned_across_roots(
    session_id: str, anchor_cwd: str, pid: int | None, deadline: float | None = None
) -> tuple[list[str], list[str]]:
    """Adopting ``list-owned``-equivalent reconciliation across all maintenance
    roots, for Stop-context use.

    Returns ``(owned, newly_adopted)`` where ``newly_adopted`` is the subset of
    ``owned`` whose ownership marker was (re)written on THIS call — either a
    missed-arm PR freshly adopted, OR a marker we already owned whose ``pr`` was
    rewritten because the branch's open PR changed (preflight parity). Both must
    bypass the clean-stop rate-limit so the Stop-gate inspects the current PR.

    Detection avoids changing ``_collect_owned_worktrees``' signature: a worktree
    is newly-adopted if it was NOT markered to us before but is owned after, OR
    if it was markered before but its marker ``pr`` changed across the call.

    ``deadline`` is an optional ``time.monotonic()`` budget — once exceeded,
    remaining roots are skipped so a slow ``gh``/worktree probe can't blow the
    Stop-hook deadline. The anchor root is serviced first.
    """
    owned: list[str] = []
    newly_adopted: list[str] = []
    seen: set[str] = set()
    anchor = os.path.abspath(os.path.expanduser(anchor_cwd))
    for root in _maint_roots_for(anchor_cwd):
        if deadline is not None and time.monotonic() > deadline:
            break
        before = set(_collect_stop_gate_worktrees(session_id, root))
        before_prs = {wt: _marker_pr(wt) for wt in before}
        if os.path.abspath(root) == anchor:
            root_owned = _collect_owned_worktrees(session_id, root, pid, deadline)
        else:
            root_owned = _collect_owned_worktrees(
                session_id,
                root,
                pid,
                deadline,
                adopt_unmarked=False,
                adopt_dead_markered=True,
            )
        for wt in root_owned:
            if wt not in seen:
                seen.add(wt)
                owned.append(wt)
            adopted = wt not in before or before_prs.get(wt) != _marker_pr(wt)
            if adopted and wt not in newly_adopted:
                newly_adopted.append(wt)
    return owned, newly_adopted


def _collect_stop_gate_worktrees(session_id: str, cwd: str) -> list[str]:
    """Return marker-owned worktrees for passive Stop-hook gating."""
    from .markers import _marker_session_id  # noqa: PLC0415
    cwd = os.path.abspath(cwd)
    if not session_id:
        return []

    candidates = list(_iter_worktrees_with_branch(cwd))
    independent = _live_independent_owner_paths(
        [path for path, _branch in candidates], session_id
    )

    result: list[str] = []
    seen: set[str] = set()
    for worktree_path, _branch in candidates:
        abs_path = os.path.abspath(worktree_path)
        if abs_path in independent:
            continue
        if _marker_session_id(worktree_path) != session_id:
            continue
        if worktree_path not in seen:
            seen.add(worktree_path)
            result.append(worktree_path)
    return result


def _worktree_is_for_entry(path: str, entry) -> bool:
    """True if the worktree at `path` still belongs to this ledger entry's PR."""
    from .markers import _read_marker  # noqa: PLC0415
    marker = _read_marker(path) or {}
    if str(marker.get("pr", "")) == str(entry.pr):
        return True
    if not marker:
        return False
    branch = _current_branch(path)
    return bool(branch) and entry.branch and branch == entry.branch
