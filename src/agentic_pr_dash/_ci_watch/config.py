"""Module-level constants, CIWatchConfig dataclass, and the eprint helper.

This module is imported by every other _ci_watch submodule — it must have NO
imports from siblings (no cycles).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .. import github_api


POLL_INTERVAL_S = 20
INITIAL_DELAY_S = 15
PR_RETRY_S = 30
WATCH_TIMEOUT_S = 540

INFRA_CHECK_PATTERNS = github_api.INFRA_CHECK_PATTERNS

# A GitHub check run is only finished when its ``status`` is ``completed``;
# every other status (``queued``, ``in_progress``, ``requested``, ``waiting``,
# ``pending``) means CI is still running and we must keep polling. See the
# Checks API docs ("about check runs").
COMPLETED_STATUS = "completed"

# Conclusions that are NOT a clean pass. GitHub treats anything other than
# ``success`` (and the soft ``neutral``/``skipped``) as needing attention:
# ``failure``, ``cancelled``, ``timed_out``, ``action_required``, ``stale``,
# ``startup_failure``. We surface those as blocking.
PASSING_CONCLUSIONS = ("success", "neutral", "skipped")

# Outcomes returned by the poller. ``done`` → all checks completed (caller
# classifies pass/fail); ``no_checks`` → the commit legitimately has no CI (a
# non-blocking terminal state, NOT a timeout); ``timeout`` → still running when
# the deadline elapsed.
POLL_DONE = "done"
POLL_NO_CHECKS = "no_checks"
POLL_TIMEOUT = "timeout"

# How long to keep seeing zero check runs before concluding the commit has no
# CI at all (rather than checks that simply haven't registered yet).
NO_CHECKS_GRACE_S = 90


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CIWatchConfig:
    """Where results land and which project-specific adapters to invoke.

    ``results_file`` / ``pid_file`` are repo-chosen locations (gaia points them at
    ``.claude/ci-watch-latest.json`` / ``.claude/ci-watch.pid``). ``status_command``
    and ``complete_command`` are optional shell-command templates the watcher
    renders with ``{status}`` / ``{message}`` / ``{pr}`` / ``{sha}`` / ``{branch}``
    and runs (best-effort, errors swallowed) so a project can mirror progress into
    its own surface (iTerm status bar, a beads gate bead, …) without the primitive
    knowing about any of it.
    """

    project_dir: Path
    results_file: Path
    pid_file: Path
    status_command: str | None = None
    complete_command: str | None = None
    poll_interval_s: int = POLL_INTERVAL_S
    initial_delay_s: int = INITIAL_DELAY_S
    watch_timeout_s: int = WATCH_TIMEOUT_S
    pr_retry_s: int = PR_RETRY_S
    extra_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "CIWatchConfig":
        """Build from environment variables (the shim's wiring surface).

        ``CI_WATCH_PROJECT_DIR`` (or ``CLAUDE_PROJECT_DIR`` / cwd) anchors the
        relative result/pid defaults. ``CI_WATCH_RESULTS_FILE`` / ``_PID_FILE`` /
        ``_STATUS_COMMAND`` / ``_COMPLETE_COMMAND`` override the defaults.
        """
        e = os.environ if env is None else env
        project = Path(
            e.get("CI_WATCH_PROJECT_DIR")
            or e.get("CLAUDE_PROJECT_DIR")
            or os.getcwd()
        )
        results = e.get("CI_WATCH_RESULTS_FILE")
        pid = e.get("CI_WATCH_PID_FILE")

        def _ival(key: str, default: int) -> int:
            try:
                return int(e[key])
            except (KeyError, ValueError):
                return default

        def _under_project(value: str | None, default: Path) -> Path:
            # A relative override must anchor under the *pushed* worktree
            # (``project_dir``), not the hook/background process cwd — otherwise
            # ``cd wt && git push`` with a relative ``.claude/ci-watch-latest.json``
            # writes results into the wrong worktree and the pushed repo's
            # stop-gate can't read them.
            if not value:
                return default
            p = Path(value)
            return p if p.is_absolute() else project / p

        return cls(
            project_dir=project,
            results_file=_under_project(results, project / ".claude" / "ci-watch-latest.json"),
            pid_file=_under_project(pid, project / ".claude" / "ci-watch.pid"),
            status_command=e.get("CI_WATCH_STATUS_COMMAND") or None,
            complete_command=e.get("CI_WATCH_COMPLETE_COMMAND") or None,
            poll_interval_s=_ival("CI_WATCH_POLL_INTERVAL_S", POLL_INTERVAL_S),
            initial_delay_s=_ival("CI_WATCH_INITIAL_DELAY_S", INITIAL_DELAY_S),
            watch_timeout_s=_ival("CI_WATCH_TIMEOUT_S", WATCH_TIMEOUT_S),
            pr_retry_s=_ival("CI_WATCH_PR_RETRY_S", PR_RETRY_S),
        )
