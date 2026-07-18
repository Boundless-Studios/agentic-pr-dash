"""Pluggable task-tracker abstraction.

The maintenance flow optionally records a durable "work item" when a PR needs
attention and closes it when the PR is clean again. Different teams track work
differently — some use a dedicated issue tracker, some use GitHub Issues, many
want nothing at all. So tracking lives behind a small protocol with a **no-op
default**; nothing in the core depends on any particular tracker.

Select an adapter via config (``tracker = "none" | "beads" | "github-issues"``)
or ``AGENTIC_PR_DASH_TRACKER``.
"""

from __future__ import annotations

import json
import subprocess
from typing import Protocol, runtime_checkable

from .config import Config


@runtime_checkable
class TaskTracker(Protocol):
    """Minimal contract: open a task for a blocked PR, find it, close it.

    ``cwd`` is the worktree the PR lives in — relevant for per-worktree trackers
    like beads; ignored by trackers that address the repo globally.
    """

    def open_task(self, *, pr: int, branch: str, title: str, body: str, cwd: str | None = None) -> str | None:
        """Create (or return the existing) task for this PR. Returns an opaque id."""
        ...

    def find_task(self, *, pr: int, branch: str, cwd: str | None = None) -> str | None:
        """Return an existing open task id for this PR, or ``None``."""
        ...

    def close_task(self, task_id: str, *, cwd: str | None = None) -> None:
        """Close the task. Best-effort; must not raise on a missing task."""
        ...


class NoOpTracker:
    """Default: track nothing. The maintenance flow works purely off PR state."""

    def open_task(self, *, pr: int, branch: str, title: str, body: str, cwd: str | None = None) -> str | None:
        return None

    def find_task(self, *, pr: int, branch: str, cwd: str | None = None) -> str | None:
        return None

    def close_task(self, task_id: str, *, cwd: str | None = None) -> None:
        return None


class BeadsTracker:
    """Adapter for the ``bd`` (beads) CLI — preserves the original behavior.

    Beads are labeled with the branch short name and titled per PR so a task is
    de-duplicated across runs.
    """

    def __init__(self, label_strip_prefixes: tuple[str, ...] = ("feature/", "fix/", "chore/", "hotfix/", "release/")):
        self._strip = label_strip_prefixes

    def _label(self, branch: str) -> str:
        for p in self._strip:
            if branch.startswith(p):
                branch = branch[len(p):]
                break
        return branch.replace("/", "-")

    def _run(self, args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(["bd", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None

    def find_task(self, *, pr: int, branch: str, cwd: str | None = None) -> str | None:
        out = self._run(["list", "--label", self._label(branch), "--json"], cwd=cwd)
        if out is None or out.returncode != 0:
            return None
        try:
            payload = json.loads(out.stdout)
        except ValueError:
            return None
        if not isinstance(payload, list):
            return None
        title = f"Address PR #{pr} maintenance blockers"
        for item in payload:
            if (
                isinstance(item, dict)
                and item.get("title") == title
                and item.get("status") in {"open", "in_progress"}
                and isinstance(item.get("id"), str)
            ):
                return item["id"]
        return None

    def open_task(self, *, pr: int, branch: str, title: str, body: str, cwd: str | None = None) -> str | None:
        existing = self.find_task(pr=pr, branch=branch, cwd=cwd)
        if existing:
            return existing
        out = self._run([
            "create", title, "--type", "task", "--priority", "1",
            "--labels", self._label(branch),
            "--description", body, "--design", body, "--notes", f"Auto-opened for PR #{pr}.",
            "--json",
        ], cwd=cwd)
        if out is None or out.returncode != 0:
            return None
        try:
            payload = json.loads(out.stdout)
        except ValueError:
            return None
        return payload.get("id") if isinstance(payload, dict) else None

    def close_task(self, task_id: str, *, cwd: str | None = None) -> None:
        self._run(["close", task_id], cwd=cwd)


class GitHubIssuesTracker:
    """Adapter that uses GitHub Issues (via ``gh issue``) as the task backend.

    De-dupes by a hidden marker comment in the body so repeated runs find the
    same issue.
    """

    _MARKER = "<!-- agentic-pr-dash:task pr={pr} -->"

    def __init__(self, repo: str | None = None):
        self._repo = repo

    def _gh(self, args: list[str]) -> subprocess.CompletedProcess:
        from agentic_pr_dash import github_api  # noqa: PLC0415
        full = ["gh", *args]
        if self._repo:
            full += ["--repo", self._repo]
        return subprocess.run(
            full, capture_output=True, text=True, timeout=30,
            env=github_api.automation_subprocess_env(),
        )

    def find_task(self, *, pr: int, branch: str, cwd: str | None = None) -> str | None:
        marker = self._MARKER.format(pr=pr)
        try:
            out = self._gh([
                "issue", "list", "--state", "open", "--search", marker,
                "--json", "number", "-q", ".[0].number",
            ])
        except (OSError, subprocess.SubprocessError):
            return None
        num = out.stdout.strip()
        return num or None

    def open_task(self, *, pr: int, branch: str, title: str, body: str, cwd: str | None = None) -> str | None:
        existing = self.find_task(pr=pr, branch=branch)
        if existing:
            return existing
        marker = self._MARKER.format(pr=pr)
        try:
            out = self._gh([
                "issue", "create", "--title", title,
                "--body", f"{body}\n\n{marker}",
            ])
        except (OSError, subprocess.SubprocessError):
            return None
        url = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
        return url.rsplit("/", 1)[-1] if url else None

    def close_task(self, task_id: str, *, cwd: str | None = None) -> None:
        try:
            self._gh(["issue", "close", task_id])
        except (OSError, subprocess.SubprocessError):
            pass


def get_tracker(config: Config) -> TaskTracker:
    """Return the configured tracker; defaults to the no-op tracker."""
    name = (config.tracker or "none").lower()
    if name in ("none", "off", "disabled", ""):
        return NoOpTracker()
    if name == "beads":
        return BeadsTracker()
    if name in ("github-issues", "github", "gh-issues"):
        return GitHubIssuesTracker(repo=config.repo)
    # Unknown tracker name: fail safe to no-op rather than crashing the flow.
    return NoOpTracker()
