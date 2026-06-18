"""Worktree discovery and ownership helpers."""
from __future__ import annotations

import os
import subprocess

from agentic_pr_dash.config import load as load_config


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
    from agentic_pr_dash import maintenance_check as _mc  # noqa: PLC0415
    owned: list[str] = []
    seen: set[str] = set()
    for root in _maint_roots_for(anchor_cwd):
        for wt in _mc._collect_stop_gate_worktrees(session_id, root):
            if wt not in seen:
                seen.add(wt)
                owned.append(wt)
    return owned


def _detached_records_across_roots(session_id: str, anchor_cwd: str) -> list[dict]:
    """Detached-ledger records across all roots, deduped by ``(root, pr)``."""
    from agentic_pr_dash import maintenance_check as _mc  # noqa: PLC0415
    roots = _maint_roots_for(anchor_cwd)
    prune_legacy = len(roots) <= 1
    records: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for root in roots:
        for r in _mc._detached_pr_records(session_id, root, include_legacy=True,
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
    from agentic_pr_dash import maintenance_check as _mc  # noqa: PLC0415

    candidates = list(dict.fromkeys(os.path.abspath(p) for p in paths if p))
    if not candidates:
        return set()

    self_pids = _mc._self_pid_chain()
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


def _collect_owned_worktrees(
    session_id: str, cwd: str, pid: int | None
) -> list[str]:
    """Return the worktree paths this session owns — markered OR adopted."""
    from agentic_pr_dash import maintenance_check as _mc  # noqa: PLC0415
    cwd = os.path.abspath(cwd)
    if not session_id:
        return []
    eff_pid = pid if pid is not None else _mc._resolve_owner_pid()

    pr_map = _mc._list_my_open_prs(cwd)

    result: list[str] = []
    seen: set[str] = set()

    def _emit(path: str) -> None:
        if path not in seen:
            seen.add(path)
            result.append(path)

    candidates = list(_mc._iter_worktrees_with_branch(cwd))

    independent = _mc._live_independent_owner_paths(
        [path for path, _branch in candidates], session_id
    )

    for worktree_path, branch in candidates:
        abs_path = os.path.abspath(worktree_path)
        if abs_path in independent:
            continue
        if _mc._marker_session_id(worktree_path) == session_id:
            _emit(worktree_path)
            continue
        if not pr_map:
            continue
        pr = pr_map.get(branch)
        if pr is None:
            continue
        number, is_draft = pr
        if is_draft:
            continue
        if _mc._marker_live_foreign_pid(worktree_path, session_id):
            continue
        if _mc._write_arm_marker(worktree_path, session_id, int(eff_pid), int(number)):
            _emit(worktree_path)
    return result


def _collect_stop_gate_worktrees(session_id: str, cwd: str) -> list[str]:
    """Return marker-owned worktrees for passive Stop-hook gating."""
    from agentic_pr_dash import maintenance_check as _mc  # noqa: PLC0415
    cwd = os.path.abspath(cwd)
    if not session_id:
        return []

    candidates = list(_mc._iter_worktrees_with_branch(cwd))
    independent = _mc._live_independent_owner_paths(
        [path for path, _branch in candidates], session_id
    )

    result: list[str] = []
    seen: set[str] = set()
    for worktree_path, _branch in candidates:
        abs_path = os.path.abspath(worktree_path)
        if abs_path in independent:
            continue
        if _mc._marker_session_id(worktree_path) != session_id:
            continue
        if worktree_path not in seen:
            seen.add(worktree_path)
            result.append(worktree_path)
    return result


def _worktree_is_for_entry(path: str, entry) -> bool:
    """True if the worktree at `path` still belongs to this ledger entry's PR."""
    from agentic_pr_dash import maintenance_check as _mc  # noqa: PLC0415
    marker = _mc._read_marker(path) or {}
    if str(marker.get("pr", "")) == str(entry.pr):
        return True
    if not marker:
        return False
    branch = _mc._current_branch(path)
    return bool(branch) and entry.branch and branch == entry.branch
