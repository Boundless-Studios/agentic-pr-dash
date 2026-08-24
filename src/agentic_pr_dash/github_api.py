"""GitHub state collection for PR maintenance and dashboard rendering.

This module is the boundary around GitHub. It shells out to ``gh`` / ``git`` and
uses GitHub REST or GraphQL where needed, then converts raw responses into
package models such as ``CICheck`` and ``ReviewComment``. Higher layers should
ask this module for PR state instead of parsing GitHub output themselves.

Responsibilities include PR lookup, mergeability, CI checks, review threads,
failed-log snippets, changed files, and self-hosted runner health. Comment
filtering is commit-aware so already-addressed review feedback does not keep
reappearing as live work.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

try:  # POSIX file locking for the snapshot refresh (unix-only; degrade gracefully)
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Generic, TypeVar

from .config import load as load_config
from .models import (
    CICheck,
    QueuedWorkflowJob,
    ReviewComment,
    RunnerExecutionSummary,
    RunnerPoolHealth,
    ThreadDecision,
)
from .quota import QuotaCaller, QuotaContext, QuotaLedger, QuotaWorkClass


ObservationValue = TypeVar("ObservationValue")


@dataclass(frozen=True, slots=True)
class ObservationReadResult(Generic[ObservationValue]):
    """Result of a dashboard observation boundary read.

    ``observable=False`` means the boundary could not establish the requested
    slice.  An observable empty value is therefore represented by
    ``ObservationReadResult(value=[], observable=True)`` rather than being
    conflated with an unavailable read.  The dashboard uses this type to stage
    values before touching ``PRData``; other callers keep the historical
    fail-open sequence APIs below.
    """

    value: ObservationValue | None
    observable: bool
    error: str | None = None
    graphql_observed: bool = False

    @classmethod
    def observed(
        cls,
        value: ObservationValue,
        *,
        graphql_observed: bool = False,
    ) -> ObservationReadResult[ObservationValue]:
        return cls(
            value=value,
            observable=True,
            graphql_observed=graphql_observed,
        )

    @classmethod
    def unavailable(
        cls,
        error: str,
        value: ObservationValue | None = None,
        *,
        graphql_observed: bool = False,
    ) -> ObservationReadResult[ObservationValue]:
        return cls(
            value=value,
            observable=False,
            error=error,
            graphql_observed=graphql_observed,
        )


@dataclass(frozen=True, slots=True)
class ConditionalPRListProbe:
    """The result of a conditional REST open-PR metadata probe.

    The dashboard uses this endpoint as a cheap validator for its rich
    ``gh pr list`` snapshot.  A ``304`` is an authoritative statement that the
    cached metadata is still current; a ``200`` means the validator changed
    and the caller may choose to spend a GraphQL-rich relist.  Keeping the
    response typed prevents a failed probe from being confused with an empty
    open-PR list (which would otherwise prune the dashboard).
    """

    status_code: int | None
    prs: list[dict]
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None
    #: Whether the underlying REST page was full, i.e. there may be open PRs
    #: this response never saw. The endpoint is repo-wide and paged, and the
    #: author filter runs *after* the fetch, so on a repo with more than one
    #: page of open PRs the configured author's older PR can fall outside it.
    #: A truncated body is still a fine cache validator, but it is NOT an
    #: authoritative open set and must never drive pruning (BOU-3095 PR #169).
    truncated: bool = False

    @property
    def observable(self) -> bool:
        return self.status_code in {200, 304} and self.error is None

    @property
    def not_modified(self) -> bool:
        return self.status_code == 304 and self.error is None

    @property
    def changed(self) -> bool:
        return self.status_code == 200 and self.error is None


class _ObservedList(list):
    """List-compatible legacy return carrying boundary observability."""

    observable: bool
    error: str | None

    def __init__(
        self,
        values: list,
        *,
        observable: bool,
        error: str | None = None,
    ) -> None:
        super().__init__(values)
        self.observable = observable
        self.error = error


class _BatchObservationDict(dict[int, dict]):
    """Dict-compatible batch result carrying quota admission state."""

    denied: bool
    error: str | None

    def __init__(self) -> None:
        super().__init__()
        self.denied = False
        self.error = None


def _runner_label() -> str | None:
    """Return the configured self-hosted runner label, or None if the runner panel is disabled."""
    return load_config().runner_label

INFRA_CHECK_PATTERNS = {"tofu", "terraform", "infrastructure"}
LOG_TAIL_LINES = 40
CLAIM_MARKER = "<!-- agentic-pr-dash:claimed -->"
COMPLETE_MARKER = "<!-- agentic-pr-dash:completed -->"
FAILED_MARKER = "<!-- agentic-pr-dash:push-failed -->"
STALE_CLAIM_SECONDS = 60 * 60
QUEUE_WARNING_SECONDS = 2 * 60
WEEKLY_RUNNER_JOB_FETCH_WORKERS = 8
WEEKLY_RUNNER_RUN_QUERY_DAYS = 1
RUNNER_SUMMARY_CACHE = Path.home() / ".cache" / "agentic-pr-dash" / "runner-summary.json"
_RUN_ID_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/actions/runs/(\d+)(?:[/?#]|$)")
_CODEX_CLEAN_REVIEW_RE = re.compile(
    r"^\s*Codex Review:\s*Didn't find any major issues\.\s*(?::rocket:|🚀)?",
    re.IGNORECASE,
)
_CODEX_REVIEW_AUTHOR_KEY = "chatgpt-codex-connector"
_REVIEWED_COMMIT_RE = re.compile(
    r"\*\*Reviewed commit:\*\*\s*`([0-9a-f]{7,40})`",
    re.IGNORECASE,
)

_REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 100) {
            nodes {
              databaseId
              path
              line
              originalLine
              body
              author { login }
              createdAt
              pullRequestReview { databaseId }
            }
          }
        }
      }
    }
  }
}
""".strip()

_RESOLVE_THREAD_MUTATION = "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }"


@dataclass(frozen=True)
class ReviewThreadComment:
    database_id: int
    path: str | None
    line: int | None
    body: str
    author: str
    created_at: str
    # GitHub's `originalLine` — the anchor line at the commit the comment was
    # made on. For OUTDATED threads GitHub nulls `line` (it can no longer track
    # the anchor), so this is the only line evidence left (BOU-2095).
    original_line: int | None = None
    review_id: int | None = None


@dataclass(frozen=True)
class ReviewThread:
    node_id: str
    is_resolved: bool
    is_outdated: bool
    top: ReviewThreadComment
    replies: list[ReviewThreadComment] = field(default_factory=list)


@dataclass(frozen=True)
class ReviewSubmission:
    """One completed GitHub review against an immutable PR head."""

    review_id: int
    author: str
    state: str
    commit_id: str
    submitted_at: str
    body: str = ""
    source: str = "review"


def _login_key(login: str) -> str:
    """Canonicalize GitHub App and REST bot spellings for comparisons."""

    key = login.strip().casefold()
    if key.startswith("app/"):
        key = key[len("app/"):]
    if key.endswith("[bot]"):
        key = key[:-len("[bot]")]
    return key


# Bounded retry for transient connectivity failures (BOU-1638 / BOU-1694).
# A gh call that can't *reach* GitHub (DNS / connect / TLS / read timeout, or a
# transient 5xx) typically succeeds moments later — the interactive shell's gh
# works while a single Python-subprocess attempt happened to hit a blip. Making
# the reach itself robust (not just diagnosable) is the point: we retry, and
# only surface the diagnostics after the retries are exhausted. Tunables are env
# overridable so tests can drive them deterministically.
_GH_RETRY_ATTEMPTS = max(1, int(os.environ.get("APD_GH_RETRY_ATTEMPTS", "3")))
_GH_RETRY_BASE_DELAY_S = float(os.environ.get("APD_GH_RETRY_BASE_DELAY_S", "0.5"))

# Substrings that identify a CONNECTION-ESTABLISHMENT / transport failure — i.e.
# the request almost certainly never reached GitHub, so re-attempting is safe
# even for the resolveReviewThread mutation. Auth errors, bad args, and "no PRs"
# are deliberately excluded: retrying those just wastes wall-clock.
_CONNECTIVITY_STDERR_PATTERNS = (
    "error connecting to",
    "could not resolve host",
    "no such host",
    "temporary failure in name resolution",
    "network is unreachable",
    "connection refused",
    "connection reset",
    "connection timed out",
    "i/o timeout",
    "client.timeout exceeded",
    "timeout awaiting response headers",
    "tls handshake timeout",
    # NB: our own full-duration TimeoutExpired wrapper ("gh timed out after Ns")
    # is deliberately NOT retried — re-running a hung gh would multiply the
    # worst-case stop-gate latency by the attempt count. Only fast-failing
    # transport errors (connect/DNS/reset/5xx) above are worth a re-attempt.
    "503 service unavailable",
    "502 bad gateway",
    "504 gateway time",
)


def _is_transient_connectivity_failure(result: subprocess.CompletedProcess) -> bool:
    """True when a failed gh result looks like a transient transport failure
    that is worth retrying (the request never reached GitHub)."""
    if result.returncode == 0:
        return False
    stderr = (result.stderr or "").lower()
    return any(pat in stderr for pat in _CONNECTIVITY_STDERR_PATTERNS)


# GitHub rate-limit signatures (BOU-1921). Two distinct modes: PRIMARY quota
# exhaustion ("API rate limit exceeded") and the velocity-triggered SECONDARY /
# abuse limit ("secondary rate limit" / "submitted too quickly"). A shared dev
# identity drains these constantly on heavy review days; the waiter is a
# long-lived poller, so a rate-limited call should back off and retry rather
# than surface as a hard failure that misfires the poll loop.
_RATE_LIMIT_STDERR_PATTERNS = (
    "rate limit exceeded",
    # GraphQL's primary-quota phrasing differs from REST's: "GraphQL: API rate
    # limit already exceeded" — the word "already" defeats the substring above,
    # which made the exact BOU-1966 symptom unclassifiable as a rate-limit.
    "rate limit already exceeded",
    "secondary rate limit",
    "exceeded a secondary rate",
    "submitted too quickly",
    "retry-after",
)

# Bounded rate-limit backoff. Reuses the connectivity attempt budget, but each
# sleep is CAPPED so a latency-sensitive caller (the stop gate) never wedges on
# a primary-exhaustion reset that can be up to an hour away — the common case is
# a secondary limit that clears in seconds. The long-lived waiter tolerates
# residual starvation by staying alive across ticks (see maintenance_check).
_GH_RATELIMIT_MAX_SLEEP_S = float(os.environ.get("APD_GH_RATELIMIT_MAX_SLEEP_S", "10"))

# Process-level rate-limit-backoff toggle (BOU-1953). The waiter (long-lived
# poller) benefits from backing off and retrying a rate-limited call — it can
# afford to wait. The stop-gate is a Stop-hook subprocess with a hard ~108s
# deadline that fails CLOSED when exceeded; when a session owns several PRs and
# the shared gh quota is exhausted, EVERY gh call in that run would each
# accumulate up to `_GH_RATELIMIT_MAX_SLEEP_S` of backoff, easily blowing the
# deadline. The stop-gate disables this at process start (see
# `_stop_gate_impl`) so a rate-limited call fails fast instead. Defaults to
# enabled (today's behavior); also honors `APD_GH_NO_RATE_LIMIT_BACKOFF=1` as a
# process-start default for callers that prefer an env var over the setter.
_RATE_LIMIT_BACKOFF_ENABLED = os.environ.get(
    "APD_GH_NO_RATE_LIMIT_BACKOFF", ""
).strip().lower() not in ("1", "true", "yes")


def set_rate_limit_backoff(enabled: bool) -> None:
    """Enable/disable the rate-limit backoff-and-retry behavior in `_run`.

    When disabled, a rate-limited `gh` call returns immediately (no sleep, no
    retry) instead of backing off up to `_GH_RATELIMIT_MAX_SLEEP_S` per call —
    `_RATE_LIMIT_SEEN` is still recorded so tick-based callers keep working, and
    the (short, unrelated) connectivity retry is unaffected.
    """
    global _RATE_LIMIT_BACKOFF_ENABLED
    _RATE_LIMIT_BACKOFF_ENABLED = enabled


def rate_limit_backoff_enabled() -> bool:
    """True iff `_run` currently backs off and retries rate-limited calls."""
    return _RATE_LIMIT_BACKOFF_ENABLED


def _is_rate_limit_failure(result: subprocess.CompletedProcess) -> bool:
    """True when a failed gh result is a primary or secondary rate-limit."""
    if result.returncode == 0:
        return False
    stderr = (result.stderr or "").lower()
    return any(pat in stderr for pat in _RATE_LIMIT_STDERR_PATTERNS)


def _retry_after_seconds(stderr: str) -> float | None:
    """Parse a ``Retry-After: N`` hint (seconds) from stderr, if present."""
    match = re.search(r"retry-after:\s*(\d+)", stderr or "", re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _rate_limit_backoff_seconds(stderr: str, attempt: int) -> float:
    """Backoff for a rate-limited attempt: honor Retry-After when parseable,
    else exponential — always clamped to the cap so latency stays bounded."""
    hint = _retry_after_seconds(stderr)
    delay = hint if hint is not None else _GH_RETRY_BASE_DELAY_S * (2 ** attempt)
    return min(delay, _GH_RATELIMIT_MAX_SLEEP_S)


# Per-tick rate-limit observation (BOU-1921 #62). A tick-based consumer (the
# await waiter) calls reset_rate_limit_seen() at the start of each tick and reads
# rate_limit_seen() after ALL its gh calls, so it reflects a rate-limit hit by
# ANY call in the tick — list_open_prs, get_ci_checks, review-thread reads,
# watch-pending probes — not just the initial PR list. This is the single source
# of truth for "GitHub was quota-limited this tick", replacing the brittle
# state=="unknown" / last-list-failure heuristics that conflated a HARD failure
# (missing gh / auth / bad JSON) with a real quota wall.
_RATE_LIMIT_SEEN = False

# Monotonic count of gh calls that ULTIMATELY failed on a rate-limit (i.e.
# after `_run`'s bounded retries). Unlike `_RATE_LIMIT_SEEN` it is NEVER reset:
# span-based callers (the PR resolvers' detail-fetch guard, BOU-1966) compare
# before/after counts to detect "a rate-limit happened during THIS span" even
# when the tick-scoped flag was already set before the span began (e.g. by the
# failed author-list call that routed them onto the REST fallback path).
_RATE_LIMIT_EVENTS = 0


def reset_rate_limit_seen() -> None:
    """Clear the rate-limit-seen flag (call at the start of a poll tick)."""
    global _RATE_LIMIT_SEEN
    _RATE_LIMIT_SEEN = False


def rate_limit_seen() -> bool:
    """True if any gh call since the last reset ultimately failed on a rate-limit."""
    return _RATE_LIMIT_SEEN


def rate_limit_events() -> int:
    """Monotonic count of rate-limited gh failures in this process (never reset).

    Compare across a span of gh calls to detect a rate-limit DURING that span
    regardless of the tick-scoped :func:`rate_limit_seen` state (BOU-1966)."""
    return _RATE_LIMIT_EVENTS


# Per-tick CI-watch-probe observation (codex PR #75 review, BOU-1962).
# ``required_checks_pending`` is deliberately fail-safe: it returns False on any
# gh/parse error so callers that merely PRIORITIZE on it never wedge. But the
# await waiter's clean-state early exit needs a POSITIVE terminal-CI
# observation — a failed probe returning False must not read as "CI terminal"
# or the waiter could exit while a required check is still queued/in_progress
# behind a non-rate-limit gh outage. Consumers reset this flag at the start of
# a tick and read it after their probes, mirroring rate_limit_seen().
_CHECKS_PROBE_FAILURE_SEEN = False


def reset_checks_probe_failure_seen() -> None:
    """Clear the checks-probe-failure flag (call before a tick's CI probes)."""
    global _CHECKS_PROBE_FAILURE_SEEN
    _CHECKS_PROBE_FAILURE_SEEN = False


def checks_probe_failure_seen() -> bool:
    """True if a required-checks probe since the last reset failed (state unobservable)."""
    return _CHECKS_PROBE_FAILURE_SEEN


def _note_checks_probe_failure() -> None:
    global _CHECKS_PROBE_FAILURE_SEEN
    _CHECKS_PROBE_FAILURE_SEEN = True


# ── Automation-token file source (BOU-1991) ────────────────────────────────
# The PR-automation GitHub App installation token lives in
# $XDG_CONFIG_HOME/agentic-pr-dash/gh-automation-token and rotates roughly
# every 45 minutes (1-hour TTL). Long-lived processes (the dashboard server,
# the maintenance loop) previously inherited GH_TOKEN once at spawn, which
# forced their supervisor to RESTART them on every rotation — dropping all
# in-memory poll state, user-visible as CI state vanishing from the board for
# the first poll cycle after each restart. Instead, source the token from the
# file per gh invocation: one stat() per call, re-reading only on mtime
# change, so a rotation is picked up mid-flight with no restart.
_TOKEN_FILE_CACHE: tuple[float, str] | None = None  # (mtime, token)


def _automation_token_file() -> str:
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(config_home, "agentic-pr-dash", "gh-automation-token")


def _read_automation_token() -> str | None:
    """Current token from the rotating token file, mtime-cached.

    Returns ``None`` when the file is absent/unreadable/empty. Rotation always
    replaces the file atomically (fresh mtime), so an unchanged mtime means an
    unchanged token by contract and the cached value is served without a read.
    """
    global _TOKEN_FILE_CACHE
    path = _automation_token_file()
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        _TOKEN_FILE_CACHE = None
        return None
    if _TOKEN_FILE_CACHE is not None and _TOKEN_FILE_CACHE[0] == mtime:
        return _TOKEN_FILE_CACHE[1] or None
    try:
        with open(path, encoding="utf-8") as fh:
            token = fh.readline().strip()
    except OSError:
        _TOKEN_FILE_CACHE = None
        return None
    _TOKEN_FILE_CACHE = (mtime, token)
    return token or None


# Sticky verdict for the initial-match opt-in heuristic (see
# `_automation_identity_opted_in`). Memoized on first use so a later rotation
# cannot flip a caller-provided PAT into "automation" mid-process.
_SPAWN_TOKEN_WAS_AUTOMATION: bool | None = None


def _automation_identity_opted_in() -> bool:
    """True when this process's ``GH_TOKEN`` is the supervisor-provided
    automation identity, so following the rotating token file is safe.

    Merely having ``GH_TOKEN`` set is NOT enough (PR #73 review): a shell or
    CI job may export its own PAT while the automation token file happens to
    exist on the machine — silently replacing that PAT would change the
    authenticated identity of every gh call. Opt-in signals, strongest first:

    1. ``AGENTIC_PR_DASH_TOKEN_FROM_FILE=1`` — the explicit marker exported by
       supervisors that sourced ``GH_TOKEN`` from the token file
       (gh-automation-token.sh since BOU-1991).
    2. Sticky initial match: the spawn-time ``GH_TOKEN`` equals the file's
       token the FIRST time this is consulted. A caller-provided PAT never
       matches the file; covers supervisors predating the marker. Memoized so
       the verdict is stable for the process lifetime (the file rotating
       AFTER a match is exactly the case we refresh for; a rotation BETWEEN
       spawn and first gh call under a marker-less supervisor is a benign
       miss — behavior degrades to the legacy inherit-spawn-env).
    """
    global _SPAWN_TOKEN_WAS_AUTOMATION
    if os.environ.get("AGENTIC_PR_DASH_TOKEN_FROM_FILE") == "1":
        return True
    if _SPAWN_TOKEN_WAS_AUTOMATION is None:
        spawn_token = os.environ.get("GH_TOKEN")
        _SPAWN_TOKEN_WAS_AUTOMATION = bool(spawn_token) and spawn_token == _read_automation_token()
    return _SPAWN_TOKEN_WAS_AUTOMATION


def automation_subprocess_env(cmd: list[str] | None = None) -> dict[str, str] | None:
    """Env for a gh subprocess with a fresh automation token, or ``None`` to inherit.

    Public entry point for DIRECT ``subprocess.run(["gh", ...])`` call sites
    that bypass `_run` (loop baseline lookups, maintenance pr_state, config
    repo detection, ...) — long-lived processes must route gh spawns through
    this or they keep the stale spawn-time token after a rotation (PR #73
    review). Pass the command to no-op safely for non-gh spawns.

    Returns ``None`` (inherit the process env unchanged) unless this process
    opted into the automation identity AND the token file currently holds a
    token different from the spawn-time env value.
    """
    if cmd is not None and (not cmd or os.path.basename(cmd[0]) != "gh"):
        return None
    spawn_token = os.environ.get("GH_TOKEN")
    if not spawn_token:
        return None
    if not _automation_identity_opted_in():
        return None
    token = _read_automation_token()
    if token is None or token == spawn_token:
        return None
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    return env


def _automation_env_refresh(cmd: list[str]) -> dict[str, str] | None:
    """`_run_once`'s hook: fresh-token env for gh commands, ``None`` to inherit.

    Explicit-env callers (`_resolve_fallback_env`) never reach this —
    `_run_once` only consults it when ``env`` is ``None``.
    """
    return automation_subprocess_env(cmd)


def _run_once(
    cmd: list[str], timeout_s: int = 20, cwd: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    if env is None:
        env = _automation_env_refresh(cmd)
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s, cwd=cwd, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        # Preserve WHY the call failed instead of returning a blank stderr that
        # collapses every failure mode into an opaque "exit 1". A timeout is a
        # distinct, actionable signal (gh hung / network stalled) that callers
        # surface to operators — see list_open_prs / maintenance_check (BOU-1638).
        partial = exc.stderr or exc.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        detail = f"gh timed out after {timeout_s}s: {' '.join(cmd)}"
        stderr = f"{detail}\n{partial}".strip() if partial else detail
        return subprocess.CompletedProcess(cmd, 1, "", stderr)
    except OSError as exc:
        # e.g. gh not on PATH, or a PATH/env difference between the interactive
        # shell and this Python subprocess (the BOU-1638 root cause). Capture the
        # OSError text so the operator sees "No such file or directory: 'gh'"
        # instead of a bare exit 1.
        return subprocess.CompletedProcess(cmd, 1, "", f"{type(exc).__name__}: {exc}")


def _run(
    cmd: list[str], timeout_s: float = 20, cwd: str | None = None,
    env: dict[str, str] | None = None, deadline: float | None = None,
) -> subprocess.CompletedProcess:
    """Run a gh command, retrying transient connectivity failures with backoff.

    Non-connectivity failures (auth, bad args) and successes return immediately;
    only connection-establishment failures are re-attempted, up to
    ``_GH_RETRY_ATTEMPTS`` total with exponential backoff. This makes the reach
    robust rather than merely diagnosable — BOU-1694's AC that the completion
    path "can be retried successfully when direct gh works from the same cwd."

    ``env``, when given, REPLACES the subprocess environment entirely (as
    ``subprocess.run`` does) — callers pass a full ``os.environ`` copy with
    targeted overrides, never a partial dict. ``None`` (the default) inherits
    this process's environment unchanged. When ``deadline`` is supplied, all
    attempts and backoffs draw from that one monotonic wall-clock budget.
    """
    def remaining_timeout() -> float:
        if deadline is None:
            return timeout_s
        return max(0.0, min(timeout_s, deadline - time.monotonic()))

    result = _run_once(cmd, timeout_s=timeout_s, cwd=cwd, env=env)
    for attempt in range(1, _GH_RETRY_ATTEMPTS):
        if _is_transient_connectivity_failure(result):
            delay = _GH_RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
        elif _is_rate_limit_failure(result):
            if not _RATE_LIMIT_BACKOFF_ENABLED:
                # Backoff suppressed (e.g. the stop-gate, BOU-1953): a
                # latency-sensitive caller must fail fast on quota exhaustion
                # rather than accumulate sleeps toward its hard deadline.
                # `_RATE_LIMIT_SEEN` is still set below.
                break
            # Rate-limited: back off (capped) and retry. Brief secondary limits
            # clear within a couple of short sleeps; a persistent primary
            # exhaustion still returns after the bounded attempts so the caller
            # (waiter) can decide to stay alive across ticks (BOU-1921).
            delay = _rate_limit_backoff_seconds(result.stderr or "", attempt)
        else:
            return result
        if deadline is not None:
            delay = min(delay, max(0.0, deadline - time.monotonic()))
        if delay > 0:
            time.sleep(delay)
        attempt_timeout = remaining_timeout()
        if attempt_timeout <= 0:
            break
        result = _run_once(cmd, timeout_s=attempt_timeout, cwd=cwd, env=env)
    # Exhausted retries. Record a persistent rate-limit so a tick-based caller
    # can tell "GitHub was quota-limited" from a hard failure (BOU-1921 #62).
    if _is_rate_limit_failure(result):
        global _RATE_LIMIT_SEEN, _RATE_LIMIT_EVENTS
        _RATE_LIMIT_SEEN = True
        _RATE_LIMIT_EVENTS += 1
    return result


def _is_infra_check(name: str) -> bool:
    lower = name.lower()
    return any(pat in lower for pat in INFRA_CHECK_PATTERNS)


def get_repo_info(cwd: str | None = None) -> tuple[str, str]:
    """Get owner/repo from git remote."""
    r = _run(["gh", "repo", "view", "--json", "owner,name"], cwd=cwd)
    if r.returncode != 0:
        return "", ""
    try:
        data = json.loads(r.stdout)
        return data.get("owner", {}).get("login", ""), data.get("name", "")
    except (json.JSONDecodeError, AttributeError):
        return "", ""


@dataclass(frozen=True)
class GhFailure:
    """Diagnostics for a failed ``gh`` invocation.

    Carried out-of-band (via :func:`last_list_open_prs_failure`) so the
    ``None`` (=failure) vs ``[]`` (=genuinely no PRs) return-value invariant of
    :func:`list_open_prs` stays intact — a transient outage must never be
    mistaken for "no PRs" and prune tracked PRs (BOU-1638 / BOU-1694)."""

    command: list[str]
    returncode: int
    stderr: str
    reason: str  # short machine-ish category: "exit", "invalid-json", "not-a-list"

    @property
    def command_str(self) -> str:
        return " ".join(self.command)

    @property
    def is_rate_limited(self) -> bool:
        """True when this failure is a GitHub quota/rate-limit (primary or
        secondary), as opposed to a missing-``gh`` / auth / malformed-output
        failure.

        Gate for the quota-safe REST fallback (BOU-1966): only a rate-limit
        classified list failure may fall back to per-PR REST resolution — auth
        and environment failures affect REST identically and keep failing
        closed."""
        if self.reason == "rate-limit":
            return True
        stderr = (self.stderr or "").lower()
        return any(pat in stderr for pat in _RATE_LIMIT_STDERR_PATTERNS)

    def summary(self) -> str:
        """One-line classified cause for event logs and dashboards.

        The bare "GitHub API unavailable" collapsed very different failure
        modes (expired App token vs DNS outage vs quota wall) into one string
        during the 2026-07-11 outage (BOU-1987); this names the class so the
        operator's first look at the event log points at the right fix.
        """
        stderr = (self.stderr or "").strip()
        lowered = stderr.lower()
        first_line = stderr.splitlines()[0] if stderr else f"gh exited {self.returncode}"
        if len(first_line) > 140:
            first_line = first_line[:137] + "..."
        probe = subprocess.CompletedProcess(self.command, self.returncode, "", stderr)
        if "401" in lowered or "bad credentials" in lowered:
            return f"auth failure — {first_line}"
        if _is_rate_limit_failure(probe):
            return f"rate-limited — {first_line}"
        if _is_transient_connectivity_failure(probe):
            return f"network unreachable — {first_line}"
        if "timed out" in lowered:
            return f"timeout — {first_line}"
        if self.reason != "exit":
            return f"{self.reason} — {first_line}"
        return first_line

    def describe(self) -> str:
        """Multi-line operator-facing diagnostic + self-check + remediation."""
        stderr = (self.stderr or "").strip() or "(no stderr captured)"
        lines = [
            f"gh call failed ({self.reason}): {self.command_str}",
            f"  exit code: {self.returncode}",
            f"  stderr: {stderr}",
            "  remediation: this is usually a PATH/env or auth difference between "
            "your interactive shell and the Python subprocess (or a transient "
            "GitHub outage / rate-limit). Confirm `gh auth status` and that `gh` "
            "is on PATH, then re-run the exact command above from the same cwd:",
            f"    {self.command_str}",
        ]
        return "\n".join(lines)


# Most-recent ``gh`` failure recorded by :func:`list_open_prs`, exposed so the
# completion / check paths can surface real diagnostics instead of an opaque
# "gh unavailable". Reset to ``None`` on every successful list so a stale failure
# can't bleed into a later healthy call.
_LAST_LIST_OPEN_PRS_FAILURE: GhFailure | None = None

# --------------------------------------------------------------------------- #
# One PR-state resolution per repo, shared by every reader (BOU-2810)
# --------------------------------------------------------------------------- #
#
# `gh pr list` and `gh pr view` are GraphQL calls, and every poller on a host
# shares ONE App-installation token with a single 5000-point hourly budget. The
# old shape had each caller resolve PR state on its own: the dashboard, the
# maintenance loop, each session's `await`, the stop-gate's own branch probe,
# and a per-PR `gh pr view` for each PR being checked. That is why the budget
# drained in ~3 minutes.
#
# The fix is to fetch ONE list per (repo, author) per TTL window and serve
# everything from it. That only works if the single fetch carries every field
# any reader might want — hence this superset. `gh pr list --json` costs the
# same one GraphQL round trip whether you ask for 3 fields or 15, so widening
# it is free and removes the need for per-PR follow-up calls entirely.
#
# DELIBERATELY EXCLUDED: `statusCheckRollup`. It is large (every check run on
# every PR), it changes far faster than this TTL, and only one caller wants it.
# Including it would bloat every snapshot to serve one reader stale CI state.
# That caller keeps its own direct call — see `resolve_pr`'s fall-through.
PR_SNAPSHOT_FIELDS = (
    "number,title,body,url,state,isDraft,mergeStateStatus,mergeable,"
    "reviewDecision,headRefOid,headRefName,headRepositoryOwner,baseRefName,"
    "mergedAt,author,labels,createdAt"
)

#: Fields a caller may request and still be served from the snapshot. Anything
#: outside this set falls through to a real `gh pr view`, so a reader can never
#: be handed a field the snapshot did not actually fetch.
_SNAPSHOT_SERVABLE_FIELDS = frozenset(PR_SNAPSHOT_FIELDS.split(","))

# ``gh pr list`` defaults to 30 items and has no explicit ``--paginate`` flag.
# Its implementation keeps fetching pages until ``--limit`` is reached or
# GitHub reports no next page.  Use the CLI's maximum signed-int count as the
# conventional "drain every page" sentinel so client-side author filtering is
# not applied to an arbitrarily capped repository population.
_GH_COMPLETE_LIST_LIMIT = str(2**31 - 1)


def _repo_hostname(cwd: str | None = None) -> str:
    """Return the current repository's GitHub host without an API call."""

    gh_repo_parts = [part for part in os.environ.get("GH_REPO", "").split("/") if part]
    if len(gh_repo_parts) >= 3:
        return gh_repo_parts[0]
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd, capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        remote = None
    url = (remote.stdout or "").strip() if remote and remote.returncode == 0 else ""
    if url.startswith("git@") and ":" in url:
        return url[4:].split(":", 1)[0]
    if "://" in url:
        hostname = urllib.parse.urlparse(url).hostname
        if hostname:
            return hostname
    return os.environ.get("GH_HOST", "").strip() or "github.com"


def _viewer_login_result(
    cwd: str | None = None, *, timeout_s: float = 15,
) -> subprocess.CompletedProcess:
    """Resolve ``@me`` on the same GitHub host as the working repository."""

    return _run(
        ["gh", "api", "user", "--hostname", _repo_hostname(cwd), "--jq", ".login"],
        cwd=cwd, timeout_s=timeout_s,
    )


def last_list_open_prs_failure() -> GhFailure | None:
    """Return diagnostics for the most recent failed :func:`list_open_prs` call.

    ``None`` once a list has succeeded (or before any failure). Callers read
    this immediately after a ``None`` return from :func:`list_open_prs`."""
    return _LAST_LIST_OPEN_PRS_FAILURE


def list_open_prs(cwd: str | None = None) -> list[dict] | None:
    """List all open PRs authored by the tracked PR author.

    The author defaults to ``@me`` but is configurable (``pr_author`` in
    ``agentic-pr-dash.toml`` / ``AGENTIC_PR_DASH_PR_AUTHOR``): under an
    isolated automation identity (a GitHub App installation token in
    ``GH_TOKEN``, BOU-1923) ``@me`` resolves to the App bot — which authored
    no PRs — so every discovery path silently returns ``[]`` and the board
    empties. See :attr:`agentic_pr_dash.config.Config.pr_author`.

    Returns ``None`` when the underlying ``gh`` call fails (e.g. the GitHub
    API is rate-limited or unreachable) so callers can distinguish a genuine
    "no open PRs" result (``[]``) from an API failure. Treating a failure as
    an empty list would let a transient outage prune every tracked PR.

    On failure, diagnostics (command, exit code, stderr, reason) are recorded in
    :func:`last_list_open_prs_failure` so the caller can surface the underlying
    cause instead of a bare "gh unavailable".
    """
    from .config import load as _load_config  # noqa: PLC0415 — deferred to avoid import cycles

    global _LAST_LIST_OPEN_PRS_FAILURE
    cmd = [
        "gh", "pr", "list", "--state", "open",
        "--limit", _GH_COMPLETE_LIST_LIMIT, "--json", PR_SNAPSHOT_FIELDS,
    ]
    r = _run(cmd, cwd=cwd, timeout_s=30)
    if r.returncode != 0:
        # Distinguish a rate-limit failure so the poll path can back off / stay
        # alive rather than treating it as a hard "gh unavailable" (BOU-1921).
        reason = "rate-limit" if _is_rate_limit_failure(r) else "exit"
        _LAST_LIST_OPEN_PRS_FAILURE = GhFailure(
            command=cmd, returncode=r.returncode, stderr=r.stderr or "", reason=reason,
        )
        return None
    try:
        prs = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        _LAST_LIST_OPEN_PRS_FAILURE = GhFailure(
            command=cmd, returncode=r.returncode,
            stderr=(r.stderr or "")
            or f"gh returned non-JSON output: {(r.stdout or '')[:200]!r}",
            reason="invalid-json",
        )
        return None
    if not isinstance(prs, list):
        _LAST_LIST_OPEN_PRS_FAILURE = GhFailure(
            command=cmd, returncode=r.returncode,
            stderr=(r.stderr or "")
            or f"gh returned a non-list JSON payload: {type(prs).__name__}",
            reason="not-a-list",
        )
        return None
    author = _load_config(cwd).pr_author
    if author == "@me":
        viewer = _viewer_login_result(cwd)
        author = viewer.stdout.strip() if viewer.returncode == 0 else ""
        if not author:
            _LAST_LIST_OPEN_PRS_FAILURE = GhFailure(
                command=viewer.args if isinstance(viewer.args, list) else ["gh", "api", "user"],
                returncode=viewer.returncode,
                stderr=(viewer.stderr or "authenticated GitHub login is unavailable"),
                reason="viewer-unavailable",
            )
            return None
    prs = [
        pr for pr in prs
        if isinstance(pr, dict)
        and isinstance(pr.get("author"), dict)
        and pr["author"].get("login") == author
    ]
    _LAST_LIST_OPEN_PRS_FAILURE = None
    return prs


def _parse_included_rest_response(
    stdout: str,
) -> tuple[int | None, dict[str, str], str]:
    """Split ``gh api --include`` output into status, headers, and body.

    ``gh`` emits the response headers before the JSON body.  The parser also
    accepts body-only output because that is the convenient shape used by
    tests and by older gh versions when a proxy strips ``--include`` output.
    """

    text = stdout or ""
    status: int | None = None
    headers: dict[str, str] = {}
    body = text
    sections = re.split(r"\r?\n\r?\n", text)
    header_index: int | None = None
    for index, section in enumerate(sections):
        match = re.search(r"^HTTP/\S+\s+(\d{3})\b", section, re.MULTILINE)
        if match is None:
            continue
        header_index = index
        status = int(match.group(1))
        for line in section.splitlines()[1:]:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip().casefold()] = value.strip()
        # A redirect can produce multiple header blocks.  The last block is
        # the response whose body follows it.
    if header_index is not None:
        body = "\n\n".join(sections[header_index + 1 :]).strip()
        # Some gh/proxy combinations place the body in the same section as the
        # headers when there is no blank separator.
        if not body:
            section = sections[header_index]
            lines = section.splitlines()
            body = "\n".join(lines[1 + len(headers) :]).strip()
    return status, headers, body


def _rest_page_is_truncated(
    headers: dict[str, str], returned: int, per_page: int
) -> bool:
    """Whether a REST page leaves more results unseen.

    GitHub advertises further pages with ``Link: <...>; rel="next"`` and omits
    the header entirely on a single-page result, so the header is authoritative
    when present. Falling back to ``returned >= per_page`` misreports the exact
    boundary case — a repository with precisely ``per_page`` open PRs — and that
    misreport is not harmless: it permanently disqualifies the conditional 304s
    built on that validator from counting as observations.
    """

    link = headers.get("link") or headers.get("Link") or ""
    if 'rel="next"' in link.replace("'", '"'):
        return True
    if link:
        return False
    # No Link header at all. GitHub omits it on a single-page result, so the
    # common reading is "complete" — and that is the case the reviewer flagged
    # (exactly per_page open PRs). A short page is unambiguous either way.
    #
    # A proxy that strips Link would make us under-report truncation, but since
    # the probe can no longer prune anything (only a rich relist may), the worst
    # case is a slightly optimistic freshness label in a rare setup — far
    # cheaper than breaking the indicator for every repo sitting exactly on the
    # page boundary.
    return False


def probe_open_prs_rest(
    owner: str,
    repo: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    author: str | None = None,
    cwd: str | None = None,
) -> ConditionalPRListProbe:
    """Conditionally validate the cached open-PR metadata via REST.

    This intentionally does not call ``gh pr list`` or GraphQL.  The response
    is capped at 100 records, which is the GitHub REST page maximum and is
    sufficient for the dashboard's supported open-PR population.  The rich
    relist remains the source of labels, mergeability, and review metadata when
    the validator reports a change.
    """

    if not owner or not repo:
        return ConditionalPRListProbe(None, [], error="repository identity unavailable")

    per_page = 100
    endpoint = (
        f"repos/{owner}/{repo}/pulls?state=open&sort=updated&direction=desc"
        f"&per_page={per_page}&page=1"
    )
    cmd = ["gh", "api", "--include", endpoint]
    if etag:
        cmd.extend(["-H", f"If-None-Match: {etag}"])
    if last_modified:
        cmd.extend(["-H", f"If-Modified-Since: {last_modified}"])
    result = _run(cmd, cwd=cwd, timeout_s=30)
    status, headers, body = _parse_included_rest_response(result.stdout or "")
    # A 304 is a successful conditional response even though some gh builds
    # surface it as a non-zero subprocess result.
    if status == 304:
        return ConditionalPRListProbe(
            status,
            [],
            etag=headers.get("etag") or etag,
            last_modified=headers.get("last-modified") or last_modified,
        )
    if result.returncode != 0:
        return ConditionalPRListProbe(
            status,
            [],
            etag=headers.get("etag") or etag,
            last_modified=headers.get("last-modified") or last_modified,
            error=(result.stderr or "REST open-PR probe failed").strip(),
        )
    if status is not None and status != 200:
        return ConditionalPRListProbe(
            status,
            [],
            etag=headers.get("etag") or etag,
            last_modified=headers.get("last-modified") or last_modified,
            error=f"REST open-PR probe returned HTTP {status}",
        )
    try:
        payload = json.loads(body or "[]")
    except json.JSONDecodeError as exc:
        return ConditionalPRListProbe(
            status or 200,
            [],
            etag=headers.get("etag") or etag,
            last_modified=headers.get("last-modified") or last_modified,
            error=f"REST open-PR probe returned invalid JSON: {exc}",
        )
    if not isinstance(payload, list):
        return ConditionalPRListProbe(
            status or 200,
            [],
            etag=headers.get("etag") or etag,
            last_modified=headers.get("last-modified") or last_modified,
            error="REST open-PR probe returned a non-list payload",
        )

    configured_author = author
    if not configured_author:
        configured_author = load_config(cwd).pr_author
    configured_author = (configured_author or "").strip()
    if configured_author.casefold() in {"@me", "me"}:
        resolved_viewer = _rest_viewer_login(cwd)
        if not resolved_viewer:
            # ``_rest_viewer_login`` returns "" on any failure, notably a GitHub
            # App installation token, which cannot call ``/user`` — and its
            # docstring requires callers to fail closed rather than adopt a PR
            # whose author they cannot verify.
            #
            # Falling through with an empty author skips filtering entirely, so
            # the "projection" would be every author's PRs. That is wrong on its
            # own terms, and it feeds the scheduling comparison: unrelated
            # activity would read as a tracked change and spend a rich GraphQL
            # relist, reintroducing the drain that comparison prevents
            # (BOU-3095 PR #169 round 9).
            return ConditionalPRListProbe(
                status,
                [],
                etag=headers.get("etag") or etag,
                last_modified=headers.get("last-modified") or last_modified,
                error=(
                    "cannot resolve the @me PR author via REST; refusing to "
                    "report an unfiltered open-PR list"
                ),
            )
        configured_author = resolved_viewer
    # Canonicalize BOTH sides. A GitHub App identity is spelled ``app/<name>``
    # in configuration and ``<name>[bot]`` in REST payloads, so a raw casefold
    # comparison silently matches nothing and the probe reports an empty
    # open-PR list. That used to cost only a wasted relist; now that the probe
    # body is authoritative for pruning, it would take every open PR off the
    # board (BOU-3095 PR #169 review).
    expected_author = _login_key(configured_author)
    normalized: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if expected_author:
            user = item.get("user")
            login = user.get("login") if isinstance(user, dict) else ""
            if _login_key(str(login or "")) != expected_author:
                continue
        converted = _normalize_rest_pr_payload(item)
        if converted is not None:
            normalized.append(converted)
    return ConditionalPRListProbe(
        status or 200,
        normalized,
        etag=headers.get("etag") or etag,
        last_modified=headers.get("last-modified") or last_modified,
        # Truncation is what the Link header says, not what a full page implies:
        # a repository with exactly ``per_page`` open PRs has a full FIRST page
        # and no second one, and calling that truncated stops every later 304
        # counting as an observation, so the freshness indicator goes stale
        # while every probe is in fact succeeding (BOU-3095 PR #169 round 7).
        # Without a Link header we genuinely cannot tell, so a full page stays
        # conservatively truncated.
        truncated=_rest_page_is_truncated(headers, len(payload), per_page),
    )


# Explicit alias for callers that describe this read as a metadata probe.
probe_open_pr_metadata = probe_open_prs_rest
conditional_open_pr_probe = probe_open_prs_rest


# ---------------------------------------------------------------------------
# Shared short-TTL PR-list snapshot cache (BOU-1923 Bucket 2 / BOU-1953).
#
# The stop-gate, the detached ``pr-maintenance-loop``, and every in-session
# ``await`` waiter each resolve "my open PRs" on their own cadence (a Stop
# hook, a poll tick, a per-owned-worktree check) — all via the SAME underlying
# ``gh pr list --author @me --state open`` call. With several of those active
# at once (a busy multi-PR session) that multiplies the call volume against one
# shared GitHub quota, and is part of why the stop-gate can time out under
# exhaustion (BOU-1953). A short-TTL, state_dir-backed JSON snapshot lets every
# process within the TTL window reuse ONE fetch instead of firing its own.
# ---------------------------------------------------------------------------

_PR_SNAPSHOT_FILENAME = "pr-snapshot.json"
# Default snapshot TTL. Short enough that a caller needing genuinely fresh
# state (a completion/mutation path) can just pass force=True; long enough
# that a burst of stop-gate/loop/waiter calls within the same few seconds
# collapses to a single `gh` call. Env-overridable for tests/tuning.
_PR_SNAPSHOT_TTL_S = float(os.environ.get("APD_PR_SNAPSHOT_TTL_S", "45"))

# Bounded wait for the snapshot refresh lock (see ``_acquire_snapshot_lock``).
# On a cold/expired cache a burst of processes would otherwise ALL fetch at
# once (the "thundering herd" the cache is meant to collapse). The first to
# grab this lock fetches+writes; the rest wait briefly, then re-read the fresh
# snapshot. Capped so a wedged holder can't hang a latency-sensitive caller
# (the stop-gate) — on timeout we fall through to a direct fetch.
_PR_SNAPSHOT_LOCK_WAIT_S = float(os.environ.get("APD_PR_SNAPSHOT_LOCK_WAIT_S", "5"))


def _pr_snapshot_dir() -> Path:
    """Host-global snapshot directory — deliberately NOT inside a worktree.

    See :func:`_pr_snapshot_path` for why.
    """
    override = os.environ.get("APD_PR_SNAPSHOT_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return Path(base) / "agentic-pr-dash"


def _pr_snapshot_path(cwd: str | None) -> Path:
    """Where the shared PR-list snapshot lives, keyed by (repo, author).

    This used to be ``<cwd>/.gaia/pr-snapshot.json`` — i.e. **per worktree**.
    That defeated the cache's own purpose (BOU-2810). ``gh pr list --author X
    --state open`` is scoped to a repo and an author; the answer is identical no
    matter which worktree you ask from. Partitioning it by worktree bought
    nothing and multiplied the call volume by the number of active worktrees:
    five concurrent ``await`` processes across five worktrees meant five
    independent snapshots and five real GraphQL calls per window, against ONE
    shared installation token with a single 5000-point hourly budget. Observed
    2026-08-02 draining a full budget in ~3 minutes, after which every PR read
    fails and the stop gate reports ``gh state unknown`` for healthy PRs.

    Keying by (repo, author) instead makes every process on the host — daemons,
    stop-gates, and each session's waiter — share one fetch per TTL window,
    which is what the cache was always meant to do. Different repos and
    different authors still get separate snapshots, so no correctness is traded
    for the sharing.

    The key is hashed to keep the filename filesystem-safe (``owner/name``
    contains a separator) and fixed-length.
    """
    config = load_config(cwd)
    try:
        repo = config.resolved_repo(Path(cwd) if cwd else None) or "unknown-repo"
    except Exception:  # noqa: BLE001 — repo detection must never break caching
        repo = "unknown-repo"
    author = getattr(config, "pr_author", None) or "unknown-author"
    key = hashlib.sha256(f"{repo}\n{author}".encode("utf-8")).hexdigest()[:16]
    stem = _PR_SNAPSHOT_FILENAME.removesuffix(".json")
    return _pr_snapshot_dir() / f"{stem}-{key}.json"


def _acquire_snapshot_lock(path: Path) -> "int | None":
    """Best-effort exclusive lock on ``<snapshot>.lock``, bounded by
    ``_PR_SNAPSHOT_LOCK_WAIT_S``.

    Returns an open fd (held lock) on success, or ``None`` when locking is
    unavailable (non-POSIX / open failure) or the wait times out. A ``None``
    return is not fatal: the caller still re-reads the snapshot (a sibling that
    held the lock has likely just refreshed it) and only fetches if it is still
    stale — so lock contention degrades to at most an unserialized fetch rather
    than a hang."""
    if fcntl is None:
        return None
    lock_path = path.with_name(path.name + ".lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    deadline = time.monotonic() + _PR_SNAPSHOT_LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                try:
                    os.close(fd)
                except OSError:
                    pass
                return None
            time.sleep(0.05)


def _release_snapshot_lock(fd: "int | None") -> None:
    if fd is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _read_pr_snapshot(path: Path, ttl_s: float, author: str) -> list[dict] | None:
    """Return the cached PR list if a fresh-enough snapshot exists, else ``None``.

    The snapshot is PARTITIONED BY AUTHOR: a snapshot fetched for a different
    ``pr_author`` (including legacy snapshots written before the ``author``
    field existed) is a MISS, never a hit. Without this, changing ``pr_author``
    (e.g. pinning the operator login after an App-token process cached a fresh
    ``@me``-as-bot ``[]``) would serve the wrong author's list until the TTL
    expired — making stop/await resolution miss the operator's PR (PR #69
    review).

    Any read/parse failure (missing file, torn write from a concurrent
    sibling process, unexpected shape) is treated as a cache miss rather than
    raised — the caller falls back to a real fetch."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    fetched_at = data.get("fetched_at")
    prs = data.get("prs")
    if not isinstance(fetched_at, (int, float)) or not isinstance(prs, list):
        return None
    if data.get("author") != author:
        return None
    if (time.time() - fetched_at) > ttl_s:
        return None
    return prs


def _write_pr_snapshot(path: Path, prs: list[dict], author: str) -> None:
    """Atomically write the snapshot (temp file + rename). Best-effort: a
    failure to persist the cache must never fail the caller's real fetch."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".pr-snapshot.")
    except OSError:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": time.time(), "prs": prs, "author": author}, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def list_open_prs_cached(
    cwd: str | None = None, *, force: bool = False, ttl_s: float | None = None,
) -> list[dict] | None:
    """``list_open_prs``, sharing one snapshot across processes within a short TTL.

    On a cache HIT (an unexpired snapshot under this worktree's ``state_dir``)
    returns it WITHOUT calling ``gh``. On MISS/expiry/torn-read, calls
    :func:`list_open_prs` for real. Preserves the ``None``-vs-``[]`` invariant:
    a failed real fetch returns ``None`` and never writes (or poisons) the
    snapshot, so a transient outage can't get cached as "no PRs" and prune
    tracked state elsewhere.

    ``force=True`` bypasses the cache entirely (read AND write a fresh value)
    for callers that must see the current state. ``ttl_s`` overrides the
    module default (`` APD_PR_SNAPSHOT_TTL_S``, default 45s) for one call.

    On a cold/expired cache the refresh is serialized behind a per-snapshot
    lock so a burst of processes collapses to ONE ``gh`` fetch (the herd this
    cache exists to prevent): the lock winner fetches+writes; the rest wait
    briefly, then re-read the now-fresh snapshot (see
    :func:`_acquire_snapshot_lock`).
    """
    from .config import load as _load_config  # noqa: PLC0415 — deferred to avoid import cycles

    path = _pr_snapshot_path(cwd)
    # The snapshot is keyed by the configured PR author (PR #69 review): a hit
    # is only a hit for the SAME author the caller would fetch for.
    author = _load_config(cwd).pr_author
    effective_ttl = _PR_SNAPSHOT_TTL_S if ttl_s is None else ttl_s
    if force:
        # The caller demands current state — skip the cache/lock entirely.
        prs = list_open_prs(cwd)
        if prs is None:
            return None
        _write_pr_snapshot(path, prs, author)
        return prs

    cached = _read_pr_snapshot(path, effective_ttl, author)
    if cached is not None:
        return cached

    # MISS/expiry: serialize the refresh so concurrent misses don't each fire
    # their own `gh pr list`.
    lock = _acquire_snapshot_lock(path)
    try:
        # Re-check under the lock: whoever we waited on (or a sibling that
        # raced us to the file before we took the lock) may have just written a
        # fresh snapshot, in which case we skip the redundant fetch. This
        # re-check ALSO covers the ``lock is None`` degraded path — a timed-out
        # waiter usually finds the holder's fresh write here.
        cached = _read_pr_snapshot(path, effective_ttl, author)
        if cached is not None:
            return cached
        prs = list_open_prs(cwd)
        if prs is None:
            return None
        _write_pr_snapshot(path, prs, author)
        return prs
    finally:
        _release_snapshot_lock(lock)


_PR_HEAD_FIELDS = (
    "number,title,body,url,isDraft,mergeStateStatus,reviewDecision,"
    "headRefOid,headRefName,headRepositoryOwner,baseRefName,mergedAt,author"
)

# GitHub's REST `GET /repos/{owner}/{repo}/pulls?head={owner}:{branch}` performs
# an *exact* head match server-side (unlike `gh pr list --head`, which is a
# *prefix* filter — `--head fix` also returns `fix-123`). The prefix filter
# forced us to over-fetch a wide page and exact-filter `headRefName` in Python,
# which silently dropped the exact-branch PR whenever more than one page of
# prefix-matches existed and the exact branch sorted beyond the fetched page.
# We use the exact REST query for resolution instead, so the result is
# independent of how many prefix-matches exist.
#
def peek_pr_snapshot(
    cwd: str | None = None, *, ttl_s: float | None = None
) -> list[dict] | None:
    """Return the shared snapshot if one is fresh, WITHOUT ever fetching.

    The read-only half of :func:`list_open_prs_cached`, for callers that operate
    under a hard deadline — the Stop-hook reconciliation path bounds its `gh`
    subprocess with the remaining budget (BOU-1787), so it must never be routed
    through something that might issue a fetch. Those callers peek first, use the
    snapshot when a sibling process has already populated it, and otherwise fall
    back to their own timeout-bounded call.

    ``None`` means "no usable snapshot", never "no PRs".
    """
    effective_ttl = _PR_SNAPSHOT_TTL_S if ttl_s is None else ttl_s
    if effective_ttl <= 0:
        return None
    try:
        from .config import load as _load_config  # noqa: PLC0415

        author = _load_config(cwd).pr_author
        path = _pr_snapshot_path(cwd)
    except (OSError, ValueError):
        # Config/repo resolution genuinely unavailable — degrade to "no snapshot".
        # Deliberately NOT a bare `except Exception`: an earlier version swallowed
        # everything and silently hid an argument-order bug in the call below,
        # turning every peek into a miss while looking like it worked.
        return None
    return _read_pr_snapshot(path, effective_ttl, author)


def resolve_pr(
    pr_number: int,
    fields: str,
    cwd: str | None = None,
    *,
    force: bool = False,
) -> dict | None:
    """Resolve one PR's state, reusing the host-global list snapshot (BOU-2810).

    This is the converged replacement for scattered ``gh pr view <n> --json ...``
    calls. Those are GraphQL, and with several tracked PRs polled from several
    sessions they were a large share of the traffic that exhausted the shared
    installation token's hourly budget. The list snapshot already contains every
    open PR by the tracked author, with the full :data:`PR_SNAPSHOT_FIELDS`
    superset — so for the common case there is nothing left to ask GitHub.

    Falls through to a real ``gh pr view`` — never guesses — when the snapshot
    genuinely cannot answer:

    * ``force=True`` (a caller about to act on the result wants it live)
    * a requested field is outside the superset (e.g. ``statusCheckRollup``)
    * the PR is absent from the snapshot, which is normal and important: the
      list is ``--author <tracked>`` and open-only, so someone else's PR or a
      closed/merged one is simply not in it.

    Returns the PR dict restricted to ``fields``, or ``None`` if it cannot be
    resolved at all. A ``None`` here means "unknown", never "does not exist".
    """
    wanted = [field.strip() for field in fields.split(",") if field.strip()]
    if not wanted:
        return None

    if not force and _SNAPSHOT_SERVABLE_FIELDS.issuperset(wanted):
        prs = list_open_prs_cached(cwd) or []
        for pr in prs:
            if pr.get("number") == pr_number:
                # Only hand back what was asked for, so a caller cannot silently
                # start depending on a field it never requested.
                return {key: pr.get(key) for key in wanted}

    result = _run(["gh", "pr", "view", str(pr_number), "--json", fields], cwd=cwd)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def resolve_pr_field(
    pr_number: int,
    field: str,
    cwd: str | None = None,
    *,
    force: bool = False,
):
    """Single-field convenience wrapper around :func:`resolve_pr`.

    Returns ``None`` when the PR could not be resolved, which callers must treat
    as "unknown" rather than as a value.
    """
    data = resolve_pr(pr_number, field, cwd, force=force)
    if data is None:
        return None
    return data.get(field)


# REST `state` is one of {open, closed, all} (no "merged"); a merged PR is a
# closed PR, so we map merged→closed for the *server-side* query. The REST API
# cannot distinguish merged-from-closed, so when the caller asked for "merged"
# we re-impose the merged-only gate in Python on each candidate via `mergedAt`
# (a closed-but-not-merged PR has `mergedAt is null`) — see `find_pr_by_head`.
_REST_STATE = {"open": "open", "closed": "closed", "merged": "closed", "all": "all"}
# Page size cap for the exact-head REST query. Distinct PRs for one exact head
# branch are rare (normally 1), so a single page is plenty; we still paginate
# defensively below.
_PR_HEAD_PER_PAGE = "100"


def find_pr_by_head(
    branch: str,
    state: str = "open",
    cwd: str | None = None,
    *,
    head_oid: str | None = None,
) -> dict | None:
    """Find a PR by its head branch name, returning the full PR payload.

    Unlike :func:`list_open_prs` (author-scoped, no body), this resolves the PR
    for a specific *head branch* and returns the fields a Stop/QA gate needs to
    evaluate gate policy: ``number, title, body, url, isDraft, mergeStateStatus,
    reviewDecision, headRefOid, headRefName, baseRefName``.

    ``state`` is one of ``"open"``, ``"merged"``, ``"closed"``, ``"all"``. For
    ``"merged"`` the merged-only contract is enforced: a PR that was *closed
    without merging* (``mergedAt is null``) is never returned, even though it
    matches the underlying ``state=closed`` REST/list filter. When ``head_oid``
    is given, only a PR whose ``headRefOid`` matches it is returned — the caller
    uses this to confirm that a merged PR corresponds to the *current* local HEAD
    (squash-merged branches can stay ahead of the default branch, and a reused
    branch name must still go through the normal gates).

    Returns ``None`` on any ``gh`` failure (so the caller fails open) or when no
    matching PR exists.
    """
    if not branch:
        return None
    # `<owner>:<branch>` head specs (fork / head-qualified) carry the head-repo
    # owner. We keep it so two fork PRs sharing a branch name (`alice:feature`,
    # `bob:feature`) don't collide. When omitted, the head lives on the base repo,
    # so the head owner is the base-repo owner (resolved below).
    head_owner = ""
    if ":" in branch:
        head_owner, branch = branch.split(":", 1)
    if not branch:
        return None

    rest_state = _REST_STATE.get(state, "all")
    # When the caller asked for "merged", the REST query can only narrow to
    # `state=closed` (merged PRs ARE closed). A closed-but-not-merged PR also
    # matches that server-side filter, so we re-impose the merged-only gate in
    # Python below via `mergedAt` — a closed-unmerged PR has `mergedAt is null`
    # and must NOT be returned when "merged" was requested.
    merged_only = state == "merged"

    if head_owner:
        # Owner-qualified head (`alice:branch`): the exact REST `head=alice:branch`
        # spec resolves the fork PR directly.
        numbers = _exact_head_pr_numbers(head_owner, branch, rest_state, cwd=cwd)
    else:
        # Unqualified head (`branch`): the head may live on the base repo OR on a
        # fork (`alice:branch`). REST `head=` requires an `owner:branch` spec, so
        # an exact REST query can only cover the base-repo owner and would MISS a
        # fork-backed PR. Use the (owner-agnostic) `gh pr list --head` lookup,
        # which matches the branch on any head repo, then exact-filter in Python.
        numbers = _unqualified_head_pr_numbers(branch, rest_state, cwd=cwd)
    if numbers is None:
        return None  # gh/API failure → fail open (distinct from "no match" → None)

    for number in numbers:
        pr = _pr_full_payload(number, cwd=cwd)
        if pr is None:
            return None  # gh failure fetching full fields → fail open
        # The lookup already targets the branch (and, for qualified heads, the
        # owner), but re-verify defensively so a contract change upstream can't
        # slip the wrong PR past a Stop/QA gate.
        if str(pr.get("headRefName") or "") != branch:
            continue
        if head_owner and str(_pr_head_owner(pr)) != head_owner:
            continue
        # Merged-only gate: REST `state=closed` also returns closed-unmerged PRs,
        # so reject any candidate that was never merged when "merged" was asked.
        if merged_only and not pr.get("mergedAt"):
            continue
        if head_oid is not None and pr.get("headRefOid") != head_oid:
            continue
        return pr
    return None


def _exact_head_pr_numbers(
    owner: str, branch: str, rest_state: str, cwd: str | None = None,
    *, deadline: float | None = None,
) -> list[int] | None:
    """Return PR numbers whose head is *exactly* ``owner:branch`` via REST.

    Uses ``GET /repos/{owner}/{repo}/pulls?head={owner}:{branch}`` which matches
    the head branch exactly (no prefix matching), paginating defensively. Returns
    ``None`` on any ``gh``/API failure (so the caller fails open) and ``[]`` when
    no PR matches.
    """
    head_spec = f"{owner}:{branch}"
    numbers: list[int] = []
    page = 1
    while True:
        r = _run(
            [
                "gh", "api",
                "-H", "Accept: application/vnd.github+json",
                (
                    "repos/{owner}/{repo}/pulls"
                    f"?head={urllib.parse.quote(head_spec, safe=':')}"
                    f"&state={rest_state}"
                    f"&per_page={_PR_HEAD_PER_PAGE}&page={page}"
                ),
            ],
            cwd=cwd,
            timeout_s=30,
            deadline=deadline,
        )
        if r.returncode != 0:
            return None
        try:
            payload = json.loads(r.stdout or "[]")
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, list):
            return None
        for pr in payload:
            if isinstance(pr, dict) and isinstance(pr.get("number"), int):
                numbers.append(pr["number"])
        if len(payload) < int(_PR_HEAD_PER_PAGE):
            break
        page += 1
    return numbers


# `gh pr list --head <branch>` accepts ONLY a bare branch name (the CLI rejects
# the `owner:branch` qualifier) and matches that branch on ANY head repo —
# including forks — which is exactly what we need for an unqualified head whose
# PR may be fork-backed. It is, however, a *prefix* filter (`--head fix` also
# returns `fix-123`), so we over-fetch a wide page and exact-filter in Python.
_PR_HEAD_LIST_LIMIT = "100"
# `gh pr list --state` takes {open, closed, merged, all} (unlike REST, which has
# no "merged"); pass the caller's state through verbatim so a "merged" request
# is honored server-side here.
_PR_LIST_STATES = {"open", "closed", "merged", "all"}


def _unqualified_head_pr_numbers(
    branch: str, rest_state: str, cwd: str | None = None
) -> list[int] | None:
    """Return PR numbers whose head branch is exactly ``branch`` on *any* repo.

    Used for an unqualified head (no ``owner:`` prefix), where the PR may be
    fork-backed. REST ``head=`` requires an ``owner:branch`` spec and so cannot
    cover a fork head, so we fall back to ``gh pr list --head`` (owner-agnostic,
    fork-inclusive) and exact-filter ``headRefName`` in Python.

    ``rest_state`` is the already-mapped REST state ({open,closed,all}); we
    recover the caller's intent for the CLI's richer state vocabulary (which DOES
    accept "merged") by passing it through when valid. Returns ``None`` on any
    ``gh`` failure (fail open) and ``[]`` when nothing matches.
    """
    list_state = rest_state if rest_state in _PR_LIST_STATES else "all"
    r = _run(
        [
            "gh", "pr", "list",
            "--head", branch,
            "--state", list_state,
            "--limit", _PR_HEAD_LIST_LIMIT,
            "--json", "number,headRefName",
        ],
        cwd=cwd,
        timeout_s=30,
    )
    if r.returncode != 0:
        return None
    try:
        payload = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    numbers: list[int] = []
    for pr in payload:
        if not isinstance(pr, dict):
            continue
        # `--head` is a prefix filter; require an exact branch match so a
        # Stop/QA gate never evaluates a `fix-123` when it asked for `fix`.
        if str(pr.get("headRefName") or "") != branch:
            continue
        if isinstance(pr.get("number"), int):
            numbers.append(pr["number"])
    return numbers


def _pr_full_payload(number: int, cwd: str | None = None) -> dict | None:
    """Fetch the full Stop/QA-gate contract fields for one PR via ``gh pr view``.

    Returns ``None`` on any ``gh`` failure so the caller fails open. The returned
    dict matches the historical ``gh pr list --json`` shape (same ``_PR_HEAD_FIELDS``).
    """
    r = _run(
        ["gh", "pr", "view", str(number), "--json", _PR_HEAD_FIELDS],
        cwd=cwd,
        timeout_s=30,
    )
    if r.returncode != 0:
        return None
    try:
        pr = json.loads(r.stdout or "")
    except json.JSONDecodeError:
        return None
    return pr if isinstance(pr, dict) else None


def _rest_repo_owner(cwd: str | None = None, *, deadline: float | None = None) -> str:
    """Base-repo owner login via the REST ``repos`` endpoint (quota fallback).

    :func:`get_repo_info` (``gh repo view``) resolves through GraphQL and
    shares the exact quota bucket whose exhaustion routes callers onto the
    BOU-1966 fallback, so the fallback resolves the owner via REST instead.
    ``gh`` expands the ``{owner}/{repo}`` placeholders locally from the git
    remote — no API call is spent on the placeholder resolution itself.
    Returns ``""`` on any failure.
    """
    r = _run(
        ["gh", "api", "-H", "Accept: application/vnd.github+json",
         "repos/{owner}/{repo}", "--jq", ".owner.login"],
        cwd=cwd, timeout_s=30, deadline=deadline,
    )
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


def _rest_viewer_login(cwd: str | None = None, *, deadline: float | None = None) -> str:
    """Authenticated identity's login via REST ``GET /user`` (quota fallback).

    Resolves the ``@me`` author sentinel without GraphQL — ``gh pr list
    --author @me`` runs through the exact quota bucket whose exhaustion routes
    callers onto the BOU-1966 fallback. Returns ``""`` on any failure
    (including a GitHub App installation token, which cannot call ``/user``) —
    the fallback caller must then fail closed rather than adopt a PR whose
    author it cannot verify (PR #77 review)."""
    r = _run(
        ["gh", "api", "-H", "Accept: application/vnd.github+json",
         "user", "--jq", ".login"],
        cwd=cwd, timeout_s=30, deadline=deadline,
    )
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


# REST `mergeable` is a nullable boolean; GraphQL's is an enum. Normalize so a
# quota-fallback payload reads identically to `gh pr list --json mergeable`.
_REST_MERGEABLE_ENUM = {True: "MERGEABLE", False: "CONFLICTING", None: "UNKNOWN"}


def _rest_pr_payload(
    number: int, cwd: str | None = None, *, deadline: float | None = None
) -> dict | None:
    """Full PR payload via REST ``pulls/{number}`` — the quota-safe twin of
    :func:`_pr_full_payload` (BOU-1966).

    ``gh pr view --json`` resolves through GraphQL, so when the GraphQL quota
    is exhausted (the exact condition that routes callers here) it fails right
    alongside the author list. ``GET /repos/{owner}/{repo}/pulls/{number}``
    spends REST quota only. The payload is normalized to the
    ``_PR_HEAD_FIELDS`` shape (plus ``mergeable``) so callers consume either
    interchangeably. Returns ``None`` on any failure — the quota-fallback
    caller stays fail-closed.
    """
    r = _run(
        ["gh", "api", "-H", "Accept: application/vnd.github+json",
         f"repos/{{owner}}/{{repo}}/pulls/{number}"],
        cwd=cwd, timeout_s=30, deadline=deadline,
    )
    if r.returncode != 0:
        return None
    try:
        pr = json.loads(r.stdout or "")
    except json.JSONDecodeError:
        return None
    if not isinstance(pr, dict):
        return None
    return _normalize_rest_pr_payload(pr)


def _normalize_rest_pr_payload(pr: dict) -> dict | None:
    """Map a REST ``pulls/{number}`` payload onto the GraphQL field names the
    resolution/gate paths consume (``_PR_HEAD_FIELDS`` + ``mergeable``)."""
    number = pr.get("number")
    if not isinstance(number, int):
        return None
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    head_owner = (
        head_repo.get("owner") if isinstance(head_repo.get("owner"), dict) else {}
    )
    user = pr.get("user") if isinstance(pr.get("user"), dict) else {}
    return {
        "number": number,
        "state": str(pr.get("state") or ""),
        # REST `user` is the PR author; GraphQL serializes it as `author`.
        # Carried so the quota fallback can preserve the author-scoped
        # resolution contract (PR #77 review).
        "author": {"login": str(user.get("login") or "")},
        "title": pr.get("title") or "",
        "body": pr.get("body") or "",
        "url": pr.get("html_url") or "",
        "isDraft": bool(pr.get("draft", False)),
        # REST `mergeable_state` is GraphQL `mergeStateStatus` lowercased
        # (clean/dirty/blocked/behind/unstable/draft/has_hooks/unknown).
        "mergeStateStatus": str(pr.get("mergeable_state") or "unknown").upper(),
        # Not exposed on the REST pulls resource; consumers treat "" as unknown.
        "reviewDecision": "",
        "headRefOid": str(head.get("sha") or ""),
        "headRefName": str(head.get("ref") or ""),
        "headRepositoryOwner": {"login": str(head_owner.get("login") or "")},
        "baseRefName": str(base.get("ref") or ""),
        "mergedAt": pr.get("merged_at"),
        "mergeable": _REST_MERGEABLE_ENUM.get(pr.get("mergeable"), "UNKNOWN"),
        # GitHub advances ``updated_at`` when a comment is posted or resolved
        # without a push. That is the only cheap signal the dashboard has that
        # a review re-scan is worth spending on an otherwise unchanged head
        # (BOU-3095), so it must survive this REST -> GraphQL field mapping.
        "createdAt": pr.get("created_at"),
        "updatedAt": pr.get("updated_at"),
    }


def _pr_head_owner(pr: dict) -> str:
    """Extract the head-repository owner login from a `gh pr view`/`pr list` payload.

    `gh` serializes ``headRepositoryOwner`` as ``{"login": ..., "id": ...}``;
    return the bare login (or "" when absent)."""
    owner = pr.get("headRepositoryOwner")
    if isinstance(owner, dict):
        return str(owner.get("login") or "")
    return str(owner or "")


def get_latest_commit(pr_number: int, cwd: str | None = None) -> tuple[str, str]:
    """Get the authoritative head SHA and its commit date for a PR.

    Resolve the immutable head from ``pulls/{number}`` first. The commits-list
    endpoint defaults to 30 entries, so taking its last element returns a stale
    SHA for larger PRs.
    """
    repo = _repo_for_cwd(cwd)
    cached = _PR_BATCH_CACHE.get((repo, pr_number)) if repo else None
    if cached is not None and "latest_commit" in cached:
        sha, committed_at = cached["latest_commit"]
        return str(sha), str(committed_at)
    return get_latest_commit_uncached(pr_number, cwd)


def get_latest_commit_uncached(
    pr_number: int,
    cwd: str | None = None,
) -> tuple[str, str]:
    """Read the exact current PR head without consulting the batch cache."""

    head_result = _run(
        [
            "gh",
            "api",
            f"repos/{{owner}}/{{repo}}/pulls/{pr_number}",
            "--jq",
            ".head.sha",
        ],
        cwd=cwd,
    )
    head_sha = (head_result.stdout or "").strip()
    if head_result.returncode != 0 or not head_sha:
        return "", ""
    commit_result = _run(
        [
            "gh",
            "api",
            f"repos/{{owner}}/{{repo}}/commits/{head_sha}",
            "--jq",
            "[.sha, .commit.author.date] | @tsv",
        ],
        cwd=cwd,
    )
    if commit_result.returncode != 0 or not commit_result.stdout.strip():
        return head_sha, ""
    parts = commit_result.stdout.strip().split("\t")
    if len(parts) < 2 or parts[0] != head_sha:
        return head_sha, ""
    return head_sha, parts[1]


def get_mergeability(pr_number: int, cwd: str | None = None) -> tuple[str, str]:
    """Return (mergeStateStatus, mergeable) for a single PR.

    GitHub computes mergeability lazily and asynchronously: a bulk ``gh pr list``
    frequently returns ``UNKNOWN`` for a freshly-pushed PR (or right after the
    base branch moves) because the value isn't computed yet. A per-PR query both
    *triggers* that background computation and returns the freshest available
    value — so the dashboard isn't stuck showing a stale/clean state for a PR
    that actually conflicts. Returns ("", "") on failure so callers keep the
    last-known value rather than clobbering it.
    """
    r = _run(
        ["gh", "pr", "view", str(pr_number), "--json", "mergeStateStatus,mergeable",
         "--jq", "[.mergeStateStatus, .mergeable] | @tsv"],
        cwd=cwd,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return "", ""
    parts = r.stdout.strip().split("\t")
    if len(parts) >= 2:
        return parts[0], parts[1]
    return (parts[0] if parts else ""), ""


def get_local_pr_head(pr_branch: str, cwd: str | None) -> tuple[str, str]:
    """Local (sha, committer-date-UTC-ISO) of the PR branch's remote-tracking ref.

    ``origin/<pr_branch>`` is updated the instant ``git push`` returns, so this
    reflects a just-pushed fix immediately — unlike the GitHub API, which lags a
    second or two (BOU-1479). The date is normalized to a UTC ``...Z`` stamp so
    the caller can compare it lexicographically against GitHub ``createdAt``
    strings: ``%cI`` emits the committer's local offset (e.g. ``...-07:00``),
    which would sort wrongly against ``...Z``. Returns ("", "") when the ref
    can't be resolved.
    """
    if not pr_branch:
        return "", ""
    ref = f"origin/{pr_branch}"
    try:
        r = subprocess.run(
            ["git", "-C", cwd or ".", "log", "-1", "--format=%H%x00%cI", ref],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "", ""
    if r.returncode != 0 or not r.stdout.strip():
        return "", ""
    sha, _, date = r.stdout.strip().partition("\0")
    parsed = _parse_github_time(date.strip())
    normalized = _format_github_time(parsed) if parsed else date.strip()
    return sha.strip(), normalized


def _rev_parse(ref: str, cwd: str | None) -> str:
    """Resolve ``ref`` to a concrete commit SHA locally, or "" if it doesn't exist."""
    if not ref:
        return ""
    try:
        r = subprocess.run(
            ["git", "-C", cwd or ".", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _is_ancestor(ancestor: str, descendant: str, cwd: str | None) -> bool:
    """True iff ``ancestor`` is an ancestor of (or equal to) ``descendant`` locally."""
    if not ancestor or not descendant:
        return False
    try:
        r = subprocess.run(
            ["git", "-C", cwd or ".", "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _local_new_commits(
    baseline_sha: str,
    cwd: str | None,
    upper_ref: str = "HEAD",
    must_contain_sha: str = "",
) -> list[tuple[str, str]]:
    """Commits in ``baseline_sha..<upper_ref>`` from LOCAL git history (oldest first).

    The local repo reflects a just-pushed commit immediately, whereas the GitHub
    API lags a second or two — so preferring local avoids the race where
    `complete` runs right after `git push`, sees no qualifying commit, and leaves
    review threads unresolved (BOU-1479). ``upper_ref`` should be the PR branch's
    remote-tracking ref (``origin/<branch>``) so the range stays scoped to what
    was actually pushed to THIS PR — not arbitrary local/unpushed commits on
    whatever HEAD happens to be.

    Returns [] (so the caller falls back to the API) when the local range can't
    be trusted:

    - **No baseline / unresolvable upper ref** — nothing to scope against.
    - **Baseline is not an ancestor of the tip** — after a rebase or force-push
      the saved baseline no longer sits on the branch, so ``baseline..tip`` would
      enumerate every replayed commit reachable from the new tip rather than only
      what was pushed after the maintenance run.
    - **A known-newer head isn't contained in the local ref** — when
      ``must_contain_sha`` (the API's view of the PR head) is set but absent from
      ``origin/<branch>``, this checkout's remote-tracking ref is stale (it never
      fetched the latest push, or the branch advanced elsewhere); trusting it
      would miss commits the API already knows.
    """
    if not baseline_sha:
        return []
    upper_sha = _rev_parse(upper_ref, cwd)
    if not upper_sha:
        return []
    if not _is_ancestor(baseline_sha, upper_sha, cwd):
        return []
    if must_contain_sha and not _is_ancestor(must_contain_sha, upper_sha, cwd):
        return []
    try:
        r = subprocess.run(
            ["git", "-C", cwd or ".", "log", "--reverse", "--format=%H%x00%s",
             f"{baseline_sha}..{upper_sha}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    out: list[tuple[str, str]] = []
    for line in r.stdout.splitlines():
        sha, _, msg = line.partition("\0")
        if sha.strip():
            out.append((sha.strip(), msg.strip()))
    return out


def get_new_pr_commits(
    pr_number: int,
    baseline_sha: str,
    latest_sha: str,
    cwd: str | None = None,
    pr_branch: str | None = None,
    api_head_sha: str = "",
) -> list[tuple[str, str]] | None:
    """Return commits added to a PR after a known baseline SHA.

    Prefers the local git range scoped to the PR branch's remote-tracking ref
    (immediate after a push, and not polluted by unrelated HEAD commits); falls
    back to the GitHub API when the range can't be resolved or trusted locally.

    ``api_head_sha`` is the GitHub API's view of the PR head: when it is set but
    absent from the local ``origin/<branch>`` ref, that ref is stale and the
    local range is rejected in favor of the API (see ``_local_new_commits``).

    Returns ``None`` when the range could not be determined — a failed ``gh``
    call or an unusable payload. This is deliberately distinct from ``[]``
    ("the range is genuinely empty"), matching the discipline
    :func:`list_open_prs` already applies one function over: during an outage,
    ``[]`` would let ``complete`` conclude that a fix never landed and leave
    genuinely-fixed threads open (BOU-2417 / BOU-2200).
    """
    upper_ref = f"origin/{pr_branch}" if pr_branch else "HEAD"
    local = _local_new_commits(baseline_sha, cwd, upper_ref, must_contain_sha=api_head_sha)
    if local:
        return local

    r = _run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/commits", "--jq", "."],
        cwd=cwd,
        timeout_s=30,
    )
    if r.returncode != 0:
        return None

    # BOU-2417 (PR #113 review): blank stdout on a zero exit is an UNUSABLE
    # payload — truncated or lost output — not an empty range. Defaulting it to
    # "[]" would route it down the success path and hand `complete` an empty
    # commit set it never actually obtained. A real empty range is the literal
    # JSON `[]`, which parses fine below.
    if not (r.stdout or "").strip():
        return None

    try:
        raw = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, list):
        return None

    commits: list[tuple[str, str]] = []
    seen_baseline = not baseline_sha
    for item in raw:
        if not isinstance(item, dict):
            continue
        sha = str(item.get("sha") or "")
        if not sha:
            continue
        if not seen_baseline:
            if sha == baseline_sha:
                seen_baseline = True
            continue
        if sha == baseline_sha:
            continue
        message = str(item.get("commit", {}).get("message") or "").splitlines()[0]
        commits.append((sha, message))
        if latest_sha and sha == latest_sha:
            break

    if commits:
        return commits

    if latest_sha:
        for item in raw:
            if not isinstance(item, dict) or str(item.get("sha") or "") != latest_sha:
                continue
            message = str(item.get("commit", {}).get("message") or "").splitlines()[0]
            return [(latest_sha, message)]

    return []


def _parse_review_thread_comment(c: dict) -> ReviewThreadComment:
    review = c.get("pullRequestReview") or {}
    return ReviewThreadComment(
        database_id=int(c.get("databaseId") or 0),
        path=c.get("path"),
        line=c.get("line"),
        body=str(c.get("body") or ""),
        author=str((c.get("author") or {}).get("login") or "unknown"),
        created_at=str(c.get("createdAt") or ""),
        original_line=c.get("originalLine"),
        review_id=(
            review.get("databaseId")
            if isinstance(review, dict)
            and isinstance(review.get("databaseId"), int)
            else None
        ),
    )


def _parse_review_thread_nodes(nodes: list) -> list[ReviewThread]:
    """Shared node->``ReviewThread`` parsing for both the paginated single-PR
    query (:func:`get_review_threads`) and the multi-PR batch query
    (:func:`batch_fetch_pr_review_and_ci`, BOU-2556) — one parsing path so a fix
    to comment/thread field handling can't drift between the two callers."""
    threads: list[ReviewThread] = []
    for node in nodes:
        try:
            comment_nodes = node["comments"]["nodes"]
            if not comment_nodes:
                continue
            top = _parse_review_thread_comment(comment_nodes[0])
            replies = [_parse_review_thread_comment(c) for c in comment_nodes[1:]]
            threads.append(ReviewThread(
                node_id=str(node["id"]),
                is_resolved=bool(node.get("isResolved")),
                is_outdated=bool(node.get("isOutdated")),
                top=top,
                replies=replies,
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return threads


# ---------------------------------------------------------------------------
# Per-process, per-PR prefetch cache (BOU-2556).
#
# The stop gate used to pay a serial "review-thread query + CI-rollup query"
# for EVERY owned PR — fine at 1 PR, a ~108s Stop-hook timeout at 7. Rather
# than rewire every call site, ``_stop_gate_impl`` primes this cache with ONE
# batched, aliased GraphQL round trip per repo (see
# :func:`batch_fetch_pr_review_and_ci`) BEFORE its per-worktree loop runs;
# :func:`get_review_threads` and :func:`required_checks_pending` below consult
# it first. A cache miss (batching skipped, PR not in the batch, pagination
# overflow) falls through to the original per-PR `gh` call unchanged — this is
# purely a speed optimization with a correctness-preserving fallback, never a
# second source of truth. Keyed by ``"owner/name"`` (never a bare PR number —
# the same number can exist in two different maintenance repos, BOU-1801/#50).
# ---------------------------------------------------------------------------
_PR_BATCH_CACHE: dict[tuple[str, int], dict] = {}
_PR_BATCH_REPO_BY_CWD: dict[str, str] = {}


def _batch_repo_for_cwd(cwd: str | None) -> str | None:
    """Return the repository primed for ``cwd`` (including legacy ``None``)."""
    key = str(Path(cwd).resolve()) if cwd else ""
    return _PR_BATCH_REPO_BY_CWD.get(key)


@dataclass(frozen=True)
class PrMaintenanceSnapshot:
    """One immutable-head PR observation collected by the aggregate query."""

    pr_number: int
    head_sha: str
    head_committed_at: str
    ci_checks: tuple[CICheck, ...]
    required_pending: bool
    unresolved_threads: tuple[ReviewThread, ...]
    merge_state: str
    mergeable: str
    review_decision: str

    @property
    def merge_conflict(self) -> bool:
        return self.mergeable.upper() == "CONFLICTING" or self.merge_state.upper() == "DIRTY"

    @property
    def changes_requested(self) -> bool:
        return self.review_decision.upper() == "CHANGES_REQUESTED"

    def cache_entry(self) -> dict:
        return {
            "threads": list(self.unresolved_threads),
            "required_pending": self.required_pending,
            "latest_commit": (self.head_sha, self.head_committed_at),
            "ci_checks": list(self.ci_checks),
            "head_sha": self.head_sha,
            "merge_state": self.merge_state,
            "mergeable": self.mergeable,
            "review_decision": self.review_decision,
        }


@dataclass(frozen=True)
class PrMaintenanceSnapshotBatch:
    """Aggregate result that never collapses a partial read into clean."""

    requested: tuple[int, ...]
    observed: dict[int, PrMaintenanceSnapshot]
    missing: tuple[int, ...]

    @property
    def complete(self) -> bool:
        return not self.missing

    def cache_entries(self) -> dict[int, dict]:
        return {number: snapshot.cache_entry() for number, snapshot in self.observed.items()}


def clear_pr_batch_cache() -> None:
    """Drop every primed batch entry (call at the top of each stop-gate tick —
    this is a plain module global and would otherwise leak across ticks in a
    long-lived process, e.g. across pytest cases in the same interpreter)."""
    _PR_BATCH_CACHE.clear()
    _PR_BATCH_REPO_BY_CWD.clear()


def prime_pr_batch_cache(
    repo_slug: str,
    entries: dict[int, dict],
    cwd: str | None = None,
) -> None:
    """Populate the cache for ``repo_slug`` with one entry per PR number.

    Each entry is ``{"threads": list[ReviewThread], "required_pending": bool}``.
    """
    for pr_number, data in entries.items():
        _PR_BATCH_CACHE[(repo_slug, pr_number)] = data
    # ``None`` is the legacy single-repository call shape. Keep a sentinel
    # mapping for it too, so a successful batch is still consumed from cache
    # instead of triggering the old per-PR GraphQL lookup.
    _PR_BATCH_REPO_BY_CWD[str(Path(cwd).resolve()) if cwd else ""] = repo_slug


def get_primed_mergeability(
    pr_number: int,
    cwd: str | None = None,
) -> tuple[str, str] | None:
    """Return mergeability already paid for by the current batch, if any."""

    primed_repo = _batch_repo_for_cwd(cwd)
    cached = (
        _PR_BATCH_CACHE.get((primed_repo, pr_number)) if primed_repo else None
    )
    if cached is None or "merge_state" not in cached or "mergeable" not in cached:
        return None
    merge_state = str(cached["merge_state"] or "")
    mergeable = str(cached["mergeable"] or "")
    if (
        not merge_state
        or merge_state.upper() == "UNKNOWN"
        or not mergeable
        or mergeable.upper() == "UNKNOWN"
    ):
        return None
    return merge_state, mergeable


def published_pr_head_sha(pr_number: int, cwd: str | None = None) -> str:
    """Return the authoritative published PR head, preferring the batch cache."""
    repo = _repo_for_cwd(cwd) or ""
    observed = _PUBLISHED_HEAD_OBSERVATIONS.get((repo, pr_number))
    if observed:
        return observed
    primed_repo = _batch_repo_for_cwd(cwd)
    cached = (
        _PR_BATCH_CACHE.get((primed_repo, pr_number)) if primed_repo else None
    )
    if cached is not None and cached.get("head_sha"):
        return str(cached["head_sha"])
    return ""


def record_published_pr_head(
    pr_number: int, head_sha: str, cwd: str | None = None
) -> None:
    """Retain the published head already resolved by the worktree check."""
    repo = _repo_for_cwd(cwd) or ""
    if head_sha:
        _PUBLISHED_HEAD_OBSERVATIONS[(repo, pr_number)] = head_sha


def get_review_threads(
    pr_number: int,
    cwd: str | None = None,
    *,
    strict: bool = False,
) -> list[ReviewThread]:
    """Return all review threads for a PR via GraphQL.

    Paginates over ``reviewThreads`` (100 per page) so PRs with more than 100
    threads are not silently truncated — a hot review can easily exceed the
    first page, and a truncated thread list would let resolved-elsewhere or
    still-open threads slip past the caller's resolved/outdated filtering.

    A *first*-page failure returns ``[]`` by default for compatibility with
    observational callers. ``strict=True`` raises instead, which completion
    gates use so GitHub unavailability cannot synthesize green. Once a page has
    succeeded and advertised ``hasNextPage``, a failure fetching a *subsequent*
    page always raises :class:`RuntimeError` rather than returning a partial
    list: silently dropping later pages would let still-open threads slip past
    the unresolved-thread gate — the exact truncation hazard this pagination
    is meant to eliminate. A malformed page that reports ``hasNextPage=true``
    but omits/empties ``endCursor`` is treated the same way (we cannot advance,
    so raising beats truncating).

    BOU-2556: a hit in the batch-prefetch cache (see ``prime_pr_batch_cache``)
    short-circuits this whole call — no `gh` invocation at all.
    """
    primed_repo = _batch_repo_for_cwd(cwd)
    cached = (
        _PR_BATCH_CACHE.get((primed_repo, pr_number)) if primed_repo else None
    )
    if cached is not None and "threads" in cached:
        return list(cached["threads"])

    # Preserve the historical unprimed lookup path (and its strict failure
    # semantics). Only a primed dashboard tick bypasses this gh repository
    # lookup; ordinary completion-gate callers still resolve exactly as before.
    owner, repo = get_repo_info(cwd)
    if not owner or not repo:
        if strict:
            raise RuntimeError(
                "get_review_threads: could not resolve the repository; "
                "refusing to synthesize a clean review state"
            )
        return []

    repo_slug = f"{owner}/{repo}"
    cached = _PR_BATCH_CACHE.get((repo_slug, pr_number))
    if cached is not None and "threads" in cached:
        return list(cached["threads"])

    threads: list[ReviewThread] = []
    cursor: str | None = None
    paged: bool = False  # True once we've started fetching a non-first page
    while True:
        cmd = [
            "gh", "api", "graphql",
            "-f", f"query={_REVIEW_THREADS_QUERY}",
            "-F", f"owner={owner}",
            "-F", f"repo={repo}",
            "-F", f"pr={pr_number}",
        ]
        if cursor:
            cmd.extend(["-F", f"cursor={cursor}"])
        r = _run(cmd, cwd=cwd, timeout_s=30)
        if r.returncode != 0:
            if paged:
                raise RuntimeError(
                    f"get_review_threads: page after the first failed for PR "
                    f"#{pr_number} (gh exit {r.returncode}); refusing to return "
                    f"a partial thread list"
                )
            if strict:
                raise RuntimeError(
                    f"get_review_threads: first page failed for PR #{pr_number} "
                    f"(gh exit {r.returncode}); refusing to synthesize a clean "
                    "review state"
                )
            break
        try:
            data = json.loads(r.stdout)
            review_threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes = review_threads["nodes"]
            page_info = review_threads.get("pageInfo") or {}
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            if paged:
                raise RuntimeError(
                    f"get_review_threads: malformed page after the first for PR "
                    f"#{pr_number}; refusing to return a partial thread list"
                ) from exc
            if strict:
                raise RuntimeError(
                    f"get_review_threads: malformed first page for PR #{pr_number}; "
                    "refusing to synthesize a clean review state"
                ) from exc
            break

        threads.extend(_parse_review_thread_nodes(nodes))

        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            # GitHub claims another page but gave us no cursor to fetch it. We
            # cannot advance, so any thread on the unreachable page(s) would be
            # silently dropped — the exact truncation hazard this pagination is
            # meant to eliminate. Refuse to return a partial list.
            raise RuntimeError(
                f"get_review_threads: page for PR #{pr_number} reports "
                f"hasNextPage=true but no endCursor; refusing to return a "
                f"partial thread list"
            )
        cursor = next_cursor
        paged = True

    return threads


def get_review_submissions(
    pr_number: int,
    head_sha: str,
    cwd: str | None = None,
    *,
    excluded_authors: set[str] | None = None,
    strict: bool = False,
) -> list[ReviewSubmission]:
    """Return completed GitHub review evidence against ``head_sha``.

    The review coordinator requires affirmative evidence that the configured
    backstop ran for the immutable head. Review threads alone cannot provide
    that evidence because a clean review legitimately creates no thread. Most
    providers create a formal review submission; Codex reports a clean review
    as a top-level issue comment, so that explicit completion format is adapted
    into the same provider-neutral result.
    Completion gates use ``strict=True`` so an unavailable or malformed reviews
    response cannot synthesize a satisfied backstop slot. The optional Codex
    comment adapter fails to absent evidence instead of making Issues-read
    permission mandatory.
    """

    owner, repo = get_repo_info(cwd)
    if not owner or not repo:
        if strict:
            raise RuntimeError(
                "get_review_submissions: could not resolve the repository; "
                "refusing to synthesize completed review submissions"
            )
        return []

    result = _run(
        [
            "gh",
            "api",
            f"repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            "--paginate",
            "--slurp",
        ],
        cwd=cwd,
        timeout_s=30,
    )
    if result.returncode != 0:
        if strict:
            raise RuntimeError(
                f"get_review_submissions: GitHub review submissions failed for "
                f"PR #{pr_number} (gh exit {result.returncode})"
            )
        return []

    excluded = {
        _login_key(author)
        for author in (excluded_authors or set())
        if author.strip()
    }
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, list):
            raise TypeError("review response is not a list")
        pages = payload if not payload or isinstance(payload[0], list) else [payload]
        submissions: list[ReviewSubmission] = []
        for page in pages:
            if not isinstance(page, list):
                raise TypeError("review page is not a list")
            for item in page:
                if not isinstance(item, dict):
                    raise TypeError("review record is not an object")
                state = str(item.get("state") or "").upper()
                commit_id = str(item.get("commit_id") or "")
                if commit_id != head_sha or state not in {
                    "APPROVED",
                    "COMMENTED",
                    "CHANGES_REQUESTED",
                }:
                    continue
                submitted_at = item.get("submitted_at")
                body = item.get("body")
                user = item.get("user")
                author = user.get("login") if isinstance(user, dict) else None
                review_id = item.get("id")
                malformed = (
                    not isinstance(submitted_at, str)
                    or not submitted_at
                    or not isinstance(author, str)
                    or not author
                    or type(review_id) is not int
                    or (
                        body is not None
                        and not isinstance(body, str)
                    )
                )
                if malformed:
                    if strict:
                        raise RuntimeError(
                            "get_review_submissions: malformed current-head "
                            f"review record for PR #{pr_number}"
                        )
                    continue
                if _login_key(author) in excluded:
                    continue
                submissions.append(
                    ReviewSubmission(
                        review_id=review_id,
                        author=author,
                        state=state,
                        commit_id=commit_id,
                        submitted_at=submitted_at,
                        body=body or "",
                    )
                )
    except (json.JSONDecodeError, TypeError) as exc:
        if strict:
            raise RuntimeError(
                f"get_review_submissions: malformed review submissions for "
                f"PR #{pr_number}"
            ) from exc
        return []

    comments_result = _run(
        [
            "gh",
            "api",
            f"repos/{owner}/{repo}/issues/{pr_number}/comments",
            "--paginate",
            "--slurp",
        ],
        cwd=cwd,
        timeout_s=30,
    )
    if comments_result.returncode != 0:
        return sorted(
            submissions,
            key=lambda review: (
                review.submitted_at,
                review.source,
                review.review_id,
            ),
        )

    try:
        comments_payload = json.loads(comments_result.stdout)
        if not isinstance(comments_payload, list):
            raise TypeError("issue comment response is not a list")
        comment_pages = (
            comments_payload
            if not comments_payload or isinstance(comments_payload[0], list)
            else [comments_payload]
        )
        for page in comment_pages:
            if not isinstance(page, list):
                raise TypeError("issue comment page is not a list")
            for item in page:
                if not isinstance(item, dict):
                    raise TypeError("issue comment record is not an object")
                user = item.get("user")
                author = user.get("login") if isinstance(user, dict) else None
                if (
                    not isinstance(author, str)
                    or _login_key(author) != _CODEX_REVIEW_AUTHOR_KEY
                ):
                    continue
                body = item.get("body")
                if not isinstance(body, str) or not _CODEX_CLEAN_REVIEW_RE.match(body):
                    continue
                reviewed_commit = _REVIEWED_COMMIT_RE.search(body)
                if reviewed_commit is None:
                    continue
                commit_prefix = reviewed_commit.group(1).lower()
                if not head_sha.lower().startswith(commit_prefix):
                    continue
                if commit_prefix != head_sha.lower():
                    commit_result = _run(
                        [
                            "gh",
                            "api",
                            f"repos/{owner}/{repo}/commits/{commit_prefix}",
                        ],
                        cwd=cwd,
                        timeout_s=30,
                    )
                    if commit_result.returncode != 0:
                        continue
                    try:
                        commit_payload = json.loads(commit_result.stdout)
                        resolved_commit = (
                            commit_payload.get("sha")
                            if isinstance(commit_payload, dict)
                            else None
                        )
                    except json.JSONDecodeError:
                        continue
                    if resolved_commit != head_sha:
                        continue
                submitted_at = item.get("created_at")
                comment_id = item.get("id")
                malformed = (
                    not isinstance(submitted_at, str)
                    or not submitted_at
                    or type(comment_id) is not int
                )
                if malformed:
                    continue
                if _login_key(author) in excluded:
                    continue
                submissions.append(
                    ReviewSubmission(
                        review_id=comment_id,
                        author=author,
                        state="COMMENTED",
                        commit_id=head_sha,
                        submitted_at=submitted_at,
                        body="",
                        source="issue-comment",
                    )
                )
    except (json.JSONDecodeError, TypeError):
        pass

    return sorted(
        submissions,
        key=lambda review: (
            review.submitted_at,
            review.source,
            review.review_id,
        ),
    )


# ---------------------------------------------------------------------------
# Mutation pacing (BOU-1923 Bucket 4).
#
# Content mutations — resolving a review thread, replying to one, editing a
# comment — fired back-to-back (e.g. reply-then-resolve from the completion
# path) trip GitHub's velocity-triggered *secondary*/abuse rate limit even
# when the primary quota has headroom. `_run` already retries a rate-limited
# call with backoff (BOU-1921), but that is reactive; this adds a small
# PROACTIVE pacing gate so consecutive mutations don't fire back-to-back in
# the first place, plus one more Retry-After-honoring sleep+retry if a
# mutation is still rate-limited after `_run`'s own retries are exhausted.
# ---------------------------------------------------------------------------

_MUTATION_MIN_INTERVAL_S = float(os.environ.get("APD_MUTATION_MIN_INTERVAL_S", "1.0"))
_LAST_MUTATION_MONOTONIC: float | None = None


def reset_mutation_pacing() -> None:
    """Clear the last-mutation timestamp (test isolation)."""
    global _LAST_MUTATION_MONOTONIC
    _LAST_MUTATION_MONOTONIC = None


def _pace_mutation() -> None:
    """Block until at least ``_MUTATION_MIN_INTERVAL_S`` has elapsed since the
    previous mutation call in this process.

    Single-process pacing: it does not coordinate across processes, but the
    back-to-back bursts this guards against (reply immediately followed by
    resolve) originate from one completion-path call in one process."""
    global _LAST_MUTATION_MONOTONIC
    now = time.monotonic()
    if _LAST_MUTATION_MONOTONIC is not None:
        remaining = _MUTATION_MIN_INTERVAL_S - (now - _LAST_MUTATION_MONOTONIC)
        if remaining > 0:
            time.sleep(remaining)
            now = time.monotonic()
    _LAST_MUTATION_MONOTONIC = now


def _run_mutation(
    cmd: list[str], cwd: str | None = None, timeout_s: int = 20, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run a content-mutating ``gh`` call with pacing + a Retry-After-aware retry.

    Paces against the previous mutation (see :func:`_pace_mutation`), then
    delegates to :func:`_run` (which already retries connectivity/rate-limit
    failures internally, bounded by ``_GH_RETRY_ATTEMPTS``). If the result is
    STILL a rate limit afterward and carries a parseable ``Retry-After``, sleep
    that long (capped at ``_GH_RATELIMIT_MAX_SLEEP_S``) and retry exactly once
    more — mutations are not idempotent-safe to retry indefinitely, but a
    single bounded extra attempt clears the common secondary-limit case.

    ``env`` is forwarded to :func:`_run` / :func:`_run_once` unchanged (a full
    replacement environment, or ``None`` to inherit) — see :func:`resolve_review_thread`
    for the one caller that overrides it.
    """
    global _LAST_MUTATION_MONOTONIC
    _pace_mutation()
    result = _run(cmd, timeout_s=timeout_s, cwd=cwd, env=env)
    if _is_rate_limit_failure(result):
        hint = _retry_after_seconds(result.stderr or "")
        if hint is not None:
            time.sleep(min(hint, _GH_RATELIMIT_MAX_SLEEP_S))
            result = _run_once(cmd, timeout_s=timeout_s, cwd=cwd, env=env)
            # The Retry-After sleep + retry advanced wall-clock well past the
            # pacing stamp set in _pace_mutation(). Re-stamp to the retry time
            # so the NEXT mutation still waits a full interval — otherwise it
            # would see the (now-stale) pre-sleep stamp as already-elapsed and
            # fire immediately, reintroducing the very back-to-back burst this
            # pacing exists to prevent (BOU-1923 review).
            _LAST_MUTATION_MONOTONIC = time.monotonic()
    return result


# GitHub App installation tokens (the BOU-1923 automation identity) cannot call
# `resolveReviewThread` over GraphQL — GitHub answers with a FORBIDDEN GraphQL
# error even when the App has full pull-request permissions. This is a
# documented platform limitation of GitHub Apps, not a missing scope, so no
# amount of permission-granting fixes it. Left unhandled, the detached
# maintenance loop can read threads and post comments under the App token but
# never actually resolve them — it re-services already-fixed PRs forever and
# never converges. The substrings below are how `gh api graphql` reports it.
_FORBIDDEN_INTEGRATION_STDERR_PATTERNS = (
    "not accessible by integration",
    "resource not accessible by integration",
)

# Logged once per process, not once per PR/thread, so a loop resolving many
# threads under the App token doesn't spam stderr on every single one.
_RESOLVE_FALLBACK_LOGGED = False


def reset_resolve_fallback_logged() -> None:
    """Reset the resolve-fallback once-per-process log flag (test isolation)."""
    global _RESOLVE_FALLBACK_LOGGED
    _RESOLVE_FALLBACK_LOGGED = False


def _is_forbidden_integration_failure(result: subprocess.CompletedProcess) -> bool:
    """True when a failed gh call is GitHub's App-token GraphQL resolve block."""
    if result.returncode == 0:
        return False
    stderr = (result.stderr or "").lower()
    return any(pat in stderr for pat in _FORBIDDEN_INTEGRATION_STDERR_PATTERNS)


def _resolve_fallback_env() -> dict[str, str] | None:
    """Build a per-call environment for retrying a FORBIDDEN resolve mutation.

    Precedence:
      1. ``AGENTIC_PR_DASH_GH_RESOLVE_TOKEN``, if set — a dedicated
         resolve-capable identity (e.g. a machine-user PAT) swapped in for
         ``GH_TOKEN`` so operators don't have to spend a human's quota/session.
      2. Otherwise, drop ``GH_TOKEN`` entirely so the subprocess falls back to
         `gh`'s ambient/keyring-authenticated identity (the human operator's),
         which CAN resolve threads.

    ``gh`` reads ``GH_TOKEN`` first and ``GITHUB_TOKEN`` second. ``GITHUB_TOKEN``
    is commonly set to the SAME App/integration token (GitHub Actions injects it;
    wrapper shells re-export it), so dropping only ``GH_TOKEN`` would let ``gh``
    silently fall through to ``GITHUB_TOKEN`` — the very App token that just got
    FORBIDDEN — defeating the fallback. So we ALWAYS remove ``GITHUB_TOKEN`` from
    the per-call env: in the ambient case it must not shadow the keyring
    identity, and in the resolve-token case it must not shadow the PAT we set in
    ``GH_TOKEN``.

    Returns ``None`` when ``GH_TOKEN`` isn't set in this process's environment
    to begin with — there's no App token to fall back FROM, so the FORBIDDEN
    has some other cause and retrying with a different env can't help.

    Builds a full copy of ``os.environ`` (subprocess.run's ``env=`` replaces
    the whole environment, it doesn't merge) so this is safe to pass straight
    to ``_run_mutation`` without touching the real process environment —
    every other call in this process still sees the original ``GH_TOKEN``.
    """
    if "GH_TOKEN" not in os.environ:
        return None
    env = dict(os.environ)
    # Never let GITHUB_TOKEN shadow the identity we're switching to (see above).
    env.pop("GITHUB_TOKEN", None)
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    token_file = config_home / "agentic-pr-dash" / "gh-resolve-token"
    try:
        file_token = token_file.read_text(encoding="utf-8").strip()
    except OSError:
        file_token = ""
    resolve_token = file_token or os.environ.get("AGENTIC_PR_DASH_GH_RESOLVE_TOKEN")
    if resolve_token:
        env["GH_TOKEN"] = resolve_token
    else:
        del env["GH_TOKEN"]
    return env


def resolve_review_thread(thread_id: str, cwd: str | None = None) -> bool:
    """Resolve a review thread via GraphQL mutation.

    See the App-token FORBIDDEN note above `_FORBIDDEN_INTEGRATION_STDERR_PATTERNS`:
    when the first attempt fails that way, retry the SAME mutation once with a
    resolve-capable identity (`_resolve_fallback_env`) instead of giving up —
    reads/comments stay on the App's own budget either way; only this
    low-volume resolve call ever uses the fallback identity.
    """
    cmd = [
        "gh", "api", "graphql",
        "-f", f"query={_RESOLVE_THREAD_MUTATION}",
        "-F", f"id={thread_id}",
    ]
    r = _run_mutation(cmd, cwd=cwd, timeout_s=20)
    if r.returncode == 0:
        return True
    if not _is_forbidden_integration_failure(r):
        return False
    fallback_env = _resolve_fallback_env()
    if fallback_env is None:
        return False
    global _RESOLVE_FALLBACK_LOGGED
    if not _RESOLVE_FALLBACK_LOGGED:
        print(
            "agentic-pr-dash: resolveReviewThread FORBIDDEN under GH_TOKEN "
            "(GitHub App tokens can't resolve review threads — platform "
            "limitation, not a permission gap) — retrying with a "
            "resolve-capable identity.",
            file=sys.stderr,
        )
        _RESOLVE_FALLBACK_LOGGED = True
    r2 = _run_mutation(cmd, cwd=cwd, timeout_s=20, env=fallback_env)
    return r2.returncode == 0


def edit_review_comment(comment_id: int, body: str, cwd: str | None = None) -> bool:
    """Edit an existing review comment in place via REST PATCH."""
    r = _run_mutation(
        [
            "gh", "api", "-X", "PATCH",
            f"repos/{{owner}}/{{repo}}/pulls/comments/{comment_id}",
            "-f", f"body={body}",
        ],
        cwd=cwd,
        timeout_s=20,
    )
    return r.returncode == 0


def get_commit_changed_files(sha: str, cwd: str | None = None) -> list[str]:
    """Return list of filenames changed by a commit.

    Prefers local git (immediate, no API-indexing lag — BOU-1479); falls back to
    the GitHub API when the commit isn't in the local history.
    """
    try:
        # `-c core.quotePath=false` so non-ASCII paths come back as their literal
        # decoded names (e.g. `café.py`, not `"caf\303\251.py"`); otherwise they
        # never match GitHub's decoded review-thread `path` and addressed inline
        # threads on those files stay open after a just-pushed fix.
        lr = subprocess.run(
            ["git", "-C", cwd or ".", "-c", "core.quotePath=false",
             "show", "--name-only", "--format=", sha],
            capture_output=True, text=True, timeout=10,
        )
        if lr.returncode == 0:
            files = [ln.strip() for ln in lr.stdout.splitlines() if ln.strip()]
            if files:
                return files
    except (OSError, subprocess.SubprocessError):
        pass

    r = _run(
        [
            "gh", "api",
            f"repos/{{owner}}/{{repo}}/commits/{sha}",
            "--jq", ".files[].filename",
        ],
        cwd=cwd,
        timeout_s=20,
    )
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.splitlines() if line.strip()]


# `@@ -old_start[,old_count] +new_start[,new_count] @@` (git unified hunk header)
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def get_changed_line_spans(
    base_sha: str,
    head_sha: str,
    path: str,
    cwd: str | None = None,
) -> list[tuple[int, int, int, int]] | None:
    """Hunk line spans changed for ``path`` in ``base_sha..head_sha`` (local git).

    Returns a list of ``(old_start, old_end, new_start, new_end)`` tuples, one
    per hunk, parsed from a zero-context diff. A side with a zero line count
    (pure insertion/deletion) yields an EMPTY span encoded as ``end < start``
    (``(start, start - 1)``) so callers can tell "no lines on this side" apart
    from a one-line change.

    Returns ``None`` when the diff is unavailable (missing SHAs, git failure,
    SHAs not in local history). Callers MUST treat ``None`` as "no evidence" —
    never as "nothing changed" (BOU-2095: absence of proof must not resolve
    review threads).
    """
    if not base_sha or not head_sha or not path:
        return None
    try:
        r = subprocess.run(
            ["git", "-C", cwd or ".", "-c", "core.quotePath=false",
             "diff", "--unified=0", f"{base_sha}..{head_sha}", "--", path],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    spans: list[tuple[int, int, int, int]] = []
    for line in r.stdout.splitlines():
        m = _HUNK_HEADER_RE.match(line)
        if not m:
            continue
        old_start = int(m.group(1))
        old_count = int(m.group(2)) if m.group(2) is not None else 1
        new_start = int(m.group(3))
        new_count = int(m.group(4)) if m.group(4) is not None else 1
        spans.append((
            old_start, old_start + old_count - 1,
            new_start, new_start + new_count - 1,
        ))
    return spans


def get_ci_checks(pr_number: int, cwd: str | None = None) -> list[CICheck]:
    """Get CI check status for a PR.

    ``gh pr checks`` exits **non-zero** precisely when there is something to
    report — 8 while checks are pending, 1 when a check has failed — yet still
    prints the full ``--json`` array to stdout in those cases. Returning ``[]``
    on any non-zero rc therefore DROPPED exactly the failing/pending checks the
    maintenance gate needs (a pending→fail flip would read as "no checks", so
    ``failing_checks`` stayed empty and the await waiter never woke on failure —
    codex PR #50 review). Parse stdout regardless of the exit code.

    Unparseable stdout is NOT always "a real gh error meaning no checks":
    under a GitHub App installation token without **Actions: Read-only**, gh's
    underlying GraphQL requests ``checkSuite.workflowRun`` and dies with
    "Resource not accessible by integration" and EMPTY stdout — for every PR,
    forever. Treating that as ``[]`` made the dashboard silently blind to CI
    (every PR with running/failing CI read as Clean — BOU-1980). Fall back to
    the REST check-runs API, which needs only Checks: Read.
    """
    repo = _repo_for_cwd(cwd)
    cached = _PR_BATCH_CACHE.get((repo, pr_number)) if repo else None
    if cached is not None and "ci_checks" in cached:
        return _ObservedList(list(cached["ci_checks"]), observable=True)

    r = _run(
        ["gh", "pr", "checks", str(pr_number),
         "--json", "name,bucket,state"],
        cwd=cwd, timeout_s=30,
    )
    try:
        raw = json.loads(r.stdout or "")
    except (json.JSONDecodeError, TypeError):
        raw = None
    if not isinstance(raw, list):
        return _get_ci_checks_rest(pr_number, cwd)

    # Dedup by name (keep latest)
    by_name: dict[str, dict] = {}
    for c in raw:
        if isinstance(c, dict) and c.get("name"):
            by_name[c["name"]] = c

    checks = []
    for c in by_name.values():
        bucket = c.get("bucket", "")
        state = c.get("state", "")
        # Map gh bucket/state to our model
        if bucket == "fail":
            conclusion = "failure"
            status = "completed"
        elif bucket == "pass":
            conclusion = "success"
            status = "completed"
        elif bucket == "pending":
            conclusion = None
            status = "in_progress"
        elif bucket == "cancel":
            conclusion = "cancelled"
            status = "completed"
        else:
            conclusion = None
            status = state or "unknown"
        checks.append(CICheck(name=c.get("name", "?"), status=status, conclusion=conclusion))
    return _ObservedList(checks, observable=True)


def get_ci_checks_observation(
    pr_number: int,
    cwd: str | None = None,
) -> ObservationReadResult[list[CICheck]]:
    """Return CI checks without collapsing an unavailable read into clean."""
    try:
        value = get_ci_checks(pr_number, cwd)
    except Exception as exc:  # noqa: BLE001
        return ObservationReadResult.unavailable(f"CI observation raised: {exc}")
    if isinstance(value, ObservationReadResult):
        return value
    if isinstance(value, _ObservedList):
        return ObservationReadResult(
            value=list(value),
            observable=value.observable,
            error=value.error,
        )
    return ObservationReadResult.observed(list(value))


# REST check-run conclusions that ``gh pr checks`` buckets as ``fail`` — must
# normalize to "failure" so orchestrator ``failing_checks`` (which matches
# ``conclusion == "failure"`` exactly) behaves identically on both paths.
# ``stale`` is terminal-but-not-success: the post-push watcher already treats
# it as blocking, so the fallback must too (codex PR #71 review).
_REST_FAIL_CONCLUSIONS = {"failure", "timed_out", "startup_failure", "action_required", "stale"}

# The only non-terminal statuses downstream pending predicates match
# (orchestrator/_compute_status and the board test ``queued``/``in_progress``
# exactly; the primary path collapses gh's whole pending bucket to
# ``in_progress``). REST also emits ``waiting``/``requested``/``pending`` —
# anything else non-completed must normalize into this set or a pending PR
# reads as clean (codex PR #71 review).
_REST_PENDING_STATUSES = ("queued", "in_progress")


def _get_ci_checks_rest(pr_number: int, cwd: str | None = None) -> list[CICheck]:
    """REST fallback for :func:`get_ci_checks` (BOU-1980).

    Requires only the **Checks: Read** permission — available to the automation
    App token even when the Actions grant that gh's GraphQL rollup needs is
    missing. Resolves the PR's ACTUAL head SHA via the REST ``pulls/{n}``
    endpoint (``get_latest_commit``'s ``pulls/{n}/commits`` read is unpaginated,
    so ``.[-1]`` is the 30th commit on big PRs — a stale SHA whose checks can
    read as clean), then reuses :func:`get_check_runs_for_commit`, which
    paginates ``/check-runs`` AND folds in legacy commit StatusContexts so
    external-CI repos aren't seen as check-less (codex PR #71 review).
    """
    r = _run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/pulls/{pr_number}",
         "--jq", ".head.sha"],
        cwd=cwd, timeout_s=30,
    )
    sha = (r.stdout or "").strip()
    if r.returncode != 0 or not sha:
        # Head-SHA resolution failed: the blocker-status read is UNOBSERVABLE,
        # not "no checks". Record it so tick consumers (await clean exit /
        # stop-gate marker skip) don't treat the empty result as a positive
        # clean observation — this is the path where BOTH the `gh pr checks`
        # JSON read and the REST fallback died (codex PR #75 review, round 5).
        _note_checks_probe_failure()
        return _ObservedList([], observable=False, error="CI head SHA unavailable")

    return _get_ci_checks_rest_for_sha(sha, cwd)


def _get_ci_checks_rest_for_sha(
    sha: str,
    cwd: str | None = None,
) -> list[CICheck]:
    """Read CI from REST endpoints for an already-cached PR head."""

    runs = get_check_runs_for_commit(sha, cwd)
    if isinstance(runs, _ObservedList) and not runs.observable:
        return _ObservedList([], observable=False, error="CI status reads unavailable")
    checks = []
    for run in runs:
        status = run.get("status") or ""
        conclusion = run.get("conclusion")
        if status != "completed" and status not in _REST_PENDING_STATUSES:
            status = "in_progress"
        if conclusion in _REST_FAIL_CONCLUSIONS:
            conclusion = "failure"
        checks.append(CICheck(name=run.get("name", "?"), status=status, conclusion=conclusion))
    return _ObservedList(checks, observable=True)


def get_ci_checks_rest_observation(
    head_sha: str,
    cwd: str | None = None,
    *,
    pr_number: int | None = None,
) -> ObservationReadResult[list[CICheck]]:
    """Return a typed, REST-only CI read for a cached immutable head.

    Pending CI polls are frequent and must not fall back to ``gh pr checks``
    (which is backed by the expensive GraphQL rollup).  The orchestrator passes
    the head SHA already present in its metadata/observation cache, so this
    boundary performs only ``check-runs`` and combined commit-status REST reads.
    Test adapters that replace the historical ``get_ci_checks`` boundary keep
    their compatibility behavior, while production always uses this path.
    """

    if not head_sha:
        return ObservationReadResult.unavailable("CI observation unavailable: cached head SHA missing")
    try:
        if get_ci_checks.__module__ != __name__:
            value = get_ci_checks(pr_number or 0, cwd)
            if isinstance(value, ObservationReadResult):
                return value
            if isinstance(value, _ObservedList):
                return ObservationReadResult(
                    value=list(value), observable=value.observable, error=value.error
                )
            return ObservationReadResult.observed(list(value))
        value = _get_ci_checks_rest_for_sha(head_sha, cwd)
    except Exception as exc:  # noqa: BLE001
        return ObservationReadResult.unavailable(f"REST CI observation raised: {exc}")
    if isinstance(value, _ObservedList):
        return ObservationReadResult(
            value=list(value), observable=value.observable, error=value.error
        )
    return ObservationReadResult.observed(list(value))


# GraphQL statusCheckRollup: per-context required-ness + non-terminal state.
# Used instead of ``gh pr checks --required`` because the CLI (a) exits 8 while
# checks are pending and (b) OMITS required checks that are merely *expected*
# (configured by branch protection but not yet reported — cli/cli#8855), both of
# which misclassify the exact "CI hasn't started / is running" case BOU-1789
# must keep watching to a terminal state.
_REQUIRED_ROLLUP_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!,$after:String){"
    "repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "commits(last:1){nodes{commit{statusCheckRollup{contexts(first:100,after:$after){"
    "pageInfo{hasNextPage endCursor} nodes{"
    "__typename "
    "... on CheckRun{status isRequired(pullRequestNumber:$number)} "
    "... on StatusContext{state isRequired(pullRequestNumber:$number)}"
    "}}}}}}}}}"
)

# A PR with hundreds of contexts is pathological; cap pages so a malformed
# never-ending cursor can't spin.
_ROLLUP_MAX_PAGES = 20


def _ctx_is_nonterminal(ctx: dict) -> bool:
    """True when this context has not reached a terminal state, required or not."""
    if not isinstance(ctx, dict):
        return False
    if ctx.get("__typename") == "CheckRun":
        return ctx.get("status") != "COMPLETED"
    if ctx.get("__typename") == "StatusContext":
        return ctx.get("state") in ("EXPECTED", "PENDING")
    return False


def _ctx_is_pending(ctx: dict) -> bool:
    if not isinstance(ctx, dict) or not ctx.get("isRequired"):
        return False
    return _ctx_is_nonterminal(ctx)


def _repo_for_cwd(cwd: str | None) -> str | None:
    """``owner/name`` for the repo at ``cwd``, preferring the cwd's OWN remote.

    ``resolved_repo`` returns a pinned/global ``repo`` (e.g. from
    ``AGENTIC_PR_DASH_REPO`` or a shared config) without consulting ``cwd``, so
    in a multi-repo session it would resolve a sibling worktree's PR against the
    anchor repo. Detect from the cwd's git remote first and only fall back to the
    config-resolved repo (codex PR #50 review)."""
    from .config import _detect_repo  # noqa: PLC0415
    cached = _batch_repo_for_cwd(cwd)
    if cached:
        return cached
    if cwd:
        detected = _detect_repo(Path(cwd))
        if detected:
            return detected
    return load_config(cwd).resolved_repo(Path(cwd) if cwd else None)


def _required_checks_pending_live(pr_number: int, cwd: str | None = None) -> bool:
    """True iff a required/merge-gating check is still non-terminal.

    Reads the GraphQL ``statusCheckRollup`` and inspects per-context
    ``isRequired``. A required context is pending when a ``CheckRun`` is not yet
    ``COMPLETED`` (queued/in_progress/waiting/requested) or a ``StatusContext``
    is ``EXPECTED``/``PENDING`` — the ``EXPECTED`` state covers required checks
    configured by branch protection that have not reported yet (the cli/cli#8855
    gap that ``gh pr checks`` misses).

    When the rollup declares NO required context at all, every non-terminal check
    counts instead (BOU-2294). ``isRequired`` reflects branch protection, and a
    repo that gates merges by convention rather than by a protection rule reports
    ``isRequired: false`` for every check — so the strict reading made this
    function answer "CI is terminal" while the whole suite was still running, and
    the waiter clean-exited seconds after a push. That answer was already
    asymmetric with failure detection, which counts ANY failing check
    (``_pr_open_state`` → ``get_ci_checks``) with no ``isRequired`` filter: CI
    could only ever be found red, never found running. The fallback is scoped to
    "no required contexts exist" so a repo that DOES declare required checks keeps
    the narrow semantics and its waiters are not held open by optional jobs.

    Paginates the contexts so a required pending context past the first 100
    isn't missed — and so the "no required contexts" question is answered over
    the WHOLE rollup, not just its first page. Returns ``False`` on any
    error or when no required check is pending (fail-safe) — but a gh/parse
    failure additionally records ``checks_probe_failure_seen()`` so tick-based
    consumers (the await waiter's clean exit) can tell "observed terminal" from
    "probe failed" (codex PR #75 review). A ``null`` rollup is a VALID
    observation (no status checks on the head commit), not a failure.
    """
    repo = _repo_for_cwd(cwd)
    if not repo or "/" not in repo:
        return False
    owner, name = repo.split("/", 1)

    # ``repo`` ("owner/name") is the SAME string shape ``get_review_threads``'s
    # cache check uses (``f"{owner}/{repo}"`` from ``get_repo_info``) — both
    # resolve to the repo's GitHub identity and agree in the overwhelming
    # common case, so priming under one hits the other. Deliberately reuses the
    # ALREADY-COMPUTED ``repo`` above rather than calling a second resolver: a
    # miss just falls through to the unchanged live query below (lost
    # optimization, never a wrong answer), so it costs nothing to be wrong.
    cached = _PR_BATCH_CACHE.get((repo, pr_number))
    if cached is not None and "required_pending" in cached:
        return bool(cached["required_pending"])

    after: str | None = None
    # Accumulated across pages: the "no required contexts anywhere" fallback can
    # only be decided once every page has been seen.
    saw_required = False
    unrequired_pending = False
    for _ in range(_ROLLUP_MAX_PAGES):
        cmd = ["gh", "api", "graphql",
               "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={pr_number}",
               "-f", f"query={_REQUIRED_ROLLUP_QUERY}"]
        if after:
            cmd += ["-F", f"after={after}"]
        r = _run(cmd, cwd=cwd, timeout_s=30)
        if r.returncode != 0:
            _note_checks_probe_failure()
            return False
        try:
            data = json.loads(r.stdout or "{}")
            rollup = (data["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]
                      ["commit"]["statusCheckRollup"])
        except (json.JSONDecodeError, KeyError, TypeError, IndexError):
            _note_checks_probe_failure()
            return False
        if rollup is None:
            return False  # observed: no status checks on the head commit
        try:
            contexts = rollup["contexts"]
        except (KeyError, TypeError):
            _note_checks_probe_failure()
            return False
        for ctx in contexts.get("nodes", []):
            if not isinstance(ctx, dict):
                continue
            if ctx.get("isRequired"):
                saw_required = True
                if _ctx_is_pending(ctx):
                    return True
            elif _ctx_is_nonterminal(ctx):
                unrequired_pending = True
        page = contexts.get("pageInfo") or {}
        if not page.get("hasNextPage") or not page.get("endCursor"):
            return unrequired_pending and not saw_required
        after = page["endCursor"]
    # Fell out of the page loop with hasNextPage still true: the rollup was
    # TRUNCATED at _ROLLUP_MAX_PAGES, so a required pending context on a later
    # page may exist — this is an incomplete observation, not terminal CI
    # (codex PR #75 review, round 2).
    _note_checks_probe_failure()
    return False


_REQUIRED_CHECK_OBSERVATIONS: dict[tuple[str, int], bool] = {}
_PUBLISHED_HEAD_OBSERVATIONS: dict[tuple[str, int], str] = {}


def reset_required_check_observations() -> None:
    """Clear per-tick required-check observations retained for gate reuse."""
    _REQUIRED_CHECK_OBSERVATIONS.clear()
    _PUBLISHED_HEAD_OBSERVATIONS.clear()


def required_checks_pending(pr_number: int, cwd: str | None = None) -> bool:
    """Observe required CI and retain the result for other readers this tick."""
    pending = _required_checks_pending_live(pr_number, cwd)
    repo = _repo_for_cwd(cwd) or ""
    _REQUIRED_CHECK_OBSERVATIONS[(repo, pr_number)] = pending
    return pending


def observed_required_checks_pending(
    pr_number: int, cwd: str | None = None
) -> bool | None:
    """Return an already-paid-for CI observation, or ``None`` if absent."""
    repo = _repo_for_cwd(cwd) or ""
    return _REQUIRED_CHECK_OBSERVATIONS.get((repo, pr_number))


# A batch call covering too many PRs at once risks a pathologically expensive
# GraphQL query (node-count limits) for no real benefit — a session owning
# dozens of PRs is not the case this exists for. Chunk conservatively; each
# chunk is still exactly ONE round trip.
_BATCH_CHUNK_SIZE = 15
# The review/CI batch query requests up to 100 threads and 100 contexts per PR.
# Keep admission conservative before the first rate-limit sample (or after a
# cheap unrelated query); callers may still provide a higher estimate.
BATCH_GRAPHQL_ESTIMATED_COST: Final[int] = 50
BATCH_GRAPHQL_FAILURE_BACKOFF: Final[timedelta] = timedelta(seconds=30)


def repo_slug_for_prefetch(cwd: str | None) -> str:
    """``"owner/name"`` for ``cwd``, resolved the SAME way ``required_checks_pending``
    resolves it (``_repo_for_cwd``) — the canonical grouping/priming key for the
    stop-gate's batch prefetch (BOU-2556). Public so orchestration outside this
    module (``_maintenance.stop_gate``) can group owned worktrees by repo
    without duplicating (or drifting from) the resolution logic."""
    return _repo_for_cwd(cwd) or ""


def _batch_ci_checks(contexts: list[dict]) -> list[CICheck]:
    """Normalize GraphQL rollup contexts to the same model as ``gh pr checks``."""
    checks: list[CICheck] = []
    for context in contexts:
        if not isinstance(context, dict):
            continue
        if context.get("__typename") == "CheckRun":
            status = str(context.get("status") or "unknown").lower()
            conclusion_raw = str(context.get("conclusion") or "").lower()
            conclusion = conclusion_raw or None
            if status != "completed":
                status = "in_progress"
                conclusion = None
            if conclusion in _REST_FAIL_CONCLUSIONS:
                conclusion = "failure"
            checks.append(CICheck(
                name=str(context.get("name") or "?"),
                status=status,
                conclusion=conclusion,
            ))
        elif context.get("__typename") == "StatusContext":
            state = str(context.get("state") or "unknown").lower()
            if state in ("expected", "pending"):
                status, conclusion = "in_progress", None
            elif state == "success":
                status, conclusion = "completed", "success"
            else:
                status, conclusion = "completed", "failure"
            checks.append(CICheck(
                name=str(context.get("context") or "?"),
                status=status,
                conclusion=conclusion,
            ))
    return checks


def batch_fetch_pr_review_and_ci(
    owner: str,
    repo: str,
    pr_numbers: list[int],
    cwd: str | None = None,
    *,
    quota_context: QuotaContext | None = None,
    quota_ledger: QuotaLedger | None = None,
    caller: QuotaCaller = QuotaCaller.DASHBOARD,
    work_class: QuotaWorkClass = QuotaWorkClass.BACKGROUND_OBSERVATION,
) -> dict[int, dict]:
    """Fetch review threads + the required-checks rollup for MANY PRs in as few
    round trips as possible (BOU-2556).

    One aliased GraphQL query per chunk of ``pr_numbers`` (see
    ``_BATCH_CHUNK_SIZE``) fetches, for each PR, its first page of review
    threads and the required-checks rollup off its latest commit — the same
    two queries :func:`get_review_threads` and :func:`required_checks_pending`
    would otherwise each make PER PR. A session owning N PRs in one repo used
    to cost >= 2N round trips serially; this costs ``ceil(N / _BATCH_CHUNK_SIZE)``.

    Returns ``{pr_number: {"threads": [...], "required_pending": bool}}`` — only
    for PRs this call could fully resolve. A PR is DELIBERATELY OMITTED (never
    given a wrong/partial answer) when:
      * the whole chunk's `gh` call failed or returned malformed JSON,
      * the PR wasn't found under this repo (bad number, wrong repo), or
      * either its review-thread page or its rollup-context page reports
        ``hasNextPage`` (more than 100 threads/contexts) — the batch query only
        fetches page one, so a truncated read here would silently narrow what
        :func:`get_review_threads` promises callers (no dropped pages, ever).
    Callers (``_stop_gate_impl``'s prefetch) treat an omitted PR exactly like a
    cache miss. Quota-aware dashboard callers additionally inspect the typed
    ``denied`` flag: a malformed chunk activates bounded backoff and suppresses
    immediate per-PR GraphQL fan-out. This function itself never raises — any
    failure means fewer PRs got batched, never a wrong answer for an entry.

    When ``quota_context`` (or ``quota_ledger`` plus attribution arguments) is
    supplied, the top-level GraphQL ``rateLimit`` sample is recorded once for
    every successful chunk. A denied context skips that chunk and returns the
    successfully observed subset.
    """
    if not owner or not repo or not pr_numbers:
        return {}

    if quota_context is not None and quota_ledger is not None:
        raise ValueError("pass quota_context or quota_ledger, not both")
    if quota_context is None and quota_ledger is not None:
        quota_context = QuotaContext(
            ledger=quota_ledger,
            caller=caller,
            work_class=work_class,
        )

    results: _BatchObservationDict = _BatchObservationDict()

    def deny_invalid_response(
        reason: str, *, count_request: bool = True
    ) -> None:
        if quota_context is not None:
            quota_context.ledger.record_failure(
                reason=reason,
                count_request=count_request,
            )
            quota_context.ledger.record_backoff(
                BATCH_GRAPHQL_FAILURE_BACKOFF,
                reason=reason,
            )
        results.denied = True
        results.error = reason

    numbers = sorted(set(pr_numbers))
    for start in range(0, len(numbers), _BATCH_CHUNK_SIZE):
        chunk = numbers[start:start + _BATCH_CHUNK_SIZE]
        fields = []
        for n in chunk:
            fields.append(
                f'pr_{n}: pullRequest(number: {n}) {{'
                f'  headRefOid mergeStateStatus mergeable reviewDecision'
                f'  reviewThreads(first: 100) {{'
                f'    pageInfo {{ hasNextPage }}'
                f'    nodes {{'
                f'      id isResolved isOutdated'
                f'      comments(first: 100) {{'
                f'        nodes {{ databaseId path line originalLine body author {{ login }} createdAt pullRequestReview {{ databaseId }} }}'
                f'      }}'
                f'    }}'
                f'  }}'
                f'  commits(last: 1) {{'
                f'    nodes {{ commit {{ oid committedDate statusCheckRollup {{ contexts(first: 100) {{'
                f'      pageInfo {{ hasNextPage }}'
                f'      nodes {{'
                f'        __typename'
                f'        ... on CheckRun {{ name status conclusion isRequired(pullRequestNumber: {n}) }}'
                f'        ... on StatusContext {{ context state isRequired(pullRequestNumber: {n}) }}'
                f'      }}'
                f'    }} }} }} }}'
                f'  }}'
                f'}}'
            )
        query = (
            "query($owner: String!, $repo: String!) { "
            "rateLimit { cost remaining resetAt limit } "
            "repository(owner: $owner, name: $repo) { "
            + " ".join(fields) +
            " } }"
        )
        cmd = [
            "gh", "api", "graphql",
            "-f", f"query={query}",
            "-F", f"owner={owner}",
            "-F", f"repo={repo}",
        ]

        reservation = None
        reserved_estimated_cost = 0
        if quota_context is not None:
            estimated_cost = max(
                quota_context.estimated_cost,
                BATCH_GRAPHQL_ESTIMATED_COST,
            )
            latest = quota_context.ledger.latest
            if latest is not None and latest.cost > 0:
                estimated_cost = max(estimated_cost, latest.cost)
            reserved_estimated_cost = estimated_cost
            reservation = quota_context.ledger.reserve(
                quota_context.caller,
                quota_context.work_class,
                estimated_cost=estimated_cost,
            )
            if reservation is None:
                results.denied = True
                results.error = "dashboard quota denied batch observation"
                continue

        try:
            r = _run(cmd, cwd=cwd, timeout_s=45)
            if r.returncode != 0:
                if quota_context is not None:
                    quota_context.ledger.record_failure(
                        reason="graphql_request_failed",
                    )
                    quota_context.ledger.record_backoff(
                        BATCH_GRAPHQL_FAILURE_BACKOFF,
                        reason="graphql_request_failed",
                    )
                results.denied = True
                results.error = "graphql_request_failed"
                continue
            try:
                data = json.loads(r.stdout)
                data_node = data["data"]
            except (json.JSONDecodeError, KeyError, TypeError):
                if quota_context is not None and reservation is not None:
                    quota_context.ledger.record_estimated(
                        quota_context.caller,
                        quota_context.work_class,
                        reserved_estimated_cost,
                        reservation=reservation,
                    )
                    reservation = None
                deny_invalid_response(
                    "graphql_response_invalid",
                    count_request=False,
                )
                continue

            rate_limit_recorded = False
            if quota_context is not None:
                try:
                    rate_limit = data_node["rateLimit"]
                    quota_context.ledger.record_graphql(
                        caller=quota_context.caller,
                        work_class=quota_context.work_class,
                        cost=int(rate_limit["cost"]),
                        remaining=int(rate_limit["remaining"]),
                        reset_at=rate_limit["resetAt"],
                        limit=int(rate_limit["limit"]),
                        reservation=reservation,
                    )
                    reservation = None
                    rate_limit_recorded = True
                except (KeyError, TypeError, ValueError, OverflowError):
                    if reservation is not None:
                        quota_context.ledger.record_estimated(
                            quota_context.caller,
                            quota_context.work_class,
                            reserved_estimated_cost,
                            reservation=reservation,
                        )
                        reservation = None
                    deny_invalid_response(
                        "graphql_rate_limit_invalid",
                        count_request=False,
                    )
                    continue

            try:
                repo_node = data_node["repository"]
            except (KeyError, TypeError):
                deny_invalid_response(
                    "graphql_response_invalid",
                    count_request=not rate_limit_recorded,
                )
                continue
            if not isinstance(repo_node, dict):
                deny_invalid_response(
                    "graphql_repository_invalid",
                    count_request=not rate_limit_recorded,
                )
                continue
        finally:
            if quota_context is not None and reservation is not None:
                quota_context.ledger.release(reservation)

        for n in chunk:
            pr_node = repo_node.get(f"pr_{n}")
            if not isinstance(pr_node, dict):
                continue
            try:
                review_threads = pr_node["reviewThreads"]
                thread_nodes = review_threads["nodes"]
                threads_truncated = bool(
                    (review_threads.get("pageInfo") or {}).get("hasNextPage")
                )
            except (KeyError, TypeError):
                continue
            if threads_truncated:
                continue
            threads = _parse_review_thread_nodes(thread_nodes)

            required_pending = False
            try:
                commit_nodes = pr_node["commits"]["nodes"]
                rollup = (
                    commit_nodes[0]["commit"]["statusCheckRollup"]
                    if commit_nodes else None
                )
            except (KeyError, TypeError, IndexError):
                continue
            if rollup is not None:
                try:
                    contexts = rollup["contexts"]
                    ctx_truncated = bool(
                        (contexts.get("pageInfo") or {}).get("hasNextPage")
                    )
                except (KeyError, TypeError):
                    continue
                if ctx_truncated:
                    continue
                saw_required = False
                unrequired_pending = False
                for ctx in contexts.get("nodes", []):
                    if not isinstance(ctx, dict):
                        continue
                    if ctx.get("isRequired"):
                        saw_required = True
                        if _ctx_is_pending(ctx):
                            required_pending = True
                            break
                    elif _ctx_is_nonterminal(ctx):
                        unrequired_pending = True
                if not required_pending:
                    required_pending = unrequired_pending and not saw_required

            commit = commit_nodes[0]["commit"] if commit_nodes else {}
            ci_checks = _batch_ci_checks(
                (rollup or {}).get("contexts", {}).get("nodes", []) if rollup else []
            )
            results[n] = {
                "threads": threads,
                "required_pending": required_pending,
                "latest_commit": (
                    str(pr_node.get("headRefOid") or commit.get("oid") or ""),
                    str(commit.get("committedDate") or ""),
                ),
                "ci_checks": ci_checks,
                "head_sha": str(pr_node.get("headRefOid") or commit.get("oid") or ""),
                "merge_state": str(pr_node.get("mergeStateStatus") or "unknown"),
                "mergeable": str(pr_node.get("mergeable") or "unknown"),
                "review_decision": str(pr_node.get("reviewDecision") or "none"),
            }
    return results


def collect_pr_maintenance_snapshots(
    owner: str, repo: str, pr_numbers: list[int], cwd: str | None = None,
) -> PrMaintenanceSnapshotBatch:
    """Collect bounded aggregate PR state while preserving partial results.

    Omitted, malformed, or paginated PRs remain in ``missing``. Callers may
    retry just those identities or report them unknown; they must never infer
    clean from an incomplete batch.
    """
    requested = tuple(sorted(set(pr_numbers)))
    entries = batch_fetch_pr_review_and_ci(owner, repo, list(requested), cwd=cwd)
    observed: dict[int, PrMaintenanceSnapshot] = {}
    for number, entry in entries.items():
        latest = entry.get("latest_commit") or ("", "")
        head_sha = str(entry.get("head_sha") or latest[0] or "")
        if not head_sha:
            continue
        observed[number] = PrMaintenanceSnapshot(
            pr_number=number,
            head_sha=head_sha,
            head_committed_at=str(latest[1] or ""),
            ci_checks=tuple(entry.get("ci_checks") or ()),
            required_pending=bool(entry.get("required_pending")),
            unresolved_threads=tuple(
                thread for thread in entry.get("threads", ()) if not thread.is_resolved
            ),
            merge_state=str(entry.get("merge_state") or "unknown"),
            mergeable=str(entry.get("mergeable") or "unknown"),
            review_decision=str(entry.get("review_decision") or "none"),
        )
    missing = tuple(number for number in requested if number not in observed)
    return PrMaintenanceSnapshotBatch(requested, observed, missing)


def get_check_runs_for_commit(sha: str, cwd: str | None = None) -> list[dict]:
    """Snapshot GitHub status for a specific commit ``sha``.

    A push targets a specific commit, so the post-push CI watcher keys off the
    pushed SHA rather than the PR's current head (which can race ahead). Returns
    a deduped list of ``{name, status, conclusion}`` dicts — the stable contract
    the post-push results file and stop-gate consume.

    Combines BOTH of GitHub's status mechanisms so the watcher can't miss a
    pending/failing signal:

    * **Check runs** (``/commits/{sha}/check-runs``) — paginated with
      ``--paginate`` because the endpoint caps at 30 per page, so a commit with
      31+ checks would otherwise drop a failing/pending job on page 2+.
    * **Commit statuses** (``/commits/{sha}/status``) — the older mechanism still
      surfaced on PRs (e.g. external CI). Mapped into the same shape so a repo
      that reports statuses instead of checks isn't seen as ``no_checks``.
    """
    checks: list[dict] = []

    r = _run(
        ["gh", "api", "--paginate",
         f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs",
         "--jq", ".check_runs[] | {name, status, conclusion}"],
        cwd=cwd, timeout_s=30,
    )
    check_runs_observable = r.returncode == 0
    if check_runs_observable:
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                checks.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    else:
        # The check-runs read failed: whatever this call returns is missing an
        # entire status mechanism — an unobservable read, not an observation of
        # "no check runs" (codex PR #75 review, round 5).
        _note_checks_probe_failure()

    statuses = _get_commit_statuses(sha, cwd)
    statuses_observable = (
        not isinstance(statuses, _ObservedList) or statuses.observable
    )
    checks.extend(statuses)

    by_name: dict[str, dict] = {}
    for c in checks:
        if c.get("name"):
            by_name[c["name"]] = c
    values = list(by_name.values()) if by_name else checks
    return _ObservedList(
        values,
        observable=check_runs_observable and statuses_observable,
        error=(
            None
            if check_runs_observable and statuses_observable
            else "one or more commit status mechanisms were unavailable"
        ),
    )


# Combined-status states → our (status, conclusion) model. A commit status is
# ``success`` / ``failure`` / ``error`` / ``pending``.
_STATUS_STATE_MAP = {
    "success": ("completed", "success"),
    "failure": ("completed", "failure"),
    "error": ("completed", "failure"),
    "pending": ("in_progress", None),
}


def _get_commit_statuses(sha: str, cwd: str | None = None) -> list[dict]:
    """Commit statuses for ``sha`` mapped into the check-run dict shape.

    Uses the combined-status endpoint's per-context entries so each external
    CI context becomes one ``{name, status, conclusion}`` dict. Best-effort:
    returns ``[]`` on any failure (advisory watcher)."""
    r = _run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/commits/{sha}/status",
         "--jq", ".statuses[] | {context, state}"],
        cwd=cwd, timeout_s=20,
    )
    if r.returncode != 0:
        # Same unobservability contract as the check-runs read above: a failed
        # commit-status read may hide an external-CI failure (round 5).
        _note_checks_probe_failure()
        return _ObservedList([], observable=False, error="commit statuses unavailable")
    out: list[dict] = []
    for line in r.stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        context = entry.get("context")
        if not context:
            continue
        status, conclusion = _STATUS_STATE_MAP.get(
            entry.get("state", ""), ("in_progress", None)
        )
        out.append({"name": context, "status": status, "conclusion": conclusion})
    return _ObservedList(out, observable=True)


def get_workflow_queue_health(
    pr_number: int,
    cwd: str | None = None,
    now: datetime | None = None,
) -> tuple[list[QueuedWorkflowJob], list[RunnerPoolHealth], RunnerExecutionSummary]:
    """Get queued workflow jobs and self-hosted runner pool health for a PR."""
    owner, repo = get_repo_info(cwd)
    if not owner or not repo:
        return [], [], RunnerExecutionSummary()

    run_ids = _get_pr_workflow_run_ids(pr_number, cwd)
    if not run_ids:
        return [], [], RunnerExecutionSummary()

    runners = _list_self_hosted_runners(owner, repo, cwd)
    if runners is None:
        return [], [], RunnerExecutionSummary()
    queued_jobs: list[QueuedWorkflowJob] = []
    summary = RunnerExecutionSummary()
    now = now or datetime.now(timezone.utc)

    for run_id in run_ids:
        jobs = _list_workflow_run_jobs(owner, repo, run_id, cwd)
        if jobs is None:
            return [], [], RunnerExecutionSummary()
        for raw_job in jobs:
            labels = _job_labels(raw_job)
            if raw_job.get("status") == "completed":
                _count_runner_execution(summary, labels, runners)
            if raw_job.get("status") != "queued":
                continue
            runner_pool = _runner_pool_for_labels(labels)
            uses_self_hosted = _matches_self_hosted_runner(labels, runners)
            matching_count = (
                _matching_online_runner_count(labels, runners)
                if uses_self_hosted
                else None
            )
            queued_at = str(raw_job.get("created_at") or raw_job.get("queued_at") or "")
            queue_seconds = _queue_seconds(queued_at, now)
            pool_health = _runner_pool_health(runner_pool, runners)
            warning = _queue_warning(labels, queue_seconds, matching_count, pool_health)
            queued_jobs.append(
                QueuedWorkflowJob(
                    name=str(raw_job.get("name") or "?"),
                    status=str(raw_job.get("status") or "queued"),
                    labels=labels,
                    queued_at=queued_at or None,
                    queue_seconds=queue_seconds,
                    runner_pool=runner_pool,
                    matching_online_runner_count=matching_count,
                    warning=warning,
                )
            )

    pool_names = {
        job.runner_pool
        for job in queued_jobs
        if _matches_self_hosted_runner(job.labels, runners)
    }
    pools = [_runner_pool_health(pool, runners) for pool in sorted(pool_names)]
    return queued_jobs, pools, summary


def get_weekly_runner_execution_summary(
    cwd: str | None = None,
    now: datetime | None = None,
) -> RunnerExecutionSummary | None:
    """Count desktop vs GitHub-hosted workflow jobs from repo runs in the last 7 days."""
    owner, repo = get_repo_info(cwd)
    if not owner or not repo:
        return None
    now = now or datetime.now(timezone.utc)
    token = _github_auth_token(cwd)
    run_windows = _weekly_runner_run_windows(now)
    runs = (
        _list_recent_workflow_runs_fast_by_windows(owner, repo, run_windows, token)
        if token
        else None
    )
    use_fast_jobs = bool(token and runs is not None)
    if runs is None:
        runs = _list_recent_workflow_runs_by_windows(owner, repo, run_windows, cwd)
    if runs is None:
        return None

    runners = _list_self_hosted_runners(owner, repo, cwd)
    if runners is None:
        return None
    summary = RunnerExecutionSummary()
    run_ids: list[str] = []
    seen_run_ids: set[str] = set()
    for run in runs:
        if not isinstance(run, dict):
            continue
        run_id = str(run.get("id") or "")
        if not run_id or run_id in seen_run_ids:
            continue
        seen_run_ids.add(run_id)
        run_ids.append(run_id)

    job_list_func = (
        lambda run_id: _list_workflow_run_jobs_fast(owner, repo, run_id, token)
        if use_fast_jobs
        else _list_workflow_run_jobs(owner, repo, run_id, cwd, True)
    )

    failed_fetches = 0
    with ThreadPoolExecutor(max_workers=min(WEEKLY_RUNNER_JOB_FETCH_WORKERS, max(1, len(run_ids)))) as executor:
        futures = [
            executor.submit(job_list_func, run_id)
            for run_id in run_ids
        ]
        for future in as_completed(futures):
            jobs = future.result()
            if jobs is None:
                # A single transient job-fetch failure must not discard the
                # whole week: with hundreds of runs per window the odds of one
                # rate-limited/errored fetch are high, and returning None here
                # leaves a stale (possibly pre-seconds, "0m") cache in place.
                # Skip this run and keep accumulating; only give up if every
                # fetch failed (below), which signals a real outage.
                failed_fetches += 1
                continue
            for raw_job in jobs:
                if raw_job.get("status") == "completed":
                    _count_runner_execution(
                        summary,
                        _job_labels(raw_job),
                        runners,
                        duration_seconds=_job_duration_seconds(raw_job),
                    )
    if run_ids and failed_fetches == len(run_ids):
        # Total fetch failure — preserve the existing cache rather than
        # overwriting it with an empty summary.
        return None
    return summary


def load_runner_execution_summary_cache() -> RunnerExecutionSummary | None:
    try:
        raw = json.loads(RUNNER_SUMMARY_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    summary = raw.get("summary", raw)
    if not isinstance(summary, dict):
        return None
    # Reject pre-#1714 cache schemas: those have job counts but no per-pool
    # *_seconds keys, which would silently load as 0.0 and render a misleading
    # "0m". Discarding forces a recompute instead of serving stale "0m" data.
    seconds_keys = ("desktop_seconds", "github_hosted_seconds", "unknown_seconds")
    count_keys = ("desktop_count", "github_hosted_count", "unknown_count")
    has_counts = any(summary.get(key) for key in count_keys)
    has_seconds_schema = any(key in summary for key in seconds_keys)
    if has_counts and not has_seconds_schema:
        return None
    try:
        return RunnerExecutionSummary(
            desktop_count=int(summary.get("desktop_count") or 0),
            github_hosted_count=int(summary.get("github_hosted_count") or 0),
            unknown_count=int(summary.get("unknown_count") or 0),
            desktop_seconds=float(summary.get("desktop_seconds") or 0.0),
            github_hosted_seconds=float(summary.get("github_hosted_seconds") or 0.0),
            unknown_seconds=float(summary.get("unknown_seconds") or 0.0),
        )
    except (TypeError, ValueError):
        return None


def load_runner_execution_summary_cache_generated_at() -> datetime | None:
    try:
        raw = json.loads(RUNNER_SUMMARY_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return _parse_github_time(str(raw.get("generated_at") or ""))


def save_runner_execution_summary_cache(summary: RunnerExecutionSummary, generated_at: str) -> None:
    try:
        RUNNER_SUMMARY_CACHE.parent.mkdir(parents=True, exist_ok=True)
        RUNNER_SUMMARY_CACHE.write_text(json.dumps({
            "generated_at": generated_at,
            "summary": summary.model_dump(),
        }, indent=2))
    except OSError:
        return


def _github_auth_token(cwd: str | None = None) -> str:
    r = _run(["gh", "auth", "token"], cwd=cwd, timeout_s=5)
    if r.returncode != 0:
        return ""
    return r.stdout.strip()


def _github_api_get_json(path: str, token: str, timeout_s: int = 10) -> dict | None:
    request = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "agentic-pr-dash",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8")
    except (OSError, urllib.error.URLError, TimeoutError):
        return None
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _format_github_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _weekly_runner_run_windows(now: datetime) -> list[tuple[str, str]]:
    end = now.astimezone(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=7)
    windows: list[tuple[str, str]] = []
    current = start
    while current < end:
        next_end = min(current + timedelta(days=WEEKLY_RUNNER_RUN_QUERY_DAYS), end)
        windows.append((_format_github_time(current), _format_github_time(next_end)))
        current = next_end
    return windows


def _list_workflow_run_jobs_fast(
    owner: str,
    repo: str,
    run_id: str,
    token: str,
) -> list[dict] | None:
    jobs: list[dict] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"per_page": "100", "filter": "all", "page": str(page)})
        raw = _github_api_get_json(
            f"repos/{owner}/{repo}/actions/runs/{run_id}/jobs?{query}",
            token,
        )
        if raw is None:
            return None
        page_jobs = raw.get("jobs", [])
        if not isinstance(page_jobs, list):
            return None
        jobs.extend(job for job in page_jobs if isinstance(job, dict))
        if len(page_jobs) < 100:
            return jobs
        page += 1


def _list_recent_workflow_runs_fast(
    owner: str,
    repo: str,
    since: str,
    token: str,
    until: str | None = None,
) -> list[dict] | None:
    cutoff = _parse_github_time(since)
    ceiling = _parse_github_time(until or "")
    runs: list[dict] = []
    page = 1
    while True:
        created_filter = f"{since}..{until}" if until else f">={since}"
        query = urllib.parse.urlencode(
            {
                "per_page": "100",
                "created": created_filter,
                "page": str(page),
            }
        )
        raw = _github_api_get_json(
            f"repos/{owner}/{repo}/actions/runs?{query}",
            token,
        )
        if raw is None:
            return None
        page_runs = raw.get("workflow_runs", [])
        if not isinstance(page_runs, list):
            return None
        stop_after_page = False
        for run in page_runs:
            if not isinstance(run, dict):
                continue
            if cutoff is not None:
                created_at = _parse_github_time(str(run.get("created_at") or ""))
                if created_at is not None and created_at < cutoff:
                    stop_after_page = True
                    continue
                if ceiling is not None and created_at is not None and created_at > ceiling:
                    continue
            runs.append(run)
        if len(page_runs) < 100 or stop_after_page:
            return runs
        page += 1


def _list_recent_workflow_runs_fast_by_windows(
    owner: str,
    repo: str,
    windows: list[tuple[str, str]],
    token: str,
) -> list[dict] | None:
    runs: list[dict] = []
    for since, until in windows:
        window_runs = _list_recent_workflow_runs_fast(owner, repo, since, token, until)
        if window_runs is None:
            return None
        runs.extend(window_runs)
    return runs


def _list_recent_workflow_runs_by_windows(
    owner: str,
    repo: str,
    windows: list[tuple[str, str]],
    cwd: str | None = None,
) -> list[dict] | None:
    runs: list[dict] = []
    for since, until in windows:
        window_runs = _list_paginated_key(
            f"repos/{owner}/{repo}/actions/runs?per_page=100&created={since}..{until}",
            "workflow_runs",
            cwd,
            cutoff_created_at=since,
            cutoff_created_before_at=until,
        )
        if window_runs is None:
            return None
        runs.extend(window_runs)
    return runs


def _get_pr_workflow_run_ids(pr_number: int, cwd: str | None = None) -> list[str]:
    r = _run(
        ["gh", "pr", "view", str(pr_number), "--json", "statusCheckRollup"],
        cwd=cwd,
        timeout_s=30,
    )
    if r.returncode != 0:
        return []
    try:
        raw = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return []
    urls = _collect_status_urls(raw.get("statusCheckRollup", raw))
    run_ids: list[str] = []
    seen: set[str] = set()
    for url in urls:
        match = _RUN_ID_RE.search(url)
        if match and match.group(1) not in seen:
            seen.add(match.group(1))
            run_ids.append(match.group(1))
    return run_ids


def _collect_status_urls(value: object) -> list[str]:
    urls: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"detailsUrl", "targetUrl", "url"} and isinstance(nested, str):
                urls.append(nested)
            else:
                urls.extend(_collect_status_urls(nested))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_collect_status_urls(item))
    return urls


def _list_self_hosted_runners(owner: str, repo: str, cwd: str | None = None) -> list[dict] | None:
    return _list_paginated_key(
        f"repos/{owner}/{repo}/actions/runners?per_page=100",
        "runners",
        cwd,
    )


def _list_workflow_run_jobs(
    owner: str,
    repo: str,
    run_id: str,
    cwd: str | None = None,
    include_all_attempts: bool = False,
) -> list[dict] | None:
    filter_param = "&filter=all" if include_all_attempts else ""
    jobs = _list_paginated_key(
        f"repos/{owner}/{repo}/actions/runs/{run_id}/jobs?per_page=100{filter_param}",
        "jobs",
        cwd,
    )
    return jobs


def _list_paginated_key(
    endpoint: str,
    key: str,
    cwd: str | None = None,
    cutoff_created_at: str | None = None,
    cutoff_created_before_at: str | None = None,
) -> list[dict] | None:
    items: list[dict] = []
    cutoff = _parse_github_time(cutoff_created_at or "")
    ceiling = _parse_github_time(cutoff_created_before_at or "")
    page = 1
    while True:
        separator = "&" if "?" in endpoint else "?"
        page_endpoint = f"{endpoint}{separator}page={page}"
        r = _run(
            ["gh", "api", page_endpoint],
            cwd=cwd,
            timeout_s=30,
        )
        if r.returncode != 0:
            return None
        try:
            raw = json.loads(r.stdout or "{}")
        except json.JSONDecodeError:
            return None
        page_items = raw.get(key, [])
        if not isinstance(page_items, list):
            return None
        stop_after_page = False
        for item in page_items:
            if not isinstance(item, dict):
                continue
            if cutoff is not None:
                created_at = _parse_github_time(str(item.get("created_at") or ""))
                if created_at is not None and created_at < cutoff:
                    stop_after_page = True
                    continue
                if ceiling is not None and created_at is not None and created_at > ceiling:
                    continue
            items.append(item)
        if len(page_items) < 100 or stop_after_page:
            return items
        page += 1


def _job_labels(raw_job: dict) -> list[str]:
    labels = raw_job.get("labels", [])
    if not isinstance(labels, list):
        return []
    normalized: list[str] = []
    for label in labels:
        if isinstance(label, str):
            normalized.append(label)
        elif isinstance(label, dict) and label.get("name"):
            normalized.append(str(label["name"]))
    return normalized


def _runner_labels(runner: dict) -> set[str]:
    labels = runner.get("labels", [])
    names: set[str] = set()
    if not isinstance(labels, list):
        return names
    for label in labels:
        if isinstance(label, str):
            names.add(label.lower())
        elif isinstance(label, dict) and label.get("name"):
            names.add(str(label["name"]).lower())
    return names


def _runner_pool_for_labels(labels: list[str]) -> str:
    lowered = {label.lower() for label in labels}
    configured_label = _runner_label()
    if configured_label and configured_label in lowered:
        return configured_label
    if "self-hosted" in lowered:
        custom = [
            label
            for label in labels
            if label.lower() not in {"self-hosted", "linux", "x64", "arm", "arm64", "windows", "macos"}
        ]
        return custom[0] if custom else "self-hosted"
    return labels[0] if labels else "unknown"


def _uses_self_hosted_runner(labels: list[str]) -> bool:
    lowered = {label.lower() for label in labels}
    configured_label = _runner_label()
    if "self-hosted" in lowered or (configured_label and configured_label in lowered):
        return True
    return False


def _matches_self_hosted_runner(labels: list[str], runners: list[dict]) -> bool:
    if _uses_self_hosted_runner(labels):
        return True
    required = {label.lower() for label in labels}
    if not required:
        return False
    return any(required.issubset(_runner_labels(runner)) for runner in runners)


def _job_duration_seconds(raw_job: dict) -> float:
    """Wall-clock runtime of a job from its started_at/completed_at stamps.

    Returns 0.0 when either stamp is missing or the delta is non-positive
    (e.g. skipped jobs, which never start a runner)."""
    started = _parse_github_time(str(raw_job.get("started_at") or ""))
    completed = _parse_github_time(str(raw_job.get("completed_at") or ""))
    if started is None or completed is None:
        return 0.0
    delta = (completed - started).total_seconds()
    return delta if delta > 0 else 0.0


def _count_runner_execution(
    summary: RunnerExecutionSummary,
    labels: list[str],
    runners: list[dict] | None = None,
    duration_seconds: float = 0.0,
) -> None:
    lowered = {label.lower() for label in labels}
    configured_label = _runner_label()
    uses_self_hosted_runner = (
        (configured_label is not None and configured_label in lowered)
        or "self-hosted" in lowered
        or (runners is not None and _matches_self_hosted_runner(labels, runners))
    )
    if uses_self_hosted_runner:
        summary.desktop_count += 1
        summary.desktop_seconds += duration_seconds
    elif labels:
        summary.github_hosted_count += 1
        summary.github_hosted_seconds += duration_seconds
    else:
        summary.unknown_count += 1
        summary.unknown_seconds += duration_seconds


def _matching_online_runner_count(labels: list[str], runners: list[dict]) -> int:
    required = {label.lower() for label in labels}
    return sum(
        1
        for runner in runners
        if runner.get("status") == "online"
        and not runner.get("busy")
        and required.issubset(_runner_labels(runner))
    )


def _runner_pool_health(pool: str, runners: list[dict]) -> RunnerPoolHealth:
    pool_lower = pool.lower()
    matching = [
        runner
        for runner in runners
        if pool_lower in _runner_labels(runner)
        or (pool_lower == "self-hosted" and "self-hosted" in _runner_labels(runner))
    ]
    return RunnerPoolHealth(
        pool=pool,
        total_count=len(matching),
        online_count=sum(1 for runner in matching if runner.get("status") == "online"),
        busy_count=sum(1 for runner in matching if runner.get("status") == "online" and runner.get("busy")),
    )


def _queue_seconds(queued_at: str, now: datetime) -> int | None:
    parsed = _parse_github_time(queued_at)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()))


def _queue_warning(
    labels: list[str],
    queue_seconds: int | None,
    matching_online_runner_count: int | None,
    pool_health: RunnerPoolHealth,
) -> str | None:
    lowered = {label.lower() for label in labels}
    configured_label = _runner_label()
    if configured_label and configured_label in lowered and pool_health.online_count == 0:
        return f"{configured_label} fleet offline"
    if (
        matching_online_runner_count is not None
        and matching_online_runner_count == 0
        and (queue_seconds is None or queue_seconds >= QUEUE_WARNING_SECONDS)
    ):
        return "No matching online runner for requested labels"
    return None


def _parse_github_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _thread_state(
    replies: list[dict],
    top_author: str | None = None,
) -> tuple[str, float | None]:
    """Walk reply history and return (terminal_state, claim_age_seconds_or_None).

    Terminal states:
      open           — nothing has happened yet
      claimed        — dashboard is working on it (active, non-stale claim)
      completed      — dashboard marked the thread done
      failed         — dashboard's push failed; agent needs to retry
      human_resolved — human replied before any dashboard engagement
      reopened       — human replied AFTER a dashboard marker

    ``top_author`` is the login of the thread's original (top) comment author.
    A non-marker reply from that SAME author on an otherwise-untouched thread is
    the reviewer's own fresh follow-up feedback — NOT a third-party resolution —
    so it must leave the thread ``open`` (still actionable). Only a reply from a
    DIFFERENT human resolves an open thread (BOU-1801: the loop was silently
    dropping reviewers' own follow-up replies as ``human_resolved``).
    """
    now = datetime.now(timezone.utc)
    state = "open"
    claim_created: datetime | None = None

    for reply in sorted(replies, key=lambda r: str(r.get("created_at", ""))):
        body = str(reply.get("body", ""))
        if COMPLETE_MARKER in body:
            state = "completed"
            claim_created = None
        elif FAILED_MARKER in body:
            state = "failed"
            claim_created = None
        elif CLAIM_MARKER in body:
            state = "claimed"
            claim_created = _parse_github_time(str(reply.get("created_at", "")))
        else:
            # Human or third-party reply.
            reply_author = str(reply.get("author", "")) or None
            reply_is_thread_author = (
                top_author is not None and reply_author == top_author
            )
            if state in ("claimed", "completed", "failed"):
                state = "reopened"
                claim_created = None
            elif state == "open" and not reply_is_thread_author:
                state = "human_resolved"
            # A same-author follow-up on an open thread stays "open" (fresh
            # reviewer feedback). human_resolved and reopened are sticky under
            # further human replies.

    claim_age: float | None = None
    if state == "claimed" and claim_created is not None:
        claim_age = (now - claim_created).total_seconds()

    return state, claim_age


def _thread_is_addressed_or_claimed(replies: list[dict]) -> bool:
    """Return True when a review thread should be skipped by auto-dispatch.

    Walks replies chronologically so a human follow-up after a dashboard
    marker (e.g. "this was NOT addressed" after a `completed` reply) re-opens
    the thread. Human replies stay idempotently "handled by human" until a
    dashboard marker appears; they do not toggle the thread back open based on
    reply count alone.
    """
    if not replies:
        return False

    state, claim_age = _thread_state(replies)

    if state in ("completed", "human_resolved"):
        return True
    if state == "claimed":
        return bool(claim_age is not None and claim_age < STALE_CLAIM_SECONDS)
    return False


class _ReviewLevelReadError(RuntimeError):
    """Strict review read failed after its GraphQL thread query completed."""


def scan_review_threads(
    pr_number: int,
    latest_commit_date: str,
    cwd: str | None = None,
    *,
    strict: bool = False,
) -> tuple[list[ReviewComment], list[ThreadDecision]]:
    """Return (picked_comments, decisions) for all review threads on a PR.

    ``picked_comments`` is identical to what :func:`get_unaddressed_comments`
    returns. ``decisions`` contains one :class:`ThreadDecision` for every
    inline thread so callers can audit why each thread was picked or skipped.
    Review-level CHANGES_REQUESTED comments are included in ``picked_comments``
    but are not reflected in ``decisions`` (they have no thread state machine).
    """
    now = datetime.now(timezone.utc)
    comments: list[ReviewComment] = []
    decisions: list[ThreadDecision] = []
    from ._maintenance import deferred_review as _deferred_review  # noqa: PLC0415

    deferred_findings = _deferred_review.deferred_threads_for_pr(
        cwd or ".",
        pr_number,
    )

    # Inline review threads via GraphQL
    threads = (
        get_review_threads(pr_number, cwd, strict=True)
        if strict
        else get_review_threads(pr_number, cwd)
    )
    for thread in threads:
        top = thread.top
        created_dt = _parse_github_time(top.created_at)
        age_seconds: float | None = None
        if created_dt is not None:
            age_seconds = (now - created_dt).total_seconds()

        if thread.is_resolved:
            decisions.append(ThreadDecision(
                thread_id=thread.node_id,
                author=top.author,
                created_at=top.created_at,
                age_seconds=age_seconds,
                decision="SKIP_RESOLVED",
                marker_state=None,
                claim_age_seconds=None,
            ))
            continue

        # BOU-2567: a deliberately-deferred thread (verified genuine, out of
        # scope, tracked by its own follow-up ticket) is a first-class state,
        # never encoded as resolved and never as plain unresolved. Checked
        # BEFORE is_outdated/marker-state so a deferred thread is never picked
        # regardless of its drift or claim-reply state — the deferral decision
        # is authoritative, not one signal among several.
        if thread.node_id in deferred_findings:
            decisions.append(ThreadDecision(
                thread_id=thread.node_id,
                author=top.author,
                created_at=top.created_at,
                age_seconds=age_seconds,
                decision="SKIP_DEFERRED",
                marker_state=None,
                claim_age_seconds=None,
            ))
            continue

        if thread.is_outdated:
            decisions.append(ThreadDecision(
                thread_id=thread.node_id,
                author=top.author,
                created_at=top.created_at,
                age_seconds=age_seconds,
                decision="SKIP_OUTDATED",
                marker_state=None,
                claim_age_seconds=None,
            ))
            continue

        replies_as_dicts = [
            {"body": r.body, "created_at": r.created_at, "author": r.author}
            for r in thread.replies
        ]

        state, claim_age = _thread_state(replies_as_dicts, top_author=top.author)

        if state == "completed":
            decisions.append(ThreadDecision(
                thread_id=thread.node_id,
                author=top.author,
                created_at=top.created_at,
                age_seconds=age_seconds,
                decision="SKIP_ADDRESSED",
                marker_state=state,
                claim_age_seconds=None,
            ))
            continue

        if state == "human_resolved":
            decisions.append(ThreadDecision(
                thread_id=thread.node_id,
                author=top.author,
                created_at=top.created_at,
                age_seconds=age_seconds,
                decision="SKIP_HUMAN_RESOLVED",
                marker_state=state,
                claim_age_seconds=None,
            ))
            continue

        if state == "claimed" and claim_age is not None and claim_age < STALE_CLAIM_SECONDS:
            decisions.append(ThreadDecision(
                thread_id=thread.node_id,
                author=top.author,
                created_at=top.created_at,
                age_seconds=age_seconds,
                decision="SKIP_CLAIMED_ACTIVE",
                marker_state=state,
                claim_age_seconds=claim_age,
            ))
            continue

        # Thread is actionable — add to results
        comments.append(ReviewComment(
            id=top.database_id,
            author=top.author,
            body=top.body,
            path=top.path,
            line=top.line,
            created_at=top.created_at,
            is_inline=True,
            thread_id=thread.node_id,
        ))
        decisions.append(ThreadDecision(
            thread_id=thread.node_id,
            author=top.author,
            created_at=top.created_at,
            age_seconds=age_seconds,
            decision="PICKED",
            marker_state=state,
            claim_age_seconds=None,
        ))

    # Review-level comments (CHANGES_REQUESTED with body)
    try:
        r2 = _run(
            ["gh", "api", f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/reviews",
             "--jq", '.[] | select((.state == "APPROVED" or .state == "COMMENTED" or .state == "CHANGES_REQUESTED") and .body != "") | {id, author: .user.login, body, state, submitted_at}'],
            cwd=cwd,
        )
    except Exception as exc:
        if strict:
            raise _ReviewLevelReadError(
                "scan_review_threads: review-level read raised after GraphQL "
                "threads completed"
            ) from exc
        raise
    if r2.returncode == 0:
        for line in r2.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                submitted = data.get("submitted_at", "")
                if latest_commit_date and submitted <= latest_commit_date:
                    continue
                review_id = data.get("id", 0)
                body = data.get("body", "")
                from ._maintenance.review_settlement import (  # noqa: PLC0415
                    declared_review_body_lines,
                )

                declared_lines = declared_review_body_lines(body)
                if (
                    data.get("state") != "CHANGES_REQUESTED"
                    and not declared_lines
                ):
                    continue
                bodies = declared_lines or [body]
                for ordinal, finding_body in enumerate(bodies, start=1):
                    disposition_key = (
                        f"review:{review_id}"
                        if len(bodies) == 1
                        else f"review:{review_id}:{ordinal}"
                    )
                    if disposition_key in deferred_findings:
                        continue
                    comments.append(ReviewComment(
                        id=review_id,
                        author=data.get("author", "unknown"),
                        body=finding_body,
                        created_at=submitted,
                        is_inline=False,
                    ))
            except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
                if strict:
                    raise _ReviewLevelReadError(
                        "scan_review_threads: malformed review-level response; "
                        "refusing to synthesize a clean review state"
                    )
                continue
    elif strict:
        raise _ReviewLevelReadError(
            "scan_review_threads: review-level read failed; refusing to "
            "synthesize a clean review state"
        )

    return comments, decisions


def scan_review_threads_observation(
    pr_number: int,
    latest_commit_date: str,
    cwd: str | None = None,
) -> ObservationReadResult[tuple[list[ReviewComment], list[ThreadDecision]]]:
    """Strict dashboard review read with an explicit unavailable outcome."""
    try:
        if scan_review_threads.__module__ == __name__:
            value = scan_review_threads(
                pr_number,
                latest_commit_date,
                cwd,
                strict=True,
            )
        else:
            # Tests and external adapters historically replace the public
            # boundary with a three-argument callable. Their return is the
            # explicit observation contract for that adapter.
            value = scan_review_threads(pr_number, latest_commit_date, cwd)
    except _ReviewLevelReadError as exc:
        return ObservationReadResult.unavailable(
            f"review observation unavailable: {exc}",
            graphql_observed=True,
        )
    except Exception as exc:  # noqa: BLE001
        return ObservationReadResult.unavailable(
            f"review observation unavailable: {exc}"
        )
    if isinstance(value, ObservationReadResult):
        return value
    return ObservationReadResult.observed(value)


def get_unaddressed_comments(
    pr_number: int,
    latest_commit_date: str,
    cwd: str | None = None,
) -> list[ReviewComment]:
    """Get review comments that have no completed or active claim reply.

    Uses the GraphQL reviewThreads API for inline threads, then appends
    review-level CHANGES_REQUESTED comments from the REST /reviews endpoint.
    """
    return scan_review_threads(pr_number, latest_commit_date, cwd)[0]


def reply_to_review_comment(
    pr_number: int,
    comment: ReviewComment,
    body: str,
    cwd: str | None = None,
) -> int | None:
    """Reply to an inline review comment, or fall back to a PR comment.

    Returns the new reply comment ID for inline replies, ``True`` for a
    successful non-inline PR comment, or ``None`` on failure.
    """
    if comment.is_inline:
        r = _run_mutation(
            [
                "gh", "api",
                f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/comments/{comment.id}/replies",
                "-f", f"body={body}",
            ],
            cwd=cwd,
            timeout_s=20,
        )
        if r.returncode != 0:
            return None
        try:
            return int(json.loads(r.stdout).get("id"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    r = _run_mutation(
        [
            "gh", "pr", "comment", str(pr_number),
            "--body", f"Review @{comment.author} ({comment.id}):\n\n{body}",
        ],
        cwd=cwd,
        timeout_s=20,
    )
    return True if r.returncode == 0 else None


def get_failed_logs(sha: str, check_names: list[str], cwd: str | None = None) -> dict[str, str]:
    """Fetch log tails for failed CI runs."""
    r = _run(
        ["gh", "run", "list", "--commit", sha, "--status", "failure",
         "--json", "databaseId,name", "--limit", "10"],
        cwd=cwd,
    )
    if r.returncode != 0:
        return {}
    try:
        runs = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    if not isinstance(runs, list):
        return {}

    logs: dict[str, str] = {}
    for wf_run in runs:
        if not isinstance(wf_run, dict):
            continue
        run_name = wf_run.get("name", "")
        run_id = wf_run.get("databaseId")
        if not run_id:
            continue
        matched_name = None
        for cn in check_names:
            if cn.lower() in run_name.lower() or run_name.lower() in cn.lower():
                matched_name = cn
                break
        if not matched_name:
            continue
        r2 = _run(["gh", "run", "view", str(run_id), "--log-failed"], cwd=cwd, timeout_s=30)
        if r2.returncode == 0 and r2.stdout.strip():
            lines = r2.stdout.strip().split("\n")
            tail = lines[-LOG_TAIL_LINES:] if len(lines) > LOG_TAIL_LINES else lines
            logs[matched_name] = "\n".join(tail)
    return logs
