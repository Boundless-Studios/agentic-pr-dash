"""Background watcher lifecycle: pid management and the polling loop body."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .. import github_api
from . import adapter as _adapter
from . import checks as _checks
from . import repo as _repo
from . import results as _results
from .config import (
    POLL_TIMEOUT,
    POLL_NO_CHECKS,
    CIWatchConfig,
)


def _is_our_watcher(pid: int) -> bool:
    """True only if ``pid`` is a live process running this watcher module.

    The pid file can outlive a finished watcher, and the OS may recycle that pid
    for an unrelated process. Before killing, confirm the live process is a
    ``ci_watch`` background poller so we never ``SIGKILL`` an innocent neighbor.
    """
    try:
        r = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if r.returncode != 0:
        return False
    cmdline = r.stdout.strip()
    return "agentic_pr_dash.ci_watch" in cmdline and "--background" in cmdline


def kill_previous_watcher(cfg: CIWatchConfig) -> None:
    """Kill a previous background watcher so stale results don't linger.

    Only kills when the recorded pid is verifiably still THIS watcher module
    (guards against a recycled pid). The pid file is always cleared afterwards.
    """
    if not cfg.pid_file.exists():
        return
    try:
        pid = int(cfg.pid_file.read_text().strip())
    except (ValueError, OSError):
        cfg.pid_file.unlink(missing_ok=True)
        return
    if _is_our_watcher(pid):
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    cfg.pid_file.unlink(missing_ok=True)


def background_watch(sha: str, pr_number: int, branch: str, cfg: CIWatchConfig) -> None:
    """Detached process body: poll CI, fetch logs, check comments, write results.

    Mirrors progress through the configured ``status`` adapter and the terminal
    state through the ``complete`` adapter. The results-file contract is stable
    so a project's stop-gate can read it for final enforcement. The pid file is
    always removed on exit so a finished watcher's stale pid can never be
    ``SIGKILL``ed after the OS recycles it.
    """
    try:
        _background_watch(sha, pr_number, branch, cfg)
    finally:
        cfg.pid_file.unlink(missing_ok=True)


def _background_watch(sha: str, pr_number: int, branch: str, cfg: CIWatchConfig) -> None:
    _results.write_results(cfg, {
        "sha": sha, "pr_number": pr_number, "branch": branch,
        "status": "watching", "timestamp": time.time(),
    })
    _adapter.run_adapter(
        cfg.status_command,
        {"status": "watching", "message": f"CI: waiting for checks... PR #{pr_number}",
         "pr": str(pr_number), "sha": sha, "branch": branch},
        cfg.project_dir,
    )

    checks, outcome = _checks.poll_checks_for_commit(sha, cfg, pr_number=pr_number)

    result: dict = {"sha": sha, "pr_number": pr_number, "branch": branch, "timestamp": time.time()}

    if outcome == POLL_TIMEOUT:
        result["status"] = "timeout"
        result["message"] = f"CI timed out on {_repo.pr_link(cfg.project_dir, pr_number)} (commit {sha[:8]})"
        result["action_needed"] = True
        _results.write_results(cfg, result)
        _adapter.run_adapter(
            cfg.complete_command,
            {"status": "timeout", "message": result["message"], "pr": str(pr_number),
             "sha": sha, "branch": branch},
            cfg.project_dir,
        )
        return

    if outcome == POLL_NO_CHECKS:
        # No CI on this commit is not a failure — mirror the foreground snapshot's
        # classification rather than blocking with a bogus timeout.
        result["status"] = "no_checks"
        result["action_needed"] = False
        result["checks_total"] = 0
        result["message"] = (
            f"No CI checks ran on {_repo.pr_link(cfg.project_dir, pr_number)} (commit {sha[:8]})"
        )
        comments = _unaddressed_comments(pr_number, sha, cfg.project_dir)
        if comments:
            result["review_comments"] = comments
            result["action_needed"] = True
            result["message"] += f" | {len(comments)} PR review comment(s) to address."
        _results.write_results(cfg, result)
        _adapter.run_adapter(
            cfg.complete_command,
            {"status": result["status"], "message": result["message"],
             "pr": str(pr_number), "sha": sha, "branch": branch},
            cfg.project_dir,
        )
        return

    # A completed check is a code failure unless its conclusion is a clean pass.
    from .config import COMPLETED_STATUS, PASSING_CONCLUSIONS
    all_failures = [c for c in checks if isinstance(c, dict)
                    and c.get("status") == COMPLETED_STATUS
                    and c.get("conclusion") not in PASSING_CONCLUSIONS]
    code_failures = [c for c in all_failures if not _checks._is_infra_check(c.get("name", ""))]
    infra_failures = [c for c in all_failures if _checks._is_infra_check(c.get("name", ""))]

    result["checks_total"] = len(checks)
    result["infra_failures"] = [c.get("name", "?") for c in infra_failures]

    if code_failures:
        names = [c.get("name", "?") for c in code_failures]
        result["status"] = "failing"
        result["code_failures"] = names
        result["action_needed"] = True
        logs = github_api.get_failed_logs(sha, names, str(cfg.project_dir))
        if logs:
            result["failed_logs"] = logs
        result["message"] = (
            f"{len(code_failures)} CI check(s) failing: {', '.join(names)}. "
            f"Read the logs, fix the code, commit, and push. {_repo.pr_link(cfg.project_dir, pr_number)}"
        )
    else:
        result["status"] = "passing"
        result["action_needed"] = False
        result["message"] = (
            f"All {len(checks)} code CI check(s) passed on {_repo.pr_link(cfg.project_dir, pr_number)}"
        )

    comments = _unaddressed_comments(pr_number, sha, cfg.project_dir)
    if comments:
        result["review_comments"] = comments
        result["action_needed"] = True
        result["message"] = result.get("message", "") + (
            f" | {len(comments)} PR review comment(s) to address."
        )

    _results.write_results(cfg, result)
    _adapter.run_adapter(
        cfg.complete_command,
        {"status": result["status"], "message": result["message"],
         "pr": str(pr_number), "sha": sha, "branch": branch},
        cfg.project_dir,
    )


def _unaddressed_comments(pr_number: int, sha: str, project_dir: Path) -> list[dict]:
    """Unaddressed review comments since the *pushed* commit, as plain dicts.

    Anchors on the pushed ``sha``'s local commit date so a just-pushed fix
    isn't measured against a stale GitHub head. Falls back to the PR's latest
    commit date only when the sha can't be resolved locally."""
    since = _repo.commit_date(project_dir, sha)
    if not since:
        try:
            _, since = github_api.get_latest_commit(pr_number, str(project_dir))
        except Exception:  # noqa: BLE001 - advisory
            since = ""
    try:
        comments = github_api.get_unaddressed_comments(pr_number, since, str(project_dir))
    except Exception:  # noqa: BLE001 - advisory
        return []
    out: list[dict] = []
    for c in comments:
        out.append({
            "id": getattr(c, "id", 0),
            "author": getattr(c, "author", ""),
            "body": getattr(c, "body", ""),
            "path": getattr(c, "path", None),
            "line": getattr(c, "line", None),
        })
    return out
