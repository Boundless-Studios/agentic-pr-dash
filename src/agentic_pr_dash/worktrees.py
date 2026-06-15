"""Git worktree discovery and branch-to-worktree mapping.

The dashboard needs to connect a GitHub PR branch to a local checkout before it
can attribute agents, open terminals, or display ports. This module owns the
``git worktree list`` parsing plus light ``.env`` metadata extraction. It does
not start, stop, or clean worktrees; tools such as worktree-deck own that.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    """Prefer AGENTIC_PR_DASH_<name>, fall back to GAIA_<name>."""
    return os.environ.get("AGENTIC_PR_DASH_" + name) or os.environ.get("GAIA_" + name) or default


def _run(cmd: list[str], timeout_s: int = 10, cwd: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, cwd=cwd)
    except (subprocess.TimeoutExpired, OSError):
        return subprocess.CompletedProcess(cmd, 1, "", "")


def get_main_repo_root() -> str:
    """Get the root of the main (non-worktree) git repo."""
    r = _run(["git", "worktree", "list", "--porcelain"])
    if r.returncode != 0:
        return os.getcwd()
    for line in r.stdout.split("\n"):
        if line.startswith("worktree "):
            return line.split(" ", 1)[1]
    return os.getcwd()


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
