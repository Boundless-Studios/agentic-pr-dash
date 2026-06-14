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
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import github_api

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


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def _is_infra_check(name: str) -> bool:
    return github_api._is_infra_check(name)


# ---------------------------------------------------------------------------
# Adapter invocation (project-specific surfaces, best-effort)
# ---------------------------------------------------------------------------

def _render(template: str, fields: dict[str, str]) -> str:
    out = template
    for key, value in fields.items():
        out = out.replace("{" + key + "}", value)
    return out


def run_adapter(template: str | None, fields: dict[str, str], cwd: Path) -> None:
    """Render ``template`` with ``fields`` and run it via the shell, best-effort.

    A missing template is a no-op. Any failure (non-zero, timeout, OSError) is
    swallowed — adapters are advisory progress mirrors and must never break the
    watcher or block the turn.
    """
    if not template:
        return
    command = _render(template, fields)
    try:
        subprocess.run(
            ["bash", "-lc", command],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


# ---------------------------------------------------------------------------
# git / repo helpers
# ---------------------------------------------------------------------------

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
    from datetime import datetime, timezone
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
             "--limit", "1", "--json", "number"],
            cwd=str(project_dir), capture_output=True, text=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        payload = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        n = payload[0].get("number")
        return n if isinstance(n, int) else None
    return None


# ---------------------------------------------------------------------------
# CI snapshot / poll (by commit SHA — a push targets a specific commit)
# ---------------------------------------------------------------------------

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
    """
    deadline = time.time() + cfg.watch_timeout_s
    time.sleep(cfg.initial_delay_s)
    first_seen_empty: float | None = None
    while time.time() < deadline:
        checks = github_api.get_check_runs_for_commit(sha, str(cfg.project_dir))
        if not checks:
            if first_seen_empty is None:
                first_seen_empty = time.time()
            elif time.time() - first_seen_empty >= NO_CHECKS_GRACE_S:
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
        run_adapter(
            cfg.status_command,
            {"status": "watching", "message": msg, "pr": str(pr_number or ""),
             "sha": sha, "branch": ""},
            cfg.project_dir,
        )

        if not in_progress:
            return checks, POLL_DONE
        time.sleep(cfg.poll_interval_s)
    return [], POLL_TIMEOUT


# ---------------------------------------------------------------------------
# Results file + pid management
# ---------------------------------------------------------------------------

def write_results(cfg: CIWatchConfig, results: dict) -> None:
    cfg.results_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.results_file.write_text(json.dumps(results, indent=2) + "\n")


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


# ---------------------------------------------------------------------------
# Background watcher
# ---------------------------------------------------------------------------

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
    write_results(cfg, {
        "sha": sha, "pr_number": pr_number, "branch": branch,
        "status": "watching", "timestamp": time.time(),
    })
    run_adapter(
        cfg.status_command,
        {"status": "watching", "message": f"CI: waiting for checks... PR #{pr_number}",
         "pr": str(pr_number), "sha": sha, "branch": branch},
        cfg.project_dir,
    )

    checks, outcome = poll_checks_for_commit(sha, cfg, pr_number=pr_number)

    results: dict = {"sha": sha, "pr_number": pr_number, "branch": branch, "timestamp": time.time()}

    if outcome == POLL_TIMEOUT:
        results["status"] = "timeout"
        results["message"] = f"CI timed out on {pr_link(cfg.project_dir, pr_number)} (commit {sha[:8]})"
        results["action_needed"] = True
        write_results(cfg, results)
        run_adapter(
            cfg.complete_command,
            {"status": "timeout", "message": results["message"], "pr": str(pr_number),
             "sha": sha, "branch": branch},
            cfg.project_dir,
        )
        return

    if outcome == POLL_NO_CHECKS:
        # No CI on this commit is not a failure — mirror the foreground snapshot's
        # classification rather than blocking with a bogus timeout.
        results["status"] = "no_checks"
        results["action_needed"] = False
        results["checks_total"] = 0
        results["message"] = (
            f"No CI checks ran on {pr_link(cfg.project_dir, pr_number)} (commit {sha[:8]})"
        )
        comments = _unaddressed_comments(pr_number, sha, cfg.project_dir)
        if comments:
            results["review_comments"] = comments
            results["action_needed"] = True
            results["message"] += f" | {len(comments)} PR review comment(s) to address."
        write_results(cfg, results)
        run_adapter(
            cfg.complete_command,
            {"status": results["status"], "message": results["message"],
             "pr": str(pr_number), "sha": sha, "branch": branch},
            cfg.project_dir,
        )
        return

    # A completed check is a code failure unless its conclusion is a clean pass.
    all_failures = [c for c in checks if isinstance(c, dict)
                    and c.get("status") == COMPLETED_STATUS
                    and c.get("conclusion") not in PASSING_CONCLUSIONS]
    code_failures = [c for c in all_failures if not _is_infra_check(c.get("name", ""))]
    infra_failures = [c for c in all_failures if _is_infra_check(c.get("name", ""))]

    results["checks_total"] = len(checks)
    results["infra_failures"] = [c.get("name", "?") for c in infra_failures]

    if code_failures:
        names = [c.get("name", "?") for c in code_failures]
        results["status"] = "failing"
        results["code_failures"] = names
        results["action_needed"] = True
        logs = github_api.get_failed_logs(sha, names, str(cfg.project_dir))
        if logs:
            results["failed_logs"] = logs
        results["message"] = (
            f"{len(code_failures)} CI check(s) failing: {', '.join(names)}. "
            f"Read the logs, fix the code, commit, and push. {pr_link(cfg.project_dir, pr_number)}"
        )
    else:
        results["status"] = "passing"
        results["action_needed"] = False
        results["message"] = (
            f"All {len(checks)} code CI check(s) passed on {pr_link(cfg.project_dir, pr_number)}"
        )

    comments = _unaddressed_comments(pr_number, sha, cfg.project_dir)
    if comments:
        results["review_comments"] = comments
        results["action_needed"] = True
        results["message"] = results.get("message", "") + (
            f" | {len(comments)} PR review comment(s) to address."
        )

    write_results(cfg, results)
    run_adapter(
        cfg.complete_command,
        {"status": results["status"], "message": results["message"],
         "pr": str(pr_number), "sha": sha, "branch": branch},
        cfg.project_dir,
    )


def _unaddressed_comments(pr_number: int, sha: str, project_dir: Path) -> list[dict]:
    """Unaddressed review comments since the *pushed* commit, as plain dicts.

    Anchors on the pushed ``sha``'s local commit date so a just-pushed fix
    isn't measured against a stale GitHub head. Falls back to the PR's latest
    commit date only when the sha can't be resolved locally."""
    since = commit_date(project_dir, sha)
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
