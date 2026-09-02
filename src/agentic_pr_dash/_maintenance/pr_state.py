"""PR-resolution and review-thread helpers."""
from __future__ import annotations

import subprocess
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from ._common import _current_branch

_GH_UNAVAILABLE = object()  # sentinel: gh CLI failed
_AUTHORITATIVE_MAINTENANCE_READ: ContextVar[bool] = ContextVar(
    "authoritative_maintenance_read", default=False
)


@contextmanager
def authoritative_maintenance_read():
    """Force resolver cache bypass without changing its adapter call shape."""
    from agentic_pr_dash import github_api  # noqa: PLC0415

    # The Stop path primes detail snapshots before resolving individual PRs.
    # A maintenance checkpoint must start after that cache boundary so every
    # dependent read (commit, CI, threads, reviews) reaches GitHub again.
    # A pre-loop batch can change before this particular PR is checked. Exact
    # maintenance observations therefore always discard it and re-read the
    # current PR; batching remains an optimization for non-authoritative probes.
    github_api.clear_pr_batch_cache()
    token = _AUTHORITATIVE_MAINTENANCE_READ.set(True)
    try:
        yield
    finally:
        _AUTHORITATIVE_MAINTENANCE_READ.reset(token)


def authoritative_observation_matches(pr) -> bool:
    """Return whether ``pr`` carries a complete exact-head maintenance read."""
    observed_at = pr.maintenance_observed_at
    return bool(
        observed_at is not None
        and observed_at.tzinfo is not None
        and observed_at.utcoffset() is not None
        and pr.maintenance_observed_head_sha == pr.latest_commit_sha
        and pr.maintenance_observed_base_branch == pr.base_branch
    )


def _authoritative_review_comments(pr_number: int, latest_date: str, cwd: str):
    """Read threads and submitted review bodies as one fail-closed slice."""
    from agentic_pr_dash import github_api  # noqa: PLC0415

    observation = github_api.scan_review_threads_observation(
        pr_number, latest_date, cwd
    )
    if not observation.observable or observation.value is None:
        return _GH_UNAVAILABLE
    comments, _decisions = observation.value
    return comments


def _authoritative_unresolved_review_threads(pr_number: int, cwd: str):
    """Read unresolved threads fail-closed while preserving adapter call shape."""
    try:
        if _unresolved_review_threads.__module__ == __name__:
            return _unresolved_review_threads(pr_number, cwd, strict=True)
        else:
            # Tests and downstream adapters historically expose a two-argument
            # callable. Their return is the explicit observation contract.
            return _unresolved_review_threads(pr_number, cwd)
    except RuntimeError:
        return _GH_UNAVAILABLE


def _authoritative_ci_checks(pr_number: int, cwd: str):
    """Read CI without collapsing an unavailable observation into clean."""
    from agentic_pr_dash import github_api  # noqa: PLC0415

    observation = github_api.get_ci_checks_observation(pr_number, cwd)
    if not observation.observable or observation.value is None:
        return _GH_UNAVAILABLE
    return observation.value


def _authoritative_identity_snapshot(
    pr_number: int,
    expected_head: str,
    expected_base: str,
    expected_branch: str,
    cwd: str,
) -> dict | None:
    """Fence a maintenance read against PR identity changes during observation."""
    from agentic_pr_dash import github_api  # noqa: PLC0415

    prs = github_api.list_open_prs_cached(cwd, force=True)
    fresh = None
    if prs is not None:
        fresh = next((entry for entry in prs if entry.get("number") == pr_number), None)
    if fresh is None:
        fresh = github_api._rest_pr_payload(pr_number, cwd=cwd)
    if fresh is None or str(fresh.get("state") or "open").lower() != "open":
        return None
    if (
        fresh.get("headRefOid") != expected_head
        or fresh.get("baseRefName") != expected_base
        or fresh.get("headRefName") != expected_branch
    ):
        return None
    return fresh


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

    Deliberately no ``pr_author`` check here (unlike the branch fallback): the
    normal ``--pr N`` path also proceeds when the explicitly-named PR is absent
    from the author-scoped list, so enforcing authorship only under quota would
    make the fallback STRICTER than the path it substitutes for.
    """
    from agentic_pr_dash import github_api  # noqa: PLC0415

    if not _list_failure_is_rate_limited():
        return None
    return github_api._rest_pr_payload(pr_number, cwd=cwd)


def _login_key(login: str) -> str:
    """Canonical form of a GitHub login for author comparison.

    ``gh pr list --author`` accepts the ``app/<name>`` form for GitHub Apps
    while REST serializes the same identity as ``<name>[bot]``, and logins are
    case-insensitive — normalize both spellings so a configured bot author
    matches its REST payload."""
    from agentic_pr_dash import github_api  # noqa: PLC0415

    return github_api._login_key(login)


def _payload_author_login(raw: dict) -> str:
    author = raw.get("author") or {}
    return str(author.get("login") or "") if isinstance(author, dict) else ""


def _rest_payload_author_is_tracked(
    raw: dict, cwd: str, *, deadline: float | None = None
) -> bool:
    """True when a REST fallback payload's author is the configured ``pr_author``.

    The normal branch resolution only considers ``gh pr list --author
    <pr_author>``, so the quota fallback must preserve that author-scoped
    contract: on a shared-repo branch, another maintainer's open PR must not be
    adopted (blocked on / serviced) as this session's PR (PR #77 review).
    ``@me`` resolves via REST ``GET /user``; an unresolvable viewer or a
    missing payload author fails closed."""
    from agentic_pr_dash import github_api  # noqa: PLC0415
    from agentic_pr_dash.config import load as _load_config  # noqa: PLC0415

    configured = _load_config(cwd).pr_author
    expected = configured
    if configured == "@me":
        expected = (
            github_api._rest_viewer_login(cwd, deadline=deadline)
            if deadline is not None
            else github_api._rest_viewer_login(cwd)
        )
    author = raw.get("author")
    actual = str(author.get("login") or "") if isinstance(author, dict) else ""
    if not expected or not actual:
        return False
    return _login_key(actual) == _login_key(expected)


def _rest_fallback_entry_for_branch(
    branch: str,
    cwd: str,
    *,
    force: bool = False,
    deadline: float | None = None,
    head_owner: str | None = None,
):
    """Quota fallback (BOU-1966): raw PR entry for the current branch via REST.

    Resolves owner (REST) → exact owner-qualified head numbers (REST) → full
    payload (REST). Deliberately avoids ``gh pr list --head`` — that path is
    GraphQL, exactly what is quota-blocked here. An empty exact-head result
    ALSO returns ``None`` (→ fail closed): the owner-qualified query cannot see
    a fork-backed head, so "no match" under quota exhaustion is weaker evidence
    than the author-scoped list and must not read as a verified "no PR".

    Every candidate must ALSO match the configured ``pr_author`` — the normal
    path is author-scoped, so a foreign maintainer's PR on a shared branch
    must not be adopted under rate limiting (PR #77 review). No author-matching
    candidate → ``None`` (fail closed).
    """
    from agentic_pr_dash import github_api  # noqa: PLC0415

    if not force and not _list_failure_is_rate_limited():
        return None
    owner = head_owner or (
        github_api._rest_repo_owner(cwd, deadline=deadline)
        if deadline is not None
        else github_api._rest_repo_owner(cwd)
    )
    if not owner:
        return None
    numbers = (
        github_api._exact_head_pr_numbers(
            owner, branch, "open", cwd=cwd, deadline=deadline
        )
        if deadline is not None
        else github_api._exact_head_pr_numbers(owner, branch, "open", cwd=cwd)
    )
    if not numbers:
        return None
    matches = []
    for number in numbers:
        raw = (
            github_api._rest_pr_payload(number, cwd=cwd, deadline=deadline)
            if deadline is not None
            else github_api._rest_pr_payload(number, cwd=cwd)
        )
        if raw is None:
            return None  # REST failure mid-verification → fail closed
        if str(raw.get("headRefName") or "") != branch:
            continue
        if not _rest_payload_author_is_tracked(raw, cwd, deadline=deadline):
            continue
        matches.append(raw)
    return matches[0] if len(matches) == 1 else None


def _resolve_pr_entry_for_branch(
    cwd: str,
    branch: str,
    *,
    head_oid: str = "",
    validate_snapshot_state: bool = False,
    deadline: float | None = None,
):
    """Resolve one exact, tracked open-PR entry for ``branch``.

    The ordinary branch helper intentionally serves the shared snapshot without
    another network call. Ownership readers have a stricter contract: a cached
    entry can be the PR that was closed immediately before a worktree switched
    branches, so a snapshot candidate is revalidated through the REST resource
    before it is used. If that candidate is stale, a forced REST head lookup gets
    the replacement PR without trusting the author-wide GraphQL list.

    ``None`` means a successful lookup found no open PR. ``_GH_UNAVAILABLE``
    means the current branch could not be established with sufficient evidence.
    """
    from agentic_pr_dash import github_api  # noqa: PLC0415

    if not branch:
        return None
    if deadline is not None and time.monotonic() >= deadline:
        return _GH_UNAVAILABLE

    def _pick(entries: list[dict]):
        exact = [
            entry for entry in entries
            if isinstance(entry, dict)
            and entry.get("headRefName") == branch
            and isinstance(entry.get("number"), int)
        ]
        if not exact:
            return None
        if head_oid:
            matching_head = [entry for entry in exact if entry.get("headRefOid") == head_oid]
            if matching_head:
                return matching_head[0] if len(matching_head) == 1 else _GH_UNAVAILABLE
        if len(exact) > 1:
            # A branch name is not an ownership proof: fork/base PRs can share
            # it. If the local head cannot disambiguate them, fail closed
            # instead of binding arbitrarily to ``exact[0]``.
            return _GH_UNAVAILABLE
        return exact[0]

    snapshot = github_api.peek_pr_snapshot(cwd)
    if snapshot is not None:
        candidate = _pick(snapshot)
        if candidate is _GH_UNAVAILABLE:
            return _GH_UNAVAILABLE
        if candidate is None:
            if not validate_snapshot_state:
                return None
            replacement = (
                _rest_fallback_entry_for_branch(
                    branch, cwd, force=True, deadline=deadline
                )
                if deadline is not None
                else _rest_fallback_entry_for_branch(branch, cwd, force=True)
            )
            return replacement if replacement is not None else _GH_UNAVAILABLE
        if not validate_snapshot_state:
            return candidate

        # A snapshot hit is not proof that the PR is still open. REST is the
        # quota-safe, current-state check and also detects a reused branch whose
        # open PR changed while the list snapshot was still warm.
        live = (
            github_api._rest_pr_payload(
                int(candidate["number"]), cwd=cwd, deadline=deadline
            )
            if deadline is not None
            else github_api._rest_pr_payload(int(candidate["number"]), cwd=cwd)
        )
        if (
            live is not None
            and str(live.get("state") or "").lower() == "open"
            and live.get("headRefName") == branch
        ):
            # The resource proves this cached PR is still open, but not that it
            # remains the only open PR for the exact head. A new PR can appear
            # after the author-wide snapshot was cached.
            live_head_owner = live.get("headRepositoryOwner")
            if isinstance(live_head_owner, dict):
                live_head_owner = live_head_owner.get("login")
            if not isinstance(live_head_owner, str):
                live_head_owner = None
            unique = (
                _rest_fallback_entry_for_branch(
                    branch,
                    cwd,
                    force=True,
                    deadline=deadline,
                    head_owner=live_head_owner,
                )
                if deadline is not None
                else _rest_fallback_entry_for_branch(
                    branch, cwd, force=True, head_owner=live_head_owner
                )
            )
            if unique is not None and unique.get("number") == live.get("number"):
                return live
            return _GH_UNAVAILABLE
        # The cached candidate was closed, changed head, or could not be
        # verified. Ask REST for the exact current head before declaring the
        # worktree unbound. This path is deliberately not gated on a prior
        # GraphQL error: the snapshot itself may be the stale source.
        replacement = (
            _rest_fallback_entry_for_branch(
                branch, cwd, force=True, deadline=deadline
            )
            if deadline is not None
            else _rest_fallback_entry_for_branch(branch, cwd, force=True)
        )
        if replacement is not None:
            return replacement
        # The owner-qualified REST head query cannot exclude a fork-backed
        # replacement.  A miss is therefore unobservable even when the stale
        # candidate itself was positively confirmed closed.
        return _GH_UNAVAILABLE

    data = (
        _gh_pr_list_json(
            cwd,
            ["--head", branch],
            "number,isDraft,headRefName,headRefOid",
            deadline=deadline,
        )
        if deadline is not None
        else _gh_pr_list_json(
            cwd, ["--head", branch], "number,isDraft,headRefName,headRefOid"
        )
    )
    if data is None:
        # On a GraphQL/list failure, REST is the only authoritative fallback.
        # A failed REST verification remains unknown rather than becoming a
        # false "no PR" answer (the BOU-2798 waiter must stay fail-loud).
        replacement = (
            _rest_fallback_entry_for_branch(branch, cwd, deadline=deadline)
            if deadline is not None
            else _rest_fallback_entry_for_branch(branch, cwd)
        )
        return replacement if replacement is not None else _GH_UNAVAILABLE
    return _pick(data)


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

    authoritative = _AUTHORITATIVE_MAINTENANCE_READ.get()
    force = force or authoritative
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
    checks = (
        _authoritative_ci_checks(pr_number, cwd)
        if authoritative
        else github_api.get_ci_checks(pr_number, cwd)
    )
    if checks is _GH_UNAVAILABLE:
        return _GH_UNAVAILABLE
    failing = [
        c.name
        for c in checks
        if c.status == "completed" and c.conclusion not in {"success", "skipped", "neutral"}
        and not github_api._is_infra_check(c.name)
    ]
    review_comments = (
        _authoritative_review_comments(pr_number, latest_date, cwd)
        if authoritative
        else github_api.get_unaddressed_comments(pr_number, latest_date, cwd)
    )
    if review_comments is _GH_UNAVAILABLE:
        return _GH_UNAVAILABLE
    if github_api.rate_limit_events() != _rl_events_before:
        return _GH_UNAVAILABLE
    observed_head = raw.get("headRefOid") or latest_sha
    observed_base = raw.get("baseRefName", "main")
    observed_branch = raw.get("headRefName") or branch
    if authoritative:
        if not latest_sha or latest_sha != observed_head:
            return _GH_UNAVAILABLE
        fresh = _authoritative_identity_snapshot(
            pr_number, observed_head, observed_base, observed_branch, cwd
        )
        if fresh is None:
            return _GH_UNAVAILABLE
        raw = fresh
    merge_state = raw.get("mergeStateStatus", "unknown")
    mergeable = raw.get("mergeable", "unknown")

    return PRData(
        number=pr_number,
        author=_payload_author_login(raw),
        title=raw.get("title", ""),
        branch=branch,
        base_branch=raw.get("baseRefName", "main"),
        url=raw.get("url", ""),
        is_draft=bool(raw.get("isDraft", False)),
        merge_state=merge_state,
        mergeable=mergeable,
        review_decision=raw.get("reviewDecision", "") or "none",
        ci_checks=checks,
        failing_checks=failing,
        review_comments=review_comments,
        latest_commit_sha=observed_head,
        latest_commit_date=latest_date,
        maintenance_observed_head_sha=observed_head if authoritative else "",
        maintenance_observed_base_branch=observed_base if authoritative else "",
        maintenance_observed_at=datetime.now(UTC) if authoritative else None,
        worktree_path=cwd,
        status=PRStatus.CLEAN,
    )


def _resolve_pr_by_number(
    pr_number: int,
    cwd: str,
    *,
    force: bool = False,
    include_reviews: bool = True,
):
    """Resolve a PR by explicit number (for --pr override).

    See :func:`_resolve_pr_for_branch` for the ``force`` semantics."""
    from agentic_pr_dash import github_api  # noqa: PLC0415
    from agentic_pr_dash.models import PRData, PRStatus  # noqa: PLC0415

    authoritative = _AUTHORITATIVE_MAINTENANCE_READ.get()
    force = force or authoritative
    # See _resolve_pr_for_branch: shares the same short-TTL snapshot (BOU-1923).
    prs = github_api.list_open_prs_cached(cwd, force=force)
    raw: dict | None = None
    needs_review_decision_probe = False
    if prs is None:
        # Quota-safe REST fallback (BOU-1966): see _resolve_pr_for_branch. A
        # rate-limited author list still verifies an explicit --pr N via the
        # REST pulls endpoint; anything unverifiable stays fail-closed.
        raw = _rest_fallback_entry_by_number(pr_number, cwd)
        if raw is None:
            return _GH_UNAVAILABLE
        needs_review_decision_probe = True
    elif prs:
        for entry in prs:
            if entry.get("number") == pr_number:
                raw = entry
                break
    if prs is not None and raw is None:
        # The configured author snapshot is not authoritative for an explicit
        # PR number: shared/fork PRs may belong to another author. Verify the
        # named PR directly, and require positive evidence that it remains open.
        raw = github_api._rest_pr_payload(pr_number, cwd=cwd)
        if raw is None:
            return _GH_UNAVAILABLE
        if str(raw.get("state") or "").lower() != "open":
            return None
        needs_review_decision_probe = True

    if needs_review_decision_probe:
        review_decision, diagnostic = _gh_pr_view_field(
            cwd, pr_number, "reviewDecision"
        )
        if diagnostic:
            return _GH_UNAVAILABLE
        raw["reviewDecision"] = review_decision or "none"

    # See _resolve_pr_for_branch: keep a live gh-availability signal across the
    # detail fetch so a warm snapshot (or REST-fallback resolution) + a
    # concurrent outage during the fetch surfaces _GH_UNAVAILABLE rather than a
    # false "clean" (BOU-1923, BOU-1966 — event-count compare, not the
    # tick-scoped boolean, which the failed list already set on the fallback
    # path).
    _rl_events_before = github_api.rate_limit_events()
    latest_sha, latest_date = github_api.get_latest_commit(pr_number, cwd)
    checks = (
        _authoritative_ci_checks(pr_number, cwd)
        if authoritative
        else github_api.get_ci_checks(pr_number, cwd)
    )
    if checks is _GH_UNAVAILABLE:
        return _GH_UNAVAILABLE
    failing = [
        c.name
        for c in checks
        if c.status == "completed" and c.conclusion not in {"success", "skipped", "neutral"}
        and not github_api._is_infra_check(c.name)
    ]
    review_comments = (
        (
            _authoritative_review_comments(pr_number, latest_date, cwd)
            if authoritative
            else github_api.get_unaddressed_comments(pr_number, latest_date, cwd)
        )
        if include_reviews
        else []
    )
    if review_comments is _GH_UNAVAILABLE:
        return _GH_UNAVAILABLE
    if github_api.rate_limit_events() != _rl_events_before:
        return _GH_UNAVAILABLE
    observed_head = (raw or {}).get("headRefOid") or latest_sha
    observed_base = (raw or {}).get("baseRefName", "main")
    observed_branch = (raw or {}).get("headRefName", "")
    if authoritative:
        if not latest_sha or latest_sha != observed_head:
            return _GH_UNAVAILABLE
        fresh = _authoritative_identity_snapshot(
            pr_number, observed_head, observed_base, observed_branch, cwd
        )
        if fresh is None:
            return _GH_UNAVAILABLE
        raw = fresh
        if needs_review_decision_probe:
            review_decision, diagnostic = _gh_pr_view_field(
                cwd, pr_number, "reviewDecision"
            )
            if diagnostic:
                return _GH_UNAVAILABLE
            raw["reviewDecision"] = review_decision or "none"
            if _authoritative_identity_snapshot(
                pr_number, observed_head, observed_base, observed_branch, cwd
            ) is None:
                return _GH_UNAVAILABLE
    merge_state = (raw or {}).get("mergeStateStatus", "unknown")
    mergeable = (raw or {}).get("mergeable", "unknown")

    return PRData(
        number=pr_number,
        author=_payload_author_login(raw or {}),
        title=(raw or {}).get("title", ""),
        branch=(raw or {}).get("headRefName", ""),
        base_branch=(raw or {}).get("baseRefName", "main"),
        url=(raw or {}).get("url", ""),
        is_draft=bool((raw or {}).get("isDraft", False)),
        merge_state=merge_state,
        mergeable=mergeable,
        review_decision=(raw or {}).get("reviewDecision", "") or "none",
        ci_checks=checks,
        failing_checks=failing,
        review_comments=review_comments,
        latest_commit_sha=observed_head,
        latest_commit_date=latest_date,
        maintenance_observed_head_sha=observed_head if authoritative else "",
        maintenance_observed_base_branch=observed_base if authoritative else "",
        maintenance_observed_at=datetime.now(UTC) if authoritative else None,
        worktree_path=cwd,
        status=PRStatus.CLEAN,
    )


# BOU-2406: a `gh pr view` issued seconds after `gh pr create` can fail while
# GitHub is still settling, and a single failed probe used to permanently decline
# to arm. Retry a bounded number of times before giving up. Kept small: arm runs
# inside a Stop hook with a ~10s preflight budget, so the total must stay well
# under it.
_PR_VIEW_ATTEMPTS = 3
_PR_VIEW_BACKOFF_SECONDS = 0.75
# BOU-2477: the per-attempt subprocess timeout, independent of any caller
# deadline. 15s x 3 attempts = 45s worst case for ONE field when `gh` hangs
# rather than fails fast — and `arm` probes two fields with independently
# fresh budgets, ~93s total, against the ~10s budget the module is meant to
# respect. Callers with a shared deadline (see `deadline=` below) already cap
# each attempt tighter via `min(this, remaining)`; this constant is only the
# ceiling for a caller that passes no deadline at all.
_PR_VIEW_PER_ATTEMPT_TIMEOUT_SECONDS = 3


def _gh_pr_view_field(
    cwd: str, pr_number: int, field: str, *, deadline: float | None = None,
) -> tuple[object, str]:
    """Read one ``gh pr view --json <field>`` value, with bounded retries.

    Returns ``(value, diagnostic)``. ``value`` is ``_GH_UNAVAILABLE`` when the
    field could not be determined, in which case ``diagnostic`` explains why --
    the captured stderr, not an assertion that gh is missing. Previously every
    failure mode collapsed to a bare ``None`` and the stderr was discarded, so
    "gh unavailable" was printed for rate limits, 404s and transient blips alike
    (BOU-2406).

    ``deadline`` (BOU-2477), a ``time.monotonic()`` timestamp, bounds the WHOLE
    call by wall clock, not merely by attempt count — a caller probing several
    fields (``arm``'s isDraft + headRefName) passes the SAME deadline to both so
    one shared budget governs the total, not two independently-fresh ones. Each
    attempt's subprocess timeout is ``min(per-attempt cap, remaining budget)``,
    honoring a sub-second remaining budget verbatim (no floor) so a
    nearly-exhausted budget can't overrun. Once the budget is gone before an
    attempt starts, remaining attempts are skipped and the sentinel is returned
    with a diagnostic that says so explicitly — distinguishable from "gh
    unavailable", a rate limit, or a 404, so a Stop-hook caller can tell
    "we ran out of time" from "gh actually failed" (the exact conflation
    BOU-2406 was filed about, one layer up).
    """
    import json  # noqa: PLC0415
    import time  # noqa: PLC0415

    from agentic_pr_dash import github_api  # noqa: PLC0415

    last = "no attempt was made"
    for attempt in range(1, _PR_VIEW_ATTEMPTS + 1):
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _GH_UNAVAILABLE, (
                    f"budget exhausted before attempt {attempt} of "
                    f"{_PR_VIEW_ATTEMPTS} ({last})"
                )
            attempt_timeout = min(_PR_VIEW_PER_ATTEMPT_TIMEOUT_SECONDS, remaining)
        else:
            attempt_timeout = _PR_VIEW_PER_ATTEMPT_TIMEOUT_SECONDS
        try:
            result = subprocess.run(
                ["gh", "pr", "view", str(pr_number), "--json", field],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=attempt_timeout,
                env=github_api.automation_subprocess_env(),
            )
        except FileNotFoundError:
            # The only failure that genuinely means "gh unavailable". Not
            # retryable, and worth saying plainly.
            return _GH_UNAVAILABLE, "the `gh` executable was not found on PATH"
        except (OSError, subprocess.TimeoutExpired) as exc:
            last = f"attempt {attempt}: {type(exc).__name__}: {exc}"
        else:
            if result.returncode == 0:
                try:
                    data = json.loads(result.stdout or "")
                except ValueError:
                    last = f"attempt {attempt}: gh returned non-JSON: {(result.stdout or '')[:200]!r}"
                else:
                    if isinstance(data, dict) and field in data:
                        return data[field], ""
                    last = f"attempt {attempt}: gh JSON has no {field!r}: {data!r}"
            else:
                last = (
                    f"attempt {attempt}: gh exited {result.returncode}: "
                    f"{(result.stderr or '').strip()[:300] or '<no stderr>'}"
                )
        if attempt < _PR_VIEW_ATTEMPTS:
            sleep_for = _PR_VIEW_BACKOFF_SECONDS
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return _GH_UNAVAILABLE, (
                        f"budget exhausted after attempt {attempt} of "
                        f"{_PR_VIEW_ATTEMPTS} ({last})"
                    )
                sleep_for = min(sleep_for, remaining)
            time.sleep(sleep_for)
    return _GH_UNAVAILABLE, last


def _pr_draft_status_detailed(
    cwd: str, pr_number: int, *, deadline: float | None = None,
) -> tuple[bool | None, str]:
    """``(is_draft, diagnostic)``; ``is_draft`` is None when undeterminable."""
    value, diagnostic = _gh_pr_view_field(cwd, pr_number, "isDraft", deadline=deadline)
    if value is _GH_UNAVAILABLE:
        return None, diagnostic
    return bool(value), ""


def _pr_draft_status(cwd: str, pr_number: int):
    """Optional[bool]: True=draft, False=non-draft, None=could-not-determine."""
    return _pr_draft_status_detailed(cwd, pr_number)[0]


def _pr_head_branch_detailed(
    cwd: str, pr_number: int, *, deadline: float | None = None,
) -> tuple[str | None, str]:
    """``(head_branch, diagnostic)``; branch is None when undeterminable."""
    value, diagnostic = _gh_pr_view_field(cwd, pr_number, "headRefName", deadline=deadline)
    if value is _GH_UNAVAILABLE:
        return None, diagnostic
    if isinstance(value, str) and value:
        return value, ""
    return None, f"gh reported an empty headRefName: {value!r}"


def _pr_head_branch(cwd: str, pr_number: int):
    """The PR's head branch name (``headRefName``), or ``None`` if gh can't say."""
    return _pr_head_branch_detailed(cwd, pr_number)[0]


def _gh_pr_list_json(
    cwd: str,
    extra_args: list[str],
    fields: str,
    timeout: float = 15,
    *,
    deadline: float | None = None,
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

    BOU-2535: routed through ``github_api._run`` (not a bare ``subprocess.run``)
    so this call gets the SAME bounded connectivity-retry-with-backoff and
    rate-limit classification ``list_open_prs`` already has. This is the
    `gh pr list` this package's `check`/`arm` path resolves branches through —
    the loop's own "could not list PRs (gh unavailable)" / "error connecting to
    api.github.com" failures were on exactly this uncovered call site: a
    transient connection blip failed the whole probe with no retry, distinct
    from (and previously indistinguishable from) an auth or rate-limit
    failure.
    """
    import json  # noqa: PLC0415
    import time  # noqa: PLC0415

    from agentic_pr_dash import github_api  # noqa: PLC0415
    from agentic_pr_dash.config import load as _load_config  # noqa: PLC0415

    if deadline is not None:
        timeout = min(timeout, max(0.0, deadline - time.monotonic()))
    if timeout <= 0:
        return None
    call_deadline = deadline if deadline is not None else time.monotonic() + timeout
    command = [
        "gh", "pr", "list", "--state", "open", "--limit",
        github_api._GH_COMPLETE_LIST_LIMIT, *extra_args, "--json",
        ",".join(dict.fromkeys([*fields.split(","), "author"])),
    ]
    result = github_api._run(
        command,
        timeout_s=timeout, deadline=call_deadline,
        cwd=cwd,
    )
    if result.returncode != 0:
        github_api._record_list_open_prs_failure(command, result)
        return None
    try:
        data = json.loads(result.stdout or "[]")
    except ValueError:
        github_api._record_list_open_prs_failure(
            command, result, reason="invalid-json"
        )
        return None
    if not isinstance(data, list):
        github_api._record_list_open_prs_failure(
            command, result, reason="not-a-list"
        )
        return None
    author = _load_config(cwd).pr_author
    if author == "@me":
        viewer_timeout = timeout
        if deadline is not None:
            viewer_timeout = min(
                viewer_timeout, max(0.0, deadline - time.monotonic())
            )
        if viewer_timeout <= 0:
            return None
        viewer = github_api._viewer_login_result(
            cwd, timeout_s=viewer_timeout, deadline=deadline
        )
        if viewer.returncode != 0:
            viewer_command = (
                viewer.args
                if isinstance(viewer.args, list)
                else ["gh", "api", "user"]
            )
            github_api._record_list_open_prs_failure(
                viewer_command, viewer, reason="viewer-unavailable"
            )
            return None
        author = viewer.stdout.strip()
        if not author:
            github_api._record_list_open_prs_failure(
                ["gh", "api", "user"],
                subprocess.CompletedProcess(
                    ["gh", "api", "user"], 1, "", "authenticated GitHub login is unavailable"
                ),
                reason="viewer-unavailable",
            )
            return None
    filtered = [
        pr for pr in data
        if isinstance(pr, dict)
        and isinstance(pr.get("author"), dict)
        and pr["author"].get("login") == author
    ]
    github_api._clear_list_open_prs_failure()
    return filtered


def _resolve_open_pr_for_branch(cwd: str, branch: str):
    """(pr_number, is_draft) for this branch's open @me PR, or None if none.

    Served from the shared host-global snapshot when one is fresh (BOU-2810), so
    every session resolving branches reuses ONE `gh pr list` instead of each
    issuing its own GraphQL call. Falls back to the direct probe on a cold
    snapshot, so behaviour is unchanged when nothing has populated it yet.

    Matching on the snapshot is EXACT on `headRefName`. `gh pr list --head` is a
    *prefix* filter — `--head fix` also returns `fix-123` — which this function
    papered over by taking `data[0]`. The exact match is the intent; see the
    `_PR_HEAD_FIELDS` note in github_api about the same hazard.
    """
    from agentic_pr_dash import github_api  # noqa: PLC0415

    snapshot = github_api.peek_pr_snapshot(cwd)
    if snapshot is not None:
        for entry in snapshot:
            if entry.get("headRefName") == branch:
                return int(entry.get("number")), bool(entry.get("isDraft", False))
        return None

    data = _gh_pr_list_json(cwd, ["--head", branch], "number,isDraft")
    if not data:
        return None
    entry = data[0]
    return int(entry.get("number")), bool(entry.get("isDraft", False))


def _list_my_open_prs(cwd: str, timeout: float = 15) -> dict[str, tuple[int, bool]]:
    """Map branch -> (pr_number, is_draft) for the user's open PRs; {} on failure.

    ``timeout`` bounds the underlying gh subprocess (BOU-1787 review).

    Peeks the shared host-global snapshot first (BOU-2810). This is the Stop-hook
    path, so it uses the read-only peek rather than ``list_open_prs_cached``: the
    peek can never issue a fetch, which keeps the caller's remaining-budget
    contract intact. A cold snapshot falls straight through to the original
    timeout-bounded call.
    """
    from agentic_pr_dash import github_api  # noqa: PLC0415

    # Budget check BEFORE the peek. `timeout <= 0` means "no time left" and the
    # contract is that NOTHING shells out — and the peek can shell out, because
    # resolving the snapshot path needs the repo/author from `config.load`, which
    # falls back to `gh repo view` on a cold process. Peeking first therefore
    # spawned a subprocess on an exhausted budget
    # (test_gh_pr_list_skips_subprocess_when_no_budget) and added a GraphQL call
    # to the very path this change exists to make cheaper.
    if timeout <= 0:
        return {}

    data = github_api.peek_pr_snapshot(cwd)
    if data is None:
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


def _unresolved_review_threads(pr_number: int, cwd: str, *, strict: bool = False):
    """Unresolved review threads for a PR — INCLUDING outdated ones, EXCLUDING
    deliberately-deferred ones.

    BOU-2095 (PR #78 review): the completion evidence gate deliberately leaves
    a thread open when GitHub marks it outdated by pure line drift but its
    anchored hunk was never edited. Filtering ``is_outdated`` here made exactly
    those threads invisible to the pending/blocker path — the stop gate idled
    and `complete` closed the bead while intentionally-kept-open feedback sat
    unresolved. Unresolved means unaddressed; drift is not resolution.

    BOU-2567: a thread carrying a persisted deferral record (see
    ``_maintenance.deferred_review``) is a THIRD state, distinct from both
    resolved and unresolved — it is a verified, tracked, deliberate exclusion,
    not silence. Use :func:`_deferred_review_threads` to report those
    separately; they must never simply vanish from every count.
    """
    from agentic_pr_dash import github_api  # noqa: PLC0415
    from . import deferred_review  # noqa: PLC0415

    if strict and github_api.get_review_threads.__module__ == github_api.__name__:
        threads = github_api.get_review_threads(pr_number, cwd, strict=True)
    else:
        # Preserve the long-standing two-argument adapter boundary. A strict
        # native read is fail-closed, while injected adapters define their own
        # explicit observation contract without accepting implementation-only
        # keywords.
        threads = github_api.get_review_threads(pr_number, cwd)
    return [
        t for t in threads
        if not t.is_resolved
        and not deferred_review.is_thread_deferred(cwd, pr_number, t.node_id)
    ]


def _deferred_review_threads(pr_number: int, cwd: str):
    """Unresolved-on-GitHub review threads that carry a deferral record.

    The counterpart to :func:`_unresolved_review_threads`: together the two
    partition a PR's non-resolved threads into "still unaddressed" and
    "deliberately deferred, tracked by a follow-up ticket" — the three-state
    split BOU-2567 requires (a thread is never both, and neither state may be
    inferred from the other's absence).
    """
    from agentic_pr_dash import github_api  # noqa: PLC0415
    from . import deferred_review  # noqa: PLC0415

    threads = github_api.get_review_threads(pr_number, cwd)
    return [
        t for t in threads
        if not t.is_resolved
        and deferred_review.is_thread_deferred(cwd, pr_number, t.node_id)
    ]


def pr_has_unresolved_review_threads(pr_number: int, cwd: str) -> bool:
    """True if the PR has at least one unresolved review thread (outdated
    included, deferred excluded)."""
    return bool(_unresolved_review_threads(pr_number, cwd))


def _pr_open_state(pr_number: int, cwd: str):
    """(state, url, has_failing_ci, failing_checks, review_decision, merge_state, mergeable) for a PR."""
    from agentic_pr_dash import github_api  # noqa: PLC0415
    import json as _json  # noqa: PLC0415

    unavailable = ("unknown", "", False, [], "", "", "", "")
    # Route through github_api._run (not raw subprocess) so a rate-limit on this
    # detached PR-state probe is detected and recorded in the per-tick
    # rate_limit_seen() flag — otherwise a detached-only waiter whose only gh call
    # is this probe can't tell a quota outage from "no PRs" and exits 0 during the
    # outage (BOU-1949, #62 follow-up). _run also gives it the connectivity /
    # rate-limit backoff, and _run_once already catches OSError/TimeoutExpired.
    res = github_api._run(
        [
            "gh", "pr", "view", str(pr_number),
            "--json", "state,url,isDraft,reviewDecision,mergeStateStatus,mergeable,headRefOid",
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
               if c.status == "completed" and c.conclusion not in {"success", "skipped", "neutral"}
               and not github_api._is_infra_check(c.name)]
    review_decision = str(d.get("reviewDecision") or "")
    merge_state = str(d.get("mergeStateStatus") or "")
    mergeable = str(d.get("mergeable") or "")
    head_sha = str(d.get("headRefOid") or "")
    return (
        state, url, bool(failing), failing, review_decision, merge_state,
        mergeable, head_sha,
    )


def _unpack_pr_open_state(raw):
    """Normalize legacy tuples from tests/callers to the current 7-field shape."""
    if len(raw) == 4:
        state, url, has_fail, failing = raw
        return state, url, has_fail, failing, "", "", ""
    if len(raw) == 6:
        state, url, has_fail, failing, review_decision, merge_state = raw
        return state, url, has_fail, failing, review_decision, merge_state, ""
    state, url, has_fail, failing, review_decision, merge_state, mergeable = raw[:7]
    return state, url, has_fail, failing, review_decision, merge_state, mergeable


def _unpack_pr_open_state_with_head(raw):
    """Normalize PR state while preserving head SHA when the probe supplied it."""
    state = _unpack_pr_open_state(raw)
    head_sha = str(raw[7] or "") if len(raw) >= 8 else ""
    return (*state, head_sha)


def _thread_is_p1(thread) -> bool:
    bodies = [thread.top.body] + [r.body for r in getattr(thread, "replies", [])]
    return any("p1" in (b or "").lower() for b in bodies)
