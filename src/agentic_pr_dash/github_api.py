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

import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import load as load_config
from .models import CICheck, QueuedWorkflowJob, ReviewComment, RunnerExecutionSummary, RunnerPoolHealth


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


@dataclass(frozen=True)
class ReviewThread:
    node_id: str
    is_resolved: bool
    is_outdated: bool
    top: ReviewThreadComment
    replies: list[ReviewThreadComment] = field(default_factory=list)


def _run(cmd: list[str], timeout_s: int = 20, cwd: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s, cwd=cwd,
        )
    except (subprocess.TimeoutExpired, OSError):
        return subprocess.CompletedProcess(cmd, 1, "", "")


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


def list_open_prs(cwd: str | None = None) -> list[dict] | None:
    """List all open PRs authored by the current user.

    Returns ``None`` when the underlying ``gh`` call fails (e.g. the GitHub
    API is rate-limited or unreachable) so callers can distinguish a genuine
    "no open PRs" result (``[]``) from an API failure. Treating a failure as
    an empty list would let a transient outage prune every tracked PR.
    """
    r = _run(
        ["gh", "pr", "list", "--author", "@me", "--state", "open",
         "--json", "number,title,headRefName,baseRefName,url,isDraft,reviewDecision,mergeStateStatus,mergeable,labels,createdAt"],
        cwd=cwd, timeout_s=30,
    )
    if r.returncode != 0:
        return None
    try:
        prs = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return prs if isinstance(prs, list) else None


_PR_HEAD_FIELDS = (
    "number,title,body,url,isDraft,mergeStateStatus,reviewDecision,"
    "headRefOid,headRefName,headRepositoryOwner,baseRefName"
)

# `gh pr list --head` is a *prefix* filter (GitHub returns every PR whose head
# branch *begins* with the query, so `--head fix` also returns `fix-123`). A
# bare `--limit 1` can therefore hand the single result slot to a prefix match
# and drop the exact-branch PR before we can filter for it — so we fetch a wide
# page and exact-filter `headRefName` (and, for fork heads, `headRepositoryOwner`)
# in Python instead.
_PR_HEAD_LOOKUP_LIMIT = "30"


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

    ``state`` is one of ``"open"``, ``"merged"``, ``"closed"``, ``"all"``. When
    ``head_oid`` is given, only a PR whose ``headRefOid`` matches it is returned
    — the caller uses this to confirm that a merged PR corresponds to the
    *current* local HEAD (squash-merged branches can stay ahead of the default
    branch, and a reused branch name must still go through the normal gates).

    Returns ``None`` on any ``gh`` failure (so the caller fails open) or when no
    matching PR exists.
    """
    if not branch:
        return None
    # `gh` accepts `<owner>:<branch>` for --head (fork/head-qualified specs), but
    # `gh pr list --head` rejects the owner qualifier — strip it before resolving
    # (mirrors the arm flow in maintenance_check). We KEEP the owner separately so
    # two fork PRs with the same branch name (`alice:feature`, `bob:feature`) don't
    # collide: results are post-filtered by `headRepositoryOwner` below.
    head_owner = ""
    if ":" in branch:
        head_owner, branch = branch.split(":", 1)
    if not branch:
        return None
    cmd = [
        "gh", "pr", "list",
        "--head", branch,
        "--state", state,
        "--limit", _PR_HEAD_LOOKUP_LIMIT,
        "--json", _PR_HEAD_FIELDS,
    ]
    r = _run(cmd, cwd=cwd, timeout_s=30)
    if r.returncode != 0:
        return None
    try:
        payload = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    for pr in payload:
        if not isinstance(pr, dict):
            continue
        # `--head` is a prefix filter (GitHub matches branch names *beginning*
        # with the query, so `fix` also returns `fix-123`). Require an exact
        # head-branch match so a Stop/QA gate never evaluates the wrong PR.
        if str(pr.get("headRefName") or "") != branch:
            continue
        # When the caller supplied an owner-qualified head (`alice:feature`),
        # require the PR's head-repo owner to match so a same-named branch on a
        # different fork (`bob:feature`) isn't returned in its place.
        if head_owner and str(_pr_head_owner(pr)) != head_owner:
            continue
        if head_oid is not None and pr.get("headRefOid") != head_oid:
            continue
        return pr
    return None


def _pr_head_owner(pr: dict) -> str:
    """Extract the head-repository owner login from a `gh pr list` PR payload.

    `gh` serializes ``headRepositoryOwner`` as ``{"login": ..., "id": ...}``;
    return the bare login (or "" when absent)."""
    owner = pr.get("headRepositoryOwner")
    if isinstance(owner, dict):
        return str(owner.get("login") or "")
    return str(owner or "")


def get_latest_commit(pr_number: int, cwd: str | None = None) -> tuple[str, str]:
    """Get the SHA and date of the latest commit on a PR."""
    r = _run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/commits",
         "--jq", ".[-1] | [.sha, .commit.author.date] | @tsv"],
        cwd=cwd,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return "", ""
    parts = r.stdout.strip().split("\t")
    if len(parts) >= 2:
        return parts[0], parts[1]
    return parts[0] if parts else "", ""


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
) -> list[tuple[str, str]]:
    """Return commits added to a PR after a known baseline SHA.

    Prefers the local git range scoped to the PR branch's remote-tracking ref
    (immediate after a push, and not polluted by unrelated HEAD commits); falls
    back to the GitHub API when the range can't be resolved or trusted locally.

    ``api_head_sha`` is the GitHub API's view of the PR head: when it is set but
    absent from the local ``origin/<branch>`` ref, that ref is stale and the
    local range is rejected in favor of the API (see ``_local_new_commits``).
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
        return []

    try:
        raw = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []

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


def get_review_threads(pr_number: int, cwd: str | None = None) -> list[ReviewThread]:
    """Return all review threads for a PR via GraphQL.

    Paginates over ``reviewThreads`` (100 per page) so PRs with more than 100
    threads are not silently truncated — a hot review can easily exceed the
    first page, and a truncated thread list would let resolved-elsewhere or
    still-open threads slip past the caller's resolved/outdated filtering.

    A *first*-page failure returns ``[]`` (total unavailability — callers fail
    open, matching :func:`find_pr_by_head`). But once a page has succeeded and
    advertised ``hasNextPage``, a failure fetching a *subsequent* page raises
    :class:`RuntimeError` rather than returning a partial list: silently
    dropping later pages would let still-open threads slip past the
    unresolved-thread gate — the exact truncation hazard this pagination is
    meant to eliminate. A malformed page that reports ``hasNextPage=true`` but
    omits/empties ``endCursor`` is treated the same way (we cannot advance, so
    raising beats truncating).
    """
    owner, repo = get_repo_info(cwd)
    if not owner or not repo:
        return []

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
            break

        for node in nodes:
            try:
                comment_nodes = node["comments"]["nodes"]
                if not comment_nodes:
                    continue

                def _parse_comment(c: dict) -> ReviewThreadComment:
                    return ReviewThreadComment(
                        database_id=int(c.get("databaseId") or 0),
                        path=c.get("path"),
                        line=c.get("line"),
                        body=str(c.get("body") or ""),
                        author=str((c.get("author") or {}).get("login") or "unknown"),
                        created_at=str(c.get("createdAt") or ""),
                    )

                top = _parse_comment(comment_nodes[0])
                replies = [_parse_comment(c) for c in comment_nodes[1:]]
                threads.append(ReviewThread(
                    node_id=str(node["id"]),
                    is_resolved=bool(node.get("isResolved")),
                    is_outdated=bool(node.get("isOutdated")),
                    top=top,
                    replies=replies,
                ))
            except (KeyError, TypeError, ValueError):
                continue

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


def resolve_review_thread(thread_id: str, cwd: str | None = None) -> bool:
    """Resolve a review thread via GraphQL mutation."""
    r = _run(
        [
            "gh", "api", "graphql",
            "-f", f"query={_RESOLVE_THREAD_MUTATION}",
            "-F", f"id={thread_id}",
        ],
        cwd=cwd,
        timeout_s=20,
    )
    return r.returncode == 0


def edit_review_comment(comment_id: int, body: str, cwd: str | None = None) -> bool:
    """Edit an existing review comment in place via REST PATCH."""
    r = _run(
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


def get_ci_checks(pr_number: int, cwd: str | None = None) -> list[CICheck]:
    """Get CI check status for a PR."""
    r = _run(
        ["gh", "pr", "checks", str(pr_number),
         "--json", "name,bucket,state"],
        cwd=cwd, timeout_s=30,
    )
    if r.returncode != 0:
        return []
    try:
        raw = json.loads(r.stdout or "[]")
        if not isinstance(raw, list):
            return []
    except json.JSONDecodeError:
        return []

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
    return checks


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
    if r.returncode == 0:
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                checks.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    checks.extend(_get_commit_statuses(sha, cwd))

    by_name: dict[str, dict] = {}
    for c in checks:
        if c.get("name"):
            by_name[c["name"]] = c
    return list(by_name.values()) if by_name else checks


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
        return []
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
    return out


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


def get_unaddressed_comments(
    pr_number: int,
    latest_commit_date: str,
    cwd: str | None = None,
) -> list[ReviewComment]:
    """Get review comments that have no completed or active claim reply.

    Uses the GraphQL reviewThreads API for inline threads, then appends
    review-level CHANGES_REQUESTED comments from the REST /reviews endpoint.
    """
    comments: list[ReviewComment] = []

    # Inline review threads via GraphQL
    threads = get_review_threads(pr_number, cwd)
    for thread in threads:
        # Skip resolved threads AND outdated ones: an outdated thread points at
        # code that has since changed, so it is not actionable feedback. Treating
        # it as unaddressed would make a green-CI PR with only an outdated thread
        # read as blocked, and would prevent `pr_has_unresolved_review_threads`
        # from being authoritative (PR #16 review round 2, P2).
        if thread.is_resolved or thread.is_outdated:
            continue
        replies_as_dicts = [
            {"body": r.body, "created_at": r.created_at}
            for r in thread.replies
        ]
        if _thread_is_addressed_or_claimed(replies_as_dicts):
            continue
        top = thread.top
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

    # Review-level comments (CHANGES_REQUESTED with body)
    r2 = _run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/reviews",
         "--jq", '.[] | select(.state == "CHANGES_REQUESTED" and .body != "") | {id, author: .user.login, body, state, submitted_at}'],
        cwd=cwd,
    )
    if r2.returncode == 0:
        for line in r2.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                submitted = data.get("submitted_at", "")
                if latest_commit_date and submitted <= latest_commit_date:
                    continue
                comments.append(ReviewComment(
                    id=data.get("id", 0),
                    author=data.get("author", "unknown"),
                    body=data.get("body", ""),
                    created_at=submitted,
                    is_inline=False,
                ))
            except json.JSONDecodeError:
                continue

    return comments


def _parse_github_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


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

    now = datetime.now(timezone.utc)
    # States:
    #   open           — nothing has happened yet
    #   claimed        — dashboard is working on it
    #   completed      — dashboard marked the thread done
    #   failed         — dashboard's push failed; agent needs to retry
    #   human_resolved — human replied before any dashboard engagement
    #   reopened       — human replied AFTER a dashboard marker; stays sticky
    #                    so additional human follow-ups don't flip back to
    #                    "human_resolved"
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
            if state in ("claimed", "completed", "failed"):
                state = "reopened"
                claim_created = None
            elif state == "open":
                state = "human_resolved"
            # human_resolved and reopened are sticky under further human replies

    if state in ("completed", "human_resolved"):
        return True
    if state == "claimed":
        return bool(
            claim_created
            and (now - claim_created).total_seconds() < STALE_CLAIM_SECONDS
        )
    return False


def reply_to_review_comment(
    pr_number: int,
    comment: ReviewComment,
    body: str,
    cwd: str | None = None,
) -> int | None:
    """Reply to an inline review comment, or fall back to a PR comment.

    Returns the new reply comment ID (int) for inline replies, or None for
    non-inline / fallback replies or on failure.
    """
    if comment.is_inline:
        r = _run(
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

    r = _run(
        [
            "gh", "pr", "comment", str(pr_number),
            "--body", f"Review @{comment.author} ({comment.id}):\n\n{body}",
        ],
        cwd=cwd,
        timeout_s=20,
    )
    return None


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
