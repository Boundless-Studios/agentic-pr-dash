"""Generic post-push CI-watch primitive.

Extracted from gaia's ``.claude/hooks/post-push-ci-watch.py`` (BOU-1670). The
generic behavior — snapshot ``gh`` check-runs for a pushed commit, spawn a
detached background poller, and write a results JSON file — lives here so BOTH
Claude and Codex route through the same upstream code via
``codex_hooks.run_post_push_watch``.

Project-specific policy (gaia's beads "gate bead", iTerm status bar, the
results-file *location*) stays in the repo as configuration: the watcher takes
an explicit results-file path and two optional shell-command *adapter* templates
(a per-poll ``status`` callback and a final ``complete`` callback) it renders and
invokes. Absent adapters → a clean no-op, so the primitive is agent-agnostic.

The watcher is ADVISORY: it must never block the push or the agent turn. The
foreground entrypoint does a fast snapshot, spawns the detached poller, and
returns immediately.

Implementation is split across ``_ci_watch/`` by responsibility (BOU-1690 WS3).
This module keeps ``arm_post_push_watch``, ``spawn_background_watcher``, and
``main`` and re-exports every other public name so ``ci_watch.X`` keeps
resolving for callers and tests.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from . import github_api  # noqa: F401 – re-exposed as ci_watch.github_api for tests

# ---------------------------------------------------------------------------
# Re-exports from _ci_watch subpackage
# (every name that external code / tests patch via ci_watch.X)
# ---------------------------------------------------------------------------

from ._ci_watch.config import (  # noqa: E402
    POLL_INTERVAL_S,
    INITIAL_DELAY_S,
    PR_RETRY_S,
    WATCH_TIMEOUT_S,
    INFRA_CHECK_PATTERNS,
    COMPLETED_STATUS,
    PASSING_CONCLUSIONS,
    POLL_DONE,
    POLL_NO_CHECKS,
    POLL_TIMEOUT,
    NO_CHECKS_GRACE_S,
    eprint,
    CIWatchConfig,
)

from ._ci_watch.repo import (  # noqa: E402
    _git,
    current_branch,
    head_sha,
    commit_date,
    _to_utc_z,
    repo_slug,
    pr_url,
    pr_link,
    get_pr_number,
)

from ._ci_watch.checks import (  # noqa: E402
    _is_infra_check,
    snapshot_ci,
    poll_checks_for_commit,
)

from ._ci_watch.adapter import (  # noqa: E402
    _render,
    run_adapter,
)

from ._ci_watch.results import (  # noqa: E402
    write_results,
)

from ._ci_watch.watcher import (  # noqa: E402
    _is_our_watcher,
    kill_previous_watcher,
    background_watch,
    _background_watch,
    _unaddressed_comments,
)

# ---------------------------------------------------------------------------
# Foreground entrypoint (fast snapshot + spawn detached watcher)
# ---------------------------------------------------------------------------

def arm_post_push_watch(cfg: CIWatchConfig) -> int:
    """Resolve branch+PR, snapshot CI, spawn the detached background watcher.

    Returns 0 always (advisory). A skip (no branch / no PR / detached HEAD /
    main) is a clean no-op.
    """
    branch = current_branch(cfg.project_dir)
    if branch in ("", "main", "master"):
        return 0

    sha = head_sha(cfg.project_dir)
    if not sha:
        return 0

    pr_number = get_pr_number(branch, cfg.project_dir)
    if pr_number is None:
        deadline = time.time() + cfg.pr_retry_s
        while time.time() < deadline and pr_number is None:
            time.sleep(5)
            pr_number = get_pr_number(branch, cfg.project_dir)
        if pr_number is None:
            eprint("[CI Watch] No PR found, skipping CI watch.")
            return 0

    snap = snapshot_ci(sha, cfg.project_dir)
    eprint(f"[CI Watch] {pr_link(cfg.project_dir, pr_number)} | {sha[:8]} | CI: {snap['message']}")

    kill_previous_watcher(cfg)
    spawn_background_watcher(sha, pr_number, branch, cfg)
    return 0


def spawn_background_watcher(sha: str, pr_number: int, branch: str, cfg: CIWatchConfig) -> int:
    """Spawn this module as a detached ``--background`` poller; record its pid."""
    env = dict(os.environ)
    env["CI_WATCH_PROJECT_DIR"] = str(cfg.project_dir)
    env["CI_WATCH_RESULTS_FILE"] = str(cfg.results_file)
    env["CI_WATCH_PID_FILE"] = str(cfg.pid_file)
    if cfg.status_command:
        env["CI_WATCH_STATUS_COMMAND"] = cfg.status_command
    if cfg.complete_command:
        env["CI_WATCH_COMPLETE_COMMAND"] = cfg.complete_command

    proc = subprocess.Popen(
        [sys.executable, "-m", "agentic_pr_dash.ci_watch",
         "--background", sha, str(pr_number), branch],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    cfg.pid_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.pid_file.write_text(str(proc.pid))
    eprint(f"[CI Watch] Background watcher started (PID {proc.pid})")
    eprint(f"[CI Watch] Results: {cfg.results_file}")
    return proc.pid


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cfg = CIWatchConfig.from_env()
    if args and args[0] == "--background":
        sha = args[1]
        pr_number = int(args[2])
        branch = args[3]
        background_watch(sha, pr_number, branch, cfg)
        return 0
    return arm_post_push_watch(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
