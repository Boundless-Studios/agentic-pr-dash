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
    return "could not list PRs (gh unavailable)\n" + failure.describe()


def _resolve_pr_for_branch(cwd: str):
    """Find the open PR whose headRefName matches the current branch."""
    from agentic_pr_dash import github_api  # noqa: PLC0415
    from agentic_pr_dash.models import PRData, PRStatus  # noqa: PLC0415

    branch = _current_branch(cwd)
    if not branch:
        return None

    prs = github_api.list_open_prs(cwd)
    if prs is None:
        return _GH_UNAVAILABLE
    if not prs:
        return None

    raw: dict | None = None
    for entry in prs:
        if entry.get("headRefName") == branch:
            raw = entry
            break
    if raw is None:
        return None

    pr_number = int(raw["number"])
    latest_sha, latest_date = github_api.get_latest_commit(pr_number, cwd)
    checks = github_api.get_ci_checks(pr_number, cwd)
    failing = [
        c.name
        for c in checks
        if c.conclusion == "failure" and not github_api._is_infra_check(c.name)
    ]
    review_comments = github_api.get_unaddressed_comments(pr_number, latest_date, cwd)
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


def _resolve_pr_by_number(pr_number: int, cwd: str):
    """Resolve a PR by explicit number (for --pr override)."""
    from agentic_pr_dash import github_api  # noqa: PLC0415
    from agentic_pr_dash.models import PRData, PRStatus  # noqa: PLC0415

    prs = github_api.list_open_prs(cwd)
    if prs is None:
        return _GH_UNAVAILABLE
    raw: dict | None = None
    if prs:
        for entry in prs:
            if entry.get("number") == pr_number:
                raw = entry
                break

    latest_sha, latest_date = github_api.get_latest_commit(pr_number, cwd)
    checks = github_api.get_ci_checks(pr_number, cwd)
    failing = [
        c.name
        for c in checks
        if c.conclusion == "failure" and not github_api._is_infra_check(c.name)
    ]
    review_comments = github_api.get_unaddressed_comments(pr_number, latest_date, cwd)
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

    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "isDraft"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
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

    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "headRefName"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
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
    """Run `gh pr list --author @me --state open --json <fields> <extra>`.

    ``timeout`` bounds the gh subprocess; Stop-context callers pass the remaining
    reconciliation budget so a single slow root cannot blow the Stop-hook
    deadline (BOU-1787 review).
    """
    import json  # noqa: PLC0415

    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--author", "@me", "--state", "open", *extra_args, "--json", fields],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=max(1.0, timeout),
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
    try:
        res = subprocess.run(
            [
                "gh", "pr", "view", str(pr_number),
                "--json", "state,url,isDraft,reviewDecision,mergeStateStatus,mergeable",
            ],
            cwd=cwd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return unavailable
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
