"""CI check-run snapshot and polling logic."""

from __future__ import annotations

import time
from pathlib import Path

from .. import github_api
from . import adapter as _adapter
from . import config as _config
from .config import (
    COMPLETED_STATUS,
    PASSING_CONCLUSIONS,
    POLL_DONE,
    POLL_NO_CHECKS,
    POLL_TIMEOUT,
    CIWatchConfig,
)


def _is_infra_check(name: str) -> bool:
    return github_api._is_infra_check(name)


def snapshot_ci(sha: str, project_dir: Path) -> dict:
    """Single non-polling CI snapshot for ``sha``. Returns a status dict."""
    checks = github_api.get_check_runs_for_commit(sha, str(project_dir))
    if not checks:
        return {"status": "no_checks", "message": "No CI checks found"}

    # Anything not ``completed`` is still running. Among completed checks, only
    # the soft-pass conclusions count as passing; every other terminal
    # conclusion (failure/cancelled/timed_out/action_required/stale/...) blocks.
    pending = [c for c in checks if c.get("status") != COMPLETED_STATUS]
    completed = [c for c in checks if c.get("status") == COMPLETED_STATUS]
    failing = [c for c in completed if c.get("conclusion") not in PASSING_CONCLUSIONS]
    passed = [c for c in completed if c.get("conclusion") in PASSING_CONCLUSIONS]

    code_failures = [c for c in failing if not _is_infra_check(c.get("name", ""))]
    infra_failures = [c for c in failing if _is_infra_check(c.get("name", ""))]

    if code_failures:
        names = [c.get("name", "?") for c in code_failures]
        return {"status": "failing", "message": f"{len(code_failures)} failing: {', '.join(names)}",
                "code_failures": names}
    if pending:
        return {"status": "pending",
                "message": f"{len(passed)}/{len(checks)} passing, {len(pending)} pending",
                "infra_failures": [c.get("name", "?") for c in infra_failures]}
    return {"status": "passing", "message": f"All {len(checks)} check(s) passing",
            "infra_failures": [c.get("name", "?") for c in infra_failures]}


def poll_checks_for_commit(
    sha: str,
    cfg: CIWatchConfig,
    pr_number: int | None = None,
) -> tuple[list[dict], str]:
    """Poll check-runs for ``sha`` until they complete, vanish, or timeout.

    Emits the optional ``status`` adapter on each poll so a project can mirror
    live progress. A check run is finished only when ``status == "completed"``;
    every other status (queued/in_progress/requested/waiting/pending) keeps the
    poll alive. Returns ``(checks, outcome)`` where ``outcome`` is one of
    :data:`POLL_DONE`, :data:`POLL_NO_CHECKS`, :data:`POLL_TIMEOUT`.

    If the commit reports zero check runs for ``NO_CHECKS_GRACE_S`` (a repo
    without CI, or a commit whose workflows create no checks), the poll returns
    ``POLL_NO_CHECKS`` instead of spinning to a blocking timeout.

    Note on NO_CHECKS_GRACE_S: read via the ``_config`` module reference so
    that ``monkeypatch.setattr(_config, "NO_CHECKS_GRACE_S", ...)`` takes
    effect in tests (patching the module attribute, not the local name copy).
    """
    deadline = time.time() + cfg.watch_timeout_s
    time.sleep(cfg.initial_delay_s)
    first_seen_empty: float | None = None
    while time.time() < deadline:
        checks = github_api.get_check_runs_for_commit(sha, str(cfg.project_dir))
        if not checks:
            if first_seen_empty is None:
                first_seen_empty = time.time()
            elif time.time() - first_seen_empty >= _config.NO_CHECKS_GRACE_S:
                return [], POLL_NO_CHECKS
            time.sleep(cfg.poll_interval_s)
            continue
        first_seen_empty = None
        in_progress = [c for c in checks if c.get("status") != COMPLETED_STATUS]
        done = [c for c in checks if c.get("status") == COMPLETED_STATUS]
        failed = [c for c in done if c.get("conclusion") not in PASSING_CONCLUSIONS]
        passed = [c for c in done if c.get("conclusion") in PASSING_CONCLUSIONS]

        pending_names = ", ".join(c.get("name", "?") for c in in_progress[:2])
        if failed:
            fail_names = ", ".join(c.get("name", "?") for c in failed)
            msg = f"CI: {fail_names} FAILED | {len(passed)} ok, {len(in_progress)} running"
        elif in_progress:
            msg = f"CI: {len(passed)}/{len(checks)} ok | running: {pending_names}"
        else:
            msg = f"CI: {len(passed)}/{len(checks)} ok"
        _adapter.run_adapter(
            cfg.status_command,
            {"status": "watching", "message": msg, "pr": str(pr_number or ""),
             "sha": sha, "branch": ""},
            cfg.project_dir,
        )

        if not in_progress:
            return checks, POLL_DONE
        time.sleep(cfg.poll_interval_s)
    return [], POLL_TIMEOUT
