"""PR-resolution and review-thread helpers."""
from __future__ import annotations

import subprocess

from ._common import _current_branch

_GH_UNAVAILABLE = object()  # sentinel: gh CLI failed


def _gh_unavailable_message(cwd: str | None = None) -> str:
    """Operator-facing message for a failed ``list_open_prs`` resolution."""
    from agentic_pr_dash import github_api  # noqa: PLC0415

    failure = github_api.last_list_open_prs_failure()
    if failure is None:
        where = f" in {cwd}" if cwd else ""
        return (
            "could not list PRs (gh unavailable): no diagnostics were captured. "
            f"Re-run `gh pr list --author @me --state open`{where} from the same "
            "cwd and check `gh auth status`."
        )
    text = "could not list PRs (gh unavailable)\n" + failure.describe()
    if failure.is_rate_limited:
        text += (
            "\n  note: the PR list was rate-limited and the per-PR REST "
            "fallback could not fully verify the target PR either (BOU-1966); "
            "this usually clears once the quota window resets."
        )
    return text


def _list_failure_is_rate_limited() -> bool:
    """True when the just-failed ``list_open_prs`` was quota/rate-limit classified.

    Gate for the BOU-1966 quota-safe REST fallback: only a rate-limited author
    list may fall back to per-PR REST resolution. Auth lapses, a missing ``gh``,
    and malformed output keep failing closed — those failure modes affect the
    REST path identically, so "verified via REST" would be meaningless there.
    """
    from agentic_pr_dash import github_api  # noqa: PLC0415

    failure = github_api.last_list_open_prs_failure()
    return failure is not None and failure.is_rate_limited


def _rest_fallback_entry_by_number(pr_number: int, cwd: str):
    """Quota fallback (BOU-1966): raw PR entry for ``--pr N`` via pure REST.

    Returns the normalized payload dict, or ``None`` when the list failure was
    not quota-classified OR REST cannot verify the PR either — the caller stays
    fail-closed (``_GH_UNAVAILABLE``) in both cases.
    """
    from agentic_pr_dash import github_api  # noqa: PLC0415

    if not _list_failure_is_rate_limited():
        return None
    return github_api._rest_pr_payload(pr_number, cwd=cwd)


def _rest_fallback_entry_for_branch(branch: str, cwd: str):
    """Quota fallback (BOU-1966): raw PR entry for the current branch via REST.

    Resolves owner (REST) → exact owner-qualified head numbers (REST) → full
    payload (REST). Deliberately avoids ``gh pr list --head`` — that path is
    GraphQL, exactly what is quota-blocked here. An empty exact-head result
    ALSO returns ``None`` (→ fail closed): the owner-qualified query cannot see
    a fork-backed head, so "no match" under quota exhaustion is weaker evidence
    than the author-scoped list and must not read as a verified "no PR".
    """
    from agentic_pr_dash import github_api  # noqa: PLC0415

    if not _list_failure_is_rate_limited():
        return None
    owner = github_api._rest_repo_owner(cwd)
    if not owner:
        return None
    numbers = github_api._exact_head_pr_numbers(owner, branch, "open", cwd=cwd)
    if not numbers:
        return None
    raw = github_api._rest_pr_payload(numbers[0], cwd=cwd)
    if raw is None or str(raw.get("headRefName") or "") != branch:
        return None
    return raw


def _resolve_pr_for_branch(cwd: str, *, force: bool = False):
    """Find the open PR whose headRefName matches the current branch.

    ``force`` bypasses the shared PR-list snapshot cache (see
    :func:`github_api.list_open_prs_cached`) for callers about to act on the
    result (e.g. the completion path, right before firing mutations) where a
    stale merge/draft state would be a correctness risk rather than merely a
    quota optimization.
    """
    from agentic_pr_dash import github_api  # noqa: PLC0415
    from agentic_pr_dash.models import PRData, PRStatus  # noqa: PLC0415

    branch = _current_branch(cwd)
    if not branch:
        return None

    # Shared short-TTL snapshot (BOU-1923 Bucket 2): the stop-gate, the
    # detached loop, and the await waiter each resolve this per worktree/tick,
    # so a cached "list my open PRs" here collapses that fan-out onto one
    # underlying `gh` call within the TTL window instead of one per caller.
    prs = github_api.list_open_prs_cached(cwd, force=force)
    raw: dict | None = None
    if prs is None:
        # Quota-safe REST fallback (BOU-1966): a rate-limited author list can
        # still verify THIS branch's PR via per-PR REST calls. Any other
        # failure mode (auth, missing gh) — and REST failing too — stays
        # fail-closed.
        raw = _rest_fallback_entry_for_branch(branch, cwd)
        if raw is None:
            return _GH_UNAVAILABLE
    else:
        if not prs:
            return None
        for entry in prs:
            if entry.get("headRefName") == branch:
                raw = entry
                break
        if raw is None:
            return None

    pr_number = int(raw["number"])
    # Preserve a live gh-availability signal across the detail fetch (BOU-1923
    # review, BOU-1966). A warm snapshot (or the REST fallback above) skips the
    # `list_open_prs` failure that used to turn a current gh/rate-limit outage
    # into _GH_UNAVAILABLE; the detail getters below then fail OPEN to empty
    # values, so an outage during the fetch would make a genuinely-blocked PR
    # read as CLEAN. Compare the monotonic rate-limit event COUNT across the
    # span — the tick-scoped rate_limit_seen() flag is already set on the
    # fallback path (by the failed list itself), so a boolean read cannot see
    # NEW failures. If any detail call ultimately rate-limits here, the empty
    # results are unreliable — surface _GH_UNAVAILABLE (→ stop-gate/check code
    # 2) instead of a false "clean". We never reset the flag or the counter,
    # so the tick-based waiter's per-tick accumulation is untouched.
    _rl_events_before = github_api.rate_limit_events()
    latest_sha, latest_date = github_api.get_latest_commit(pr_number, cwd)
    checks = github_api.get_ci_checks(pr_number, cwd)
    failing = [
        c.name
        for c in checks
        if c.conclusion == "failure" and not github_api._is_infra_check(c.name)
    ]
    review_comments = github_api.get_unaddressed_comments(pr_number, latest_date, cwd)
    if github_api.rate_limit_events() != _rl_events_before:
        return _GH_UNAVAILABLE
    merge_state = raw.get("mergeStateStatus", "unknown")
    mergeable = raw.get("mergeable", "unknown")

    return PRData(
        number=pr_number,
        title=raw.get("title", ""),
        branch=branch,
        base_branch=raw.get("baseRefName", "main"),
        url=raw.get("url", ""),
        is_draft=bool(raw.get("isDraft", False)),
        merge_state=merge_state,
        mergeable=mergeable,
        ci_checks=checks,
        failing_checks=failing,
        review_comments=review_comments,
        latest_commit_sha=latest_sha,
        latest_commit_date=latest_date,
        worktree_path=cwd,
        status=PRStatus.CLEAN,
    )


def _resolve_pr_by_number(pr_number: int, cwd: str, *, force: bool = False):
    """Resolve a PR by explicit number (for --pr override).

    See :func:`_resolve_pr_for_branch` for the ``force`` semantics."""
    from agentic_pr_dash import github_api  # noqa: PLC0415
    from agentic_pr_dash.models import PRData, PRStatus  # noqa: PLC0415

    # See _resolve_pr_for_branch: shares the same short-TTL snapshot (BOU-1923).
    prs = github_api.list_open_prs_cached(cwd, force=force)
    raw: dict | None = None
    if prs is None:
        # Quota-safe REST fallback (BOU-1966): see _resolve_pr_for_branch. A
        # rate-limited author list still verifies an explicit --pr N via the
        # REST pulls endpoint; anything unverifiable stays fail-closed.
        raw = _rest_fallback_entry_by_number(pr_number, cwd)
        if raw is None:
            return _GH_UNAVAILABLE
    elif prs:
        for entry in prs:
            if entry.get("number") == pr_number:
                raw = entry
                break

    # See _resolve_pr_for_branch: keep a live gh-availability signal across the
    # detail fetch so a warm snapshot (or REST-fallback resolution) + a
    # concurrent outage during the fetch surfaces _GH_UNAVAILABLE rather than a
    # false "clean" (BOU-1923, BOU-1966 — event-count compare, not the
    # tick-scoped boolean, which the failed list already set on the fallback
    # path).
    _rl_events_before = github_api.rate_limit_events()
    latest_sha, latest_date = github_api.get_latest_commit(pr_number, cwd)
    checks = github_api.get_ci_checks(pr_number, cwd)
    failing = [
        c.name
        for c in checks
        if c.conclusion == "failure" and not github_api._is_infra_check(c.name)
    ]
    review_comments = github_api.get_unaddressed_comments(pr_number, latest_date, cwd)
    if github_api.rate_limit_events() != _rl_events_before:
        return _GH_UNAVAILABLE
    merge_state = (raw or {}).get("mergeStateStatus", "unknown")
    mergeable = (raw or {}).get("mergeable", "unknown")

    return PRData(
        number=pr_number,
        title=(raw or {}).get("title", ""),
        branch=(raw or {}).get("headRefName", ""),
        base_branch=(raw or {}).get("baseRefName", "main"),
        url=(raw or {}).get("url", ""),
        is_draft=bool((raw or {}).get("isDraft", False)),
        merge_state=merge_state,
        mergeable=mergeable,
        ci_checks=checks,
        failing_checks=failing,
        review_comments=review_comments,
        latest_commit_sha=latest_sha,
        latest_commit_date=latest_date,
        worktree_path=cwd,
        status=PRStatus.CLEAN,
    )


def _pr_draft_status(cwd: str, pr_number: int):
    """Optional[bool]: True=draft, False=non-draft, None=could-not-determine."""
    import json  # noqa: PLC0415

    from agentic_pr_dash import github_api  # noqa: PLC0415

    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "isDraft"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
            env=github_api.automation_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "")
    except ValueError:
        return None
    if not isinstance(data, dict) or "isDraft" not in data:
        return None
    return bool(data["isDraft"])


def _pr_head_branch(cwd: str, pr_number: int):
    """The PR's head branch name (``headRefName``), or ``None`` if gh can't say."""
    import json  # noqa: PLC0415

    from agentic_pr_dash import github_api  # noqa: PLC0415

    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "headRefName"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
            env=github_api.automation_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    head = data.get("headRefName")
    return head if isinstance(head, str) and head else None


def _gh_pr_list_json(
    cwd: str, extra_args: list[str], fields: str, timeout: float = 15
) -> list | None:
    """Run `gh pr list --author <pr_author> --state open --json <fields> <extra>`.

    The author comes from config (``pr_author``, default ``@me``) — under an
    App-token automation identity ``@me`` is the App bot and every PR-state
    probe would silently see no PRs (BOU-1923); see
    :attr:`agentic_pr_dash.config.Config.pr_author`.

    ``timeout`` bounds the gh subprocess; Stop-context callers pass the remaining
    reconciliation budget so a single slow root cannot blow the Stop-hook
    deadline (BOU-1787 review). Sub-second budgets are honored verbatim — no 1s
    floor — so a nearly-exhausted budget times out at the actual remaining time
    instead of overrunning the deadline. A non-positive budget means "no time
    left": skip the call entirely (PR #54 review round 2).
    """
    import json  # noqa: PLC0415

    from agentic_pr_dash import github_api  # noqa: PLC0415
    from agentic_pr_dash.config import load as _load_config  # noqa: PLC0415

    if timeout <= 0:
        return None
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--author", _load_config(cwd).pr_author, "--state", "open", *extra_args, "--json", fields],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=github_api.automation_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "[]")
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def _resolve_open_pr_for_branch(cwd: str, branch: str):
    """(pr_number, is_draft) for this branch's open @me PR, or None if none."""
    data = _gh_pr_list_json(cwd, ["--head", branch], "number,isDraft")
    if not data:
        return None
    entry = data[0]
    return int(entry.get("number")), bool(entry.get("isDraft", False))


def _list_my_open_prs(cwd: str, timeout: float = 15) -> dict[str, tuple[int, bool]]:
    """Map branch -> (pr_number, is_draft) for the user's open PRs; {} on failure.

    ``timeout`` bounds the underlying gh subprocess (BOU-1787 review)."""
    data = _gh_pr_list_json(cwd, [], "number,headRefName,isDraft", timeout=timeout)
    if not data:
        return {}
    out: dict[str, tuple[int, bool]] = {}
    for entry in data:
        branch = entry.get("headRefName")
        if not branch:
            continue
        number = int(entry.get("number"))
        is_draft = bool(entry.get("isDraft", False))
        existing = out.get(branch)
        if existing is not None and not existing[1] and is_draft:
            continue
        out[branch] = (number, is_draft)
    return out


def _unresolved_review_threads(pr_number: int, cwd: str):
    """Non-outdated, unresolved review threads for a PR."""
    from agentic_pr_dash import github_api  # noqa: PLC0415

    threads = github_api.get_review_threads(pr_number, cwd)
    return [t for t in threads if not t.is_resolved and not t.is_outdated]


def pr_has_unresolved_review_threads(pr_number: int, cwd: str) -> bool:
    """True if the PR has at least one non-outdated, unresolved review thread."""
    return bool(_unresolved_review_threads(pr_number, cwd))


def _pr_open_state(pr_number: int, cwd: str):
    """(state, url, has_failing_ci, failing_checks, review_decision, merge_state, mergeable) for a PR."""
    from agentic_pr_dash import github_api  # noqa: PLC0415
    import json as _json  # noqa: PLC0415

    unavailable = ("unknown", "", False, [], "", "", "")
    # Route through github_api._run (not raw subprocess) so a rate-limit on this
    # detached PR-state probe is detected and recorded in the per-tick
    # rate_limit_seen() flag — otherwise a detached-only waiter whose only gh call
    # is this probe can't tell a quota outage from "no PRs" and exits 0 during the
    # outage (BOU-1949, #62 follow-up). _run also gives it the connectivity /
    # rate-limit backoff, and _run_once already catches OSError/TimeoutExpired.
    res = github_api._run(
        [
            "gh", "pr", "view", str(pr_number),
            "--json", "state,url,isDraft,reviewDecision,mergeStateStatus,mergeable",
        ],
        timeout_s=15, cwd=cwd,
    )
    if res.returncode != 0:
        return unavailable
    try:
        d = _json.loads(res.stdout or "{}")
    except ValueError:
        return unavailable
    state = str(d.get("state", "unknown")).lower()
    if state == "open" and bool(d.get("isDraft", False)):
        state = "draft"
    url = str(d.get("url", ""))
    checks = github_api.get_ci_checks(pr_number, cwd)
    failing = [c.name for c in checks
               if c.conclusion == "failure" and not github_api._is_infra_check(c.name)]
    review_decision = str(d.get("reviewDecision") or "")
    merge_state = str(d.get("mergeStateStatus") or "")
    mergeable = str(d.get("mergeable") or "")
    return (state, url, bool(failing), failing, review_decision, merge_state, mergeable)


def _unpack_pr_open_state(raw):
    """Normalize legacy tuples from tests/callers to the current 7-field shape."""
    if len(raw) == 4:
        state, url, has_fail, failing = raw
        return state, url, has_fail, failing, "", "", ""
    if len(raw) == 6:
        state, url, has_fail, failing, review_decision, merge_state = raw
        return state, url, has_fail, failing, review_decision, merge_state, ""
    state, url, has_fail, failing, review_decision, merge_state, mergeable = raw
    return state, url, has_fail, failing, review_decision, merge_state, mergeable


def _thread_is_p1(thread) -> bool:
    bodies = [thread.top.body] + [r.body for r in getattr(thread, "replies", [])]
    return any("p1" in (b or "").lower() for b in bodies)
