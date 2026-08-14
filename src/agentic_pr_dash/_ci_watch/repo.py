"""Git/PR helpers: branch, sha, dates, PR number lookup."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .. import github_api


def _git(project_dir: Path, args: list[str], timeout_s: int = 10) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(project_dir), *args],
            capture_output=True, text=True, timeout=timeout_s,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def current_branch(project_dir: Path) -> str:
    return _git(project_dir, ["branch", "--show-current"])


def head_sha(project_dir: Path) -> str:
    return _git(project_dir, ["rev-parse", "HEAD"])


def commit_date(project_dir: Path, sha: str) -> str:
    """UTC ISO (``...Z``) committer date of ``sha`` from the local repo.

    Anchors the review-comment freshness check to the *pushed* commit rather
    than re-fetching the PR's latest commit from GitHub, which can still report
    the previous head for a second or two right after a push (BOU-1479) and
    would resurface comments the just-pushed fix already addressed. Returns ""
    if the sha isn't resolvable locally."""
    if not sha:
        return ""
    out = _git(project_dir, ["show", "-s", "--format=%cI", sha])
    if not out:
        return ""
    # %cI emits the committer's local offset (e.g. ...-07:00); normalize to a
    # ...Z UTC stamp so it sorts lexicographically against GitHub createdAt.
    stamp = out.splitlines()[0].strip()
    return _to_utc_z(stamp)


def _to_utc_z(stamp: str) -> str:
    try:
        dt = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_slug(project_dir: Path) -> str:
    env_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if env_repo and "/" in env_repo:
        return env_repo
    owner, name = github_api.get_repo_info(str(project_dir))
    if owner and name:
        return f"{owner}/{name}"
    return ""


def pr_url(project_dir: Path, pr_number: int | str) -> str:
    slug = repo_slug(project_dir)
    return f"https://github.com/{slug}/pull/{pr_number}" if slug else f"PR #{pr_number}"


def pr_link(project_dir: Path, pr_number: int | str) -> str:
    return f"PR #{pr_number} ({pr_url(project_dir, pr_number)})"


def get_pr_number(branch: str, project_dir: Path) -> int | None:
    if not branch:
        return None
    try:
        r = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "open",
             "--limit", "100", "--json", "number,headRefName"],
            cwd=str(project_dir), capture_output=True, text=True, timeout=20,
            env=github_api.automation_subprocess_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        payload = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if isinstance(payload, list):
        for entry in payload:
            if isinstance(entry, dict) and entry.get("headRefName") == branch:
                n = entry.get("number")
                return n if isinstance(n, int) else None
    return None
