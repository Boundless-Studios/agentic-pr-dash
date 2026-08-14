"""Git worktree discovery and branch-to-worktree mapping.

The dashboard needs to connect a GitHub PR branch to a local checkout before it
can attribute agents, open terminals, or display ports. This module owns the
``git worktree list`` parsing plus light ``.env`` metadata extraction. It does
not start or stop worktree stacks; cleanup here is limited to conservative
git-worktree removal after callers confirm no active owner is present.
"""

from __future__ import annotations

from datetime import datetime
import os
import platform
import subprocess
from pathlib import Path

from agentic_pr_dash.config import safe_cwd

ZERO_COMMIT_STALE_SECS = 86400
AGENT_STALE_SECS = 3 * 86400
OTHER_STALE_SECS = 7 * 86400
PR_LOOKUP_UNKNOWN = "__PR_LOOKUP_UNKNOWN__"


def _env(name: str, default: str = "") -> str:
    """Prefer AGENTIC_PR_DASH_<name>, fall back to GAIA_<name>."""
    return os.environ.get("AGENTIC_PR_DASH_" + name) or os.environ.get("GAIA_" + name) or default


def _run(cmd: list[str], timeout_s: int = 10, cwd: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, cwd=cwd)
    except (subprocess.TimeoutExpired, OSError):
        return subprocess.CompletedProcess(cmd, 1, "", "")


def _run_text(cmd: list[str], *, cwd: str | None = None, timeout: int = 10) -> str | None:
    result = _run(cmd, timeout_s=timeout, cwd=cwd)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _now_epoch() -> int:
    return int(datetime.now().timestamp())


def get_main_repo_root(root: str | None = None) -> str:
    """Get the root of the main (non-worktree) git repo."""
    # ``str(safe_cwd())`` — the dashboard server outlives the ephemeral worktree it
    # was launched from, and a raw ``os.getcwd()`` then raises FileNotFoundError on
    # every board render for the rest of the process's life (BOU-2193). ``str(...)``
    # keeps the declared -> str contract: orchestrator._maintenance_roots compares
    # this value against a list of str, where a bare Path would never match.
    scan_root = root or str(safe_cwd())
    r = _run(["git", "worktree", "list", "--porcelain"], cwd=scan_root)
    if r.returncode != 0:
        return scan_root
    for line in r.stdout.split("\n"):
        if line.startswith("worktree "):
            return line.split(" ", 1)[1]
    return scan_root


def discover_worktrees(root: str | None = None) -> list[dict]:
    """List all git worktrees with branch, path, and optional .env info.

    Returns list of dicts:
        path: str — absolute worktree path
        branch: str — current branch (or HEAD sha if detached)
        bare: bool — if this is the bare repo entry
        backend_port: str | None — from .env
        frontend_port: str | None — from .env
        environment_name: str | None — from .env
        slot: str | None — from .env

    ``root`` scopes discovery to a single repo: ``git worktree list`` runs with
    ``root`` as its cwd, so it enumerates that repo's worktree pool rather than
    whatever repo the process cwd happens to be in. Passing ``None`` preserves
    the legacy process-cwd behavior (BOU-1720).
    """
    r = _run(["git", "worktree", "list", "--porcelain"], cwd=root)
    if r.returncode != 0:
        return []

    worktrees = []
    current: dict = {}

    for line in r.stdout.split("\n"):
        if line.startswith("worktree "):
            if current:
                worktrees.append(current)
            path = line.split(" ", 1)[1]
            current = {"path": path, "branch": "", "bare": False}
        elif line.startswith("branch "):
            ref = line.split(" ", 1)[1]
            # refs/heads/feature/foo -> feature/foo
            current["branch"] = ref.replace("refs/heads/", "")
        elif line == "bare":
            current["bare"] = True
        elif line.startswith("HEAD "):
            if not current.get("branch"):
                current["branch"] = line.split(" ", 1)[1][:8]  # short sha

    if current:
        worktrees.append(current)

    # Enrich with .env data
    for wt in worktrees:
        if wt.get("bare"):
            continue
        env_path = Path(wt["path"]) / ".env"
        wt.update(_parse_env(env_path))

    return [wt for wt in worktrees if not wt.get("bare")]


def _parse_env(env_path: Path) -> dict:
    """Parse relevant keys from a worktree .env file."""
    result: dict = {
        "backend_port": None,
        "frontend_port": None,
        "environment_name": None,
        "slot": None,
    }
    if not env_path.exists():
        return result
    try:
        text = env_path.read_text()
    except OSError:
        return result

    keys_map = {
        "BACKEND_PORT": "backend_port",
        "FRONTEND_PORT": "frontend_port",
        "ENVIRONMENT_NAME": "environment_name",
        "GAIA_SLOT": "slot",
        "AGENTIC_PR_DASH_SLOT": "slot",
    }
    for line in text.split("\n"):
        line = line.strip()
        if "=" not in line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key in keys_map:
            result[keys_map[key]] = val
    return result


def find_worktree_for_branch(branch: str, root: str | None = None) -> str | None:
    """Find the worktree path for a given branch name.

    ``root`` scopes the search to a single repo's worktree pool (BOU-1720). The
    dashboard is multi-repo: two repos can each have a worktree on a same-named
    branch (e.g. ``feature/x``), so an unscoped search can resolve a PR in repo A
    to repo B's worktree. Passing the PR's repo root restricts ``discover_worktrees``
    to that repo so the match lands in the right checkout. ``None`` preserves the
    legacy process-cwd behavior.
    """
    for wt in discover_worktrees(root=root):
        if wt.get("branch") == branch:
            return wt["path"]
    return None


def find_worktree_for_path(path: str, root: str | None = None) -> dict | None:
    """Find the discovered worktree record for an exact worktree path."""
    target = str(Path(path))
    for wt in discover_worktrees(root=root or path):
        if wt.get("path") == target:
            return wt
    return None


def _is_main_or_protected_worktree(worktree: dict, main_repo: str | None = None) -> bool:
    path = worktree.get("path") or ""
    branch = worktree.get("branch") or ""
    if branch in {"", "main", "master", "detached"}:
        return True
    resolved_main = main_repo or get_main_repo_root(path)
    try:
        return Path(path).resolve() == Path(resolved_main).resolve()
    except OSError:
        return path == resolved_main


def _branch_pr_state(branch: str, main_repo: str) -> str | None:
    if not branch:
        return None
    result = _run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            "1",
            "--json",
            "number,state",
            "--template",
            "{{range .}}{{.state}}{{end}}",
        ],
        cwd=main_repo,
    )
    if result.returncode != 0:
        return PR_LOOKUP_UNKNOWN
    output = result.stdout.strip()
    return output or None


def _worktree_is_dirty(path: str) -> bool:
    output = _run_text(["git", "-C", path, "status", "--porcelain"])
    return bool(output)


def _worktree_branch_stale_reason(path: str, branch: str, main_repo: str) -> str | None:
    fork_point = _run_text(["git", "-C", main_repo, "merge-base", branch, "origin/main"])
    if not fork_point:
        fork_point = _run_text(["git", "-C", main_repo, "merge-base", branch, "main"])
    if not fork_point:
        return None

    raw_count = _run_text(["git", "-C", main_repo, "rev-list", "--count", f"{fork_point}..{branch}"])
    try:
        commit_count = int(raw_count or "0")
    except ValueError:
        return None

    now = _now_epoch()
    if commit_count == 0:
        stat_args = ["stat", "-f", "%B", path] if platform.system() == "Darwin" else ["stat", "-c", "%W", path]
        raw_birth = _run_text(stat_args)
        try:
            birth_epoch = int(raw_birth or "0")
        except ValueError:
            birth_epoch = 0
        if birth_epoch <= 0:
            return None
        age_secs = now - birth_epoch if birth_epoch > 0 else ZERO_COMMIT_STALE_SECS
        if age_secs >= ZERO_COMMIT_STALE_SECS:
            return "orphan with no commits beyond main"
        return None

    raw_last = _run_text(["git", "-C", main_repo, "log", "-1", "--format=%ct", branch])
    try:
        last_epoch = int(raw_last or "0")
    except ValueError:
        return None
    threshold = (
        AGENT_STALE_SECS
        if Path(path).name.startswith(("worktree-agent-", "agent-")) or branch.startswith("worktree-agent-")
        else OTHER_STALE_SECS
    )
    age_secs = now - last_epoch
    if age_secs >= threshold:
        return f"stale orphan ({age_secs // 86400}d old, no PR)"
    return None


def selected_worktree_cleanup_reason(
    worktree: dict,
    active_agents: list[object],
    main_repo: str | None = None,
    *,
    check_remote_pr: bool = True,
) -> tuple[bool, str]:
    """Return whether a worktree is eligible for conservative no-open-PR cleanup."""
    path = worktree.get("path") or ""
    branch = worktree.get("branch") or ""
    resolved_main = main_repo or get_main_repo_root(path)
    if _is_main_or_protected_worktree(worktree, resolved_main):
        return False, "protected worktree"
    if active_agents:
        return False, "active agent detected"
    if _worktree_is_dirty(path):
        return False, "local changes present"

    if check_remote_pr:
        pr_state = _branch_pr_state(branch, resolved_main)
        if pr_state == PR_LOOKUP_UNKNOWN:
            return False, "PR lookup unavailable"
        if pr_state == "OPEN":
            return False, "open PR exists"
        if pr_state in {"MERGED", "CLOSED"}:
            return True, f"{pr_state.lower()} PR branch"
    else:
        # Dashboard rendering already has an authoritative open-PR snapshot.
        # Avoid a second `gh pr list` for every no-PR worktree; only make the
        # conservative local stale/merged determination here. A closed but
        # unmerged recent branch remains non-reclaimable (a safe false negative).
        merged = _run(
            ["git", "-C", resolved_main, "merge-base", "--is-ancestor", branch, "main"]
        )
        if merged.returncode == 0:
            return True, "branch merged into main"

    stale_reason = _worktree_branch_stale_reason(path, branch, resolved_main)
    if stale_reason:
        return True, stale_reason
    return False, "selected worktree is not stale enough"


def _worktree_is_registered(path: str) -> bool:
    """True when git still lists ``path`` as a worktree of its main repo."""
    listed = _run(["git", "-C", get_main_repo_root(path), "worktree", "list", "--porcelain"])
    if listed.returncode != 0:
        # Can't tell. Treat as still-registered so the caller reports failure
        # rather than claiming a removal it did not verify.
        return True
    target = os.path.abspath(path)
    return any(
        os.path.abspath(line[len("worktree ") :].strip()) == target
        for line in (listed.stdout or "").splitlines()
        if line.startswith("worktree ")
    )


def remove_worktree(path: str) -> tuple[bool, str]:
    """Remove a git worktree and report a short failure detail.

    The success post-check asks git whether the worktree is still REGISTERED
    rather than whether the directory exists. A bare ``Path(path).exists()``
    was wrong: the agent-session guardian recreates ``.agent-session-harness/``
    and ``.gaia/`` under the dead path within seconds, so a fully successful
    removal reported ``selected worktree still exists`` — after the checkout was
    already destroyed. That log read as "nothing happened" and the caller
    retried the entry forever (BOU-2933).
    """
    try:
        result = subprocess.run(
            ["git", "-C", get_main_repo_root(path), "worktree", "remove", "--force", path],
            cwd=None,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        return False, str(exc)

    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        return False, output.splitlines()[-1] if output else f"exit {result.returncode}"
    if _worktree_is_registered(path):
        return False, "selected worktree still registered with git"
    return True, ""
