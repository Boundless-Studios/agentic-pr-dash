"""Central configuration for agentic-pr-dash.

Everything that used to be hardcoded to one project — the GitHub repo, the
on-disk state directory, the task tracker, the agent executor, the CI runner
label, the maintenance-prompt wording — is resolved here from (in priority
order):

    1. explicit environment variables (``AGENTIC_PR_DASH_*``)
    2. a project-local ``agentic-pr-dash.toml`` (found by walking up from cwd)
    3. sensible, project-agnostic defaults

Legacy ``GAIA_*`` environment variables and a ``.gaia`` state directory are
still honored as fallbacks so an existing install keeps working, but nothing
in this package *assumes* them.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

ENV_PREFIX = "AGENTIC_PR_DASH_"
LEGACY_ENV_PREFIX = "GAIA_"
CONFIG_FILENAME = "agentic-pr-dash.toml"

DEFAULT_STATE_DIRNAME = ".agentic-pr-dash"
LEGACY_STATE_DIRNAME = ".gaia"
DEFAULT_DISCOVERY_NAMES = ("claude", "codex")
DEFAULT_LEASE_SECONDS = 1800
DEFAULT_HEARTBEAT_TTL_SECONDS = 600

# --- Capability markers (BOU-2086) -----------------------------------------
# Importable, machine-checkable feature flags. Config keys are silently inert on
# an OLD installed snapshot (e.g. ``escalation_failure_threshold`` before
# BOU-1789 shipped) — a consumer setting the key has no way to tell whether the
# engine honors it. External wrappers (gaia's daemon.sh / Stop hook) probe THIS
# set instead of guessing from the package version:
#
#     python -c "from agentic_pr_dash.config import has_capability; \
#                print(has_capability('loop_health_executor_viability'))"
#
# An ImportError/AttributeError from that probe means the snapshot predates the
# marker entirely — treat every listed capability as absent (fail closed).
CAPABILITIES: frozenset[str] = frozenset({
    # BOU-1789: per-PR executor-failure streaks + escalation; the
    # ``escalation_failure_threshold`` config key is honored.
    "escalation_failure_threshold",
    # BOU-2086: the loop persists a heartbeat + executor-viability record and
    # coverage checks (``_detached_loop_alive`` / ``_loop_covers_pr``) require
    # it fresh — pid-alive alone never counts as coverage.
    "loop_health_executor_viability",
})


def has_capability(name: str) -> bool:
    """True when this installed snapshot implements the named capability."""
    return name in CAPABILITIES


def _env(name: str) -> str | None:
    """Read ``AGENTIC_PR_DASH_<name>``, falling back to the legacy ``GAIA_<name>``."""
    return os.environ.get(ENV_PREFIX + name) or os.environ.get(LEGACY_ENV_PREFIX + name)


def _find_config_file(start: Path) -> Path | None:
    """Locate the config file, in priority order:

    1. ``AGENTIC_PR_DASH_CONFIG`` env var (explicit path) — wins everywhere.
    2. a repo-local ``agentic-pr-dash.toml`` (walking up from ``start``).
    3. a global ``~/.config/agentic-pr-dash/config.toml``.

    The global fallback matters because the dashboard (``serve``) runs from an
    arbitrary cwd that may not contain the project config — without it, the
    dashboard would silently fall back to defaults (no tracker, no runner label).
    """
    explicit = os.environ.get(ENV_PREFIX + "CONFIG") or os.environ.get(LEGACY_ENV_PREFIX + "PR_DASH_CONFIG")
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    cur = start.resolve()
    for parent in (cur, *cur.parents):
        candidate = parent / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    global_cfg = Path.home() / ".config" / "agentic-pr-dash" / "config.toml"
    if global_cfg.is_file():
        return global_cfg
    return None


def _load_toml(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


@dataclass(frozen=True)
class Config:
    """Resolved, project-agnostic configuration.

    Construct via :func:`load` (cached) rather than directly so env + file
    resolution happens consistently.
    """

    repo: str | None
    """``owner/name``. ``None`` means "auto-detect from the local git remote"."""

    state_dir: Path
    """Where per-PR maintenance state and ownership markers live."""

    tracker: str
    """Task-tracker adapter name: ``none`` (default), ``beads``, ``github-issues``."""

    executor: str
    """Shell command template the loop runs to fix a PR; ``{prompt}`` is substituted."""

    discovery_names: tuple[str, ...]
    """Process names treated as live agents (for ownership/liveness checks)."""

    runner_label: str | None
    """Self-hosted CI runner label to monitor. ``None`` disables the runner panel."""

    lease_seconds: int
    heartbeat_ttl_seconds: int

    prompt_template: str | None
    """Optional override for the maintenance prompt (path or inline string)."""

    session_registry_path: Path | None
    """Optional session-event log consumed by the dashboard's "agent working" view."""

    await_command: str = "agentic-pr-dash await --cwd {cwd} --session-id {session_id}"
    """Shell command template the stop-gate surfaces when spawning the in-session
    feedback waiter; ``{cwd}`` and ``{session_id}`` are substituted at render time."""

    pr_author: str = "@me"
    """GitHub login whose open PRs the dashboard/maintenance tooling tracks.

    Every PR-discovery path runs ``gh pr list --author <pr_author>``. The
    ``@me`` default is correct when ``gh`` authenticates as the human operator,
    but under an ISOLATED AUTOMATION IDENTITY — a GitHub App installation
    token in ``GH_TOKEN`` (BOU-1923) — ``@me`` resolves to the App bot, which
    authored nothing: the board goes silently empty and waiters see no PR to
    watch. Set this to the operator's login (``pr_author = "ilganeli"``) in
    ``agentic-pr-dash.toml`` (or ``AGENTIC_PR_DASH_PR_AUTHOR``) whenever the
    automation identity differs from the PR author."""

    fallback_executor: str = ""
    """Shell command template the loop runs when the primary :attr:`executor`
    fails for a PR (non-zero exit or a failed spawn); ``{prompt}`` is substituted.
    Empty (default) means no fallback — the loop leaves a failed PR for the next
    tick, as before. Lets a rate-limited primary (e.g. codex) hand off to a
    second agent (e.g. claude) so maintenance keeps flowing (BOU-1734)."""

    maintenance_loop_pidfile: Path | None = None
    """Pidfile of the detached ``pr-maintenance-loop`` daemon. When that process
    is live it is sufficient idle coverage, so the stop-gate skips the fragile
    per-session feedback-waiter prompt (BOU-1653). Resolved from
    ``maintenance_loop_pidfile`` / a ``daemon_dir`` (env or toml); defaults to
    ``~/.claude/daemons/pr-maintenance-loop.pid``."""

    maintenance_loop_machine_wide: bool = False
    """Whether the detached loop services EVERY worktree on the machine (rather
    than a single ``--session-id``/repo scope). Only then is a live loop proof of
    coverage for an arbitrary session, so the stop-gate honors
    ``maintenance_loop_pidfile`` only when this is set. Default ``False`` keeps
    multi-repo/multi-session machines safe — a foreign-scoped loop never
    suppresses another session's waiter (codex PR #21 review)."""

    escalation_failure_threshold: int = 3
    """Number of consecutive executor failures for a PR before escalating.

    When the failure streak reaches this value the loop is considered no longer
    capable of covering the PR autonomously, the stop-gate forces a per-session
    waiter, and an iTerm2 notification is sent. Resolved from:
    ``AGENTIC_PR_DASH_ESCALATION_THRESHOLD`` env > toml ``escalation_failure_threshold`` > 3.
    """

    maintenance_repo_roots: tuple[str, ...] = ()
    """Additional repo MAIN-CHECKOUT paths this (super-)repo services PR
    maintenance for, beyond its own checkout (BOU-1546). The stop-gate /
    list-owned expand ``[anchor] + maintenance_repo_roots`` and run per-root
    ``git worktree list`` discovery; each worktree still resolves its OWN
    per-repo config, so state/markers never bleed across repos. Set in the
    super-repo's ``agentic-pr-dash.toml`` (``maintenance_repo_roots = [...]``)
    or via ``AGENTIC_PR_DASH_MAINTENANCE_REPO_ROOTS`` (comma/``os.pathsep``
    separated). Paths are ``~``-expanded and absolutized at load.

    This is ALSO the repo-selection knob for the PR DASHBOARD (BOU-1598): the
    orchestrator polls ``[anchor] + maintenance_repo_roots`` and aggregates each
    repo's open PRs, tagged by ``owner/name`` so same-number PRs across repos
    don't collide. Zero extra roots ⇒ the dashboard covers only the anchor
    (today's single-repo behavior)."""

    extra: dict = field(default_factory=dict)
    """Anything else from the ``[project]`` table, for adapters to read."""

    # --- derived paths -------------------------------------------------
    # For the *primary* checkout (config-file root). Use the ``*_for(cwd)``
    # variants when operating across sibling worktrees: ownership markers are
    # per-worktree and MUST live under the worktree being checked, not under
    # the config root.
    @property
    def state_dir_name(self) -> str:
        return self.state_dir.name

    @property
    def maintenance_dir(self) -> Path:
        return self.state_dir / "pr-maintenance"

    @property
    def watch_marker(self) -> Path:
        return self.state_dir / "pr-watch.armed"

    @property
    def session_marker(self) -> Path:
        return self.state_dir / "pr-watch.session"

    def state_dir_for(self, cwd: str | Path) -> Path:
        return Path(cwd) / self.state_dir_name

    def watch_marker_for(self, cwd: str | Path) -> Path:
        return self.state_dir_for(cwd) / "pr-watch.armed"

    def session_marker_for(self, cwd: str | Path) -> Path:
        return self.state_dir_for(cwd) / "pr-watch.session"

    def maintenance_dir_for(self, cwd: str | Path) -> Path:
        return self.state_dir_for(cwd) / "pr-maintenance"

    def resolved_repo(self, cwd: Path | None = None) -> str | None:
        """Return ``owner/name``, auto-detecting from ``gh``/git remote if unset."""
        if self.repo:
            return self.repo
        # ``_safe_cwd()`` (not ``Path.cwd()``) so an ambient caller
        # (coordinator._repo_slug_for_pr, maintenance.pr_url(cwd=None)) does not
        # re-raise the deleted-cwd FileNotFoundError that ``load()`` already
        # guards against (BOU-1905 / PR #62 review).
        return _detect_repo(cwd or _safe_cwd())


def _safe_cwd() -> Path:
    """``Path.cwd()`` that tolerates a deleted working directory.

    A long-lived detached process (the waiter) can outlive its cwd when
    stale-worktree reaping removes the directory, so ``os.getcwd()`` raises
    FileNotFoundError. Fall back to $HOME rather than crashing (BOU-1905)."""
    try:
        return Path.cwd()
    except (FileNotFoundError, OSError):
        return Path(os.path.expanduser("~"))


def _detect_repo(cwd: Path) -> str | None:
    """Best-effort ``owner/name`` discovery from the local checkout."""
    try:
        out = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            cwd=cwd, capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    # Fall back to parsing the git remote URL.
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        url = out.stdout.strip()
        if url:
            # git@github.com:owner/name.git  or  https://github.com/owner/name(.git)
            tail = url.split("github.com", 1)[-1].lstrip(":/")
            if tail.endswith(".git"):
                tail = tail[:-4]
            if tail.count("/") == 1:
                return tail
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _resolve_state_dir(file_cfg: dict, base: Path) -> Path:
    raw = _env("STATE_DIR") or file_cfg.get("state_dir")
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (base / p)
    # No explicit config: prefer the modern dir, but adopt a pre-existing
    # legacy ``.gaia`` so current installs don't lose their markers.
    legacy = base / LEGACY_STATE_DIRNAME
    if legacy.is_dir():
        return legacy
    return base / DEFAULT_STATE_DIRNAME


@lru_cache(maxsize=8)
def load(cwd: str | None = None) -> Config:
    """Load and cache the resolved config for ``cwd`` (defaults to the process cwd)."""
    # ``_safe_cwd()`` tolerates a deleted ambient cwd (a long-lived detached
    # waiter can outlive its worktree via stale-worktree reaping), so every
    # ambient-cwd ``load()`` site — agents.py's discovery/cpu helpers among
    # them — inherits the safety net rather than a raw traceback (BOU-1905).
    base = Path(cwd) if cwd else _safe_cwd()
    cfg_path = _find_config_file(base)
    root = cfg_path.parent if cfg_path else base
    data = _load_toml(cfg_path)
    proj = data.get("project", data)  # accept bare keys or a [project] table

    discovery = (
        _env("DISCOVERY_NAMES")
        or ",".join(proj.get("discovery_names", []))
        or ",".join(DEFAULT_DISCOVERY_NAMES)
    )
    discovery_names = tuple(n.strip() for n in discovery.split(",") if n.strip())

    lease = _env("LEASE_SECONDS") or proj.get("lease_seconds")
    heartbeat = _env("HEARTBEAT_TTL_SECONDS") or proj.get("heartbeat_ttl_seconds")

    sess = _env("SESSION_REGISTRY") or proj.get("session_registry_path")

    _default_await_cmd = "agentic-pr-dash await --cwd {cwd} --session-id {session_id}"
    await_command = (
        _env("AWAIT_COMMAND")
        or proj.get("await_command")
        or _default_await_cmd
    )

    pidfile_raw = _env("MAINTENANCE_LOOP_PIDFILE") or proj.get("maintenance_loop_pidfile")
    if pidfile_raw:
        loop_pidfile = Path(pidfile_raw).expanduser()
    else:
        daemon_dir_raw = _env("DAEMON_DIR") or proj.get("daemon_dir")
        daemon_dir = (
            Path(daemon_dir_raw).expanduser()
            if daemon_dir_raw
            else Path.home() / ".claude" / "daemons"
        )
        loop_pidfile = daemon_dir / "pr-maintenance-loop.pid"

    machine_wide_raw = _env("MAINTENANCE_LOOP_MACHINE_WIDE")
    if machine_wide_raw is None:
        machine_wide_raw = proj.get("maintenance_loop_machine_wide")
    machine_wide = str(machine_wide_raw).strip().lower() in {"1", "true", "yes", "on"}

    roots_raw = _env("MAINTENANCE_REPO_ROOTS")
    if roots_raw is None:
        roots_raw = proj.get("maintenance_repo_roots") or []
    if isinstance(roots_raw, str):
        # env form: comma- or os.pathsep-separated
        roots_raw = roots_raw.replace(os.pathsep, ",").split(",")
    seen_roots: set[str] = set()
    maintenance_repo_roots: list[str] = []
    for entry in roots_raw:
        text = str(entry).strip()
        if not text:
            continue
        expanded = os.path.expanduser(text)
        # Resolve a RELATIVE root (e.g. "../agentic-pr-dash") against the config
        # file's directory (``root``), NOT the process cwd — the stop hook / loop
        # may run from anywhere while pointing ``--cwd`` at the super-repo, so a
        # cwd-relative absolutize would land on the wrong path and get skipped
        # (codex PR #30 review, P2).
        if not os.path.isabs(expanded):
            expanded = os.path.join(str(root), expanded)
        ab = os.path.abspath(expanded)
        if ab not in seen_roots:
            seen_roots.add(ab)
            maintenance_repo_roots.append(ab)

    # escalation_failure_threshold: env > toml > 3. Clamp to a positive int and
    # fall back to the default on invalid input — a mistyped value must not make
    # load() raise (breaking every config read), and 0/negative would make
    # _loop_covers_pr treat the live loop as never-covering while _maybe_escalate
    # never fires (codex PR #50 review).
    esc_threshold_raw = (
        os.environ.get("AGENTIC_PR_DASH_ESCALATION_THRESHOLD")
        or os.environ.get("GAIA_PR_WATCH_ESCALATION_THRESHOLD")
        or proj.get("escalation_failure_threshold", 3)
    )
    try:
        esc_threshold = int(esc_threshold_raw)
    except (TypeError, ValueError):
        esc_threshold = 3
    if esc_threshold < 1:
        esc_threshold = 3

    return Config(
        repo=_env("REPO") or proj.get("repo"),
        state_dir=_resolve_state_dir(proj, root),
        tracker=(_env("TRACKER") or proj.get("tracker") or "none").lower(),
        executor=_env("EXECUTOR") or proj.get("executor") or "",
        fallback_executor=_env("FALLBACK_EXECUTOR") or proj.get("fallback_executor") or "",
        await_command=await_command,
        pr_author=_env("PR_AUTHOR") or proj.get("pr_author") or "@me",
        maintenance_loop_pidfile=loop_pidfile,
        maintenance_loop_machine_wide=machine_wide,
        maintenance_repo_roots=tuple(maintenance_repo_roots),
        escalation_failure_threshold=esc_threshold,
        discovery_names=discovery_names,
        runner_label=_env("RUNNER_LABEL") or proj.get("runner_label") or None,
        lease_seconds=int(lease) if lease else DEFAULT_LEASE_SECONDS,
        heartbeat_ttl_seconds=int(heartbeat) if heartbeat else DEFAULT_HEARTBEAT_TTL_SECONDS,
        prompt_template=_env("PROMPT_TEMPLATE") or proj.get("prompt_template") or None,
        session_registry_path=Path(sess).expanduser() if sess else None,
        extra={k: v for k, v in proj.items()},
    )
