"""BOU-3095 — the dashboard board does not reflect up-to-date PR state.

Observed 2026-08-22 against a live daemon: PR #3540 merged at 04:47:18Z and was
still rendered as an active "Awaiting Fixes" card reading "Updated 1h 53m ago".
``POST /api/refresh`` corrected it instantly, so the poll loop was alive — the
reconciliation clocks were the problem.

Four defects are pinned here:

* the prune that removes a merged PR is gated on ``metadata_read``, so a
  quota-denied or failed rich GraphQL relist leaves merged PRs on the board;
* on a probe ``200`` the probe body — an authoritative, author-filtered
  open-PR list — is discarded in favour of that quota-gated relist;
* one 15-minute clock gates the cheap conditional REST probe and the expensive
  GraphQL relist alike, and a *failed* attempt stamps the same clock, so a
  failure buys a full interval of no reconciliation;
* review comment counts are only re-planned hourly per immutable head, so a
  comment added or resolved without a push is invisible for up to an hour.

``test_probe_failure_for_one_root_does_not_touch_another_root`` is a regression
guard rather than a RED case: per-root isolation holds today and must survive
moving the prune onto the probe result.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_pr_dash import github_api, orchestrator
from agentic_pr_dash.models import RunnerExecutionSummary
from agentic_pr_dash.observation import ObservationController
from agentic_pr_dash.quota import QuotaLedger


class ManualClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 22, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _raw_pr(
    number: int = 7,
    *,
    head: str = "head-1",
    repo: str = "org/widgets",
    updated_at: str = "2026-08-22T00:00:00Z",
) -> dict:
    """A PR as the rich ``gh pr list`` boundary returns it."""
    return {
        "number": number,
        "title": f"PR {number}",
        "headRefName": f"feature/{number}",
        "headRefOid": head,
        "baseRefName": "main",
        "url": f"https://github.com/{repo}/pull/{number}",
        "isDraft": False,
        "reviewDecision": "",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "labels": [],
        "createdAt": "2026-08-22T00:00:00Z",
        "updatedAt": updated_at,
    }


def _probe_pr(number: int, *, updated_at: str = "2026-08-22T00:00:00Z") -> dict:
    """A PR as ``probe_open_prs_rest`` normalizes it out of the REST body."""
    raw = _raw_pr(number, updated_at=updated_at)
    raw["author"] = {"login": "alice"}
    return raw


@pytest.fixture
def dashboard_boundaries(monkeypatch: pytest.MonkeyPatch):
    """Stub the ``github_api`` read boundary only.

    The orchestrator's reconciliation layer, its prune path and the real
    ObservationController are all exercised — that is where these defects live.
    """
    monkeypatch.setattr(
        orchestrator, "_resolve_maintenance_roots", lambda cwd: ["/repos/widgets"]
    )
    monkeypatch.setattr(
        github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None
    )
    monkeypatch.setattr(
        github_api, "get_repo_info", lambda cwd=None: ("org", "widgets")
    )
    monkeypatch.setattr(
        github_api,
        "batch_fetch_pr_review_and_ci",
        lambda owner, repo, numbers, cwd=None: {},
    )
    monkeypatch.setattr(
        github_api, "get_mergeability", lambda number, cwd=None: ("CLEAN", "MERGEABLE")
    )
    monkeypatch.setattr(
        github_api,
        "get_latest_commit",
        lambda number, cwd=None: ("head-1", "2026-08-22T00:00:00Z"),
    )
    monkeypatch.setattr(github_api, "get_ci_checks", lambda number, cwd=None: [])
    monkeypatch.setattr(
        github_api,
        "get_workflow_queue_health",
        lambda number, cwd=None: ([], [], RunnerExecutionSummary()),
    )
    monkeypatch.setattr(
        github_api, "scan_review_threads", lambda number, latest, cwd=None: ([], [])
    )
    monkeypatch.setattr(
        orchestrator, "find_worktree_for_branch", lambda branch, root=None: None
    )


def _orchestrator(clock: ManualClock, ledger: QuotaLedger) -> orchestrator.Orchestrator:
    return orchestrator.Orchestrator(
        repo_cwd="/repos/widgets",
        observation_controller=ObservationController(clock=clock),
        quota_ledger=ledger,
    )


def _tracked_numbers(orch: orchestrator.Orchestrator) -> set[int]:
    return {number for _repo, number in orch.prs}


# ---------------------------------------------------------------------------
# Case A / B — a merged PR must leave the board on the probe alone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merged_pr_is_pruned_from_the_probe_while_graphql_is_denied(
    monkeypatch: pytest.MonkeyPatch, dashboard_boundaries
) -> None:
    """The reported symptom: a merged PR keeps rendering as open.

    The board is seeded by an operator-class force refresh, then the background
    GraphQL budget is exhausted — exactly the live state observed on
    2026-08-22, where ``/api/quota`` reported
    ``"last_denial_reason": "background_hourly_budget"``. A PR then merges. The
    conditional REST probe returns a 200 whose body no longer contains it, and
    that body is authoritative about which PRs are open: the merged PR must be
    pruned without any GraphQL spend at all.
    """
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, background_hourly_budget=0)
    calls = {"list": 0, "probe": 0}
    open_prs = [_raw_pr(7), _raw_pr(8)]

    def list_open_prs(cwd=None):
        calls["list"] += 1
        return list(open_prs)

    def probe(owner, repo, *, etag=None, last_modified=None, author=None, cwd=None):
        calls["probe"] += 1
        return github_api.ConditionalPRListProbe(
            200, [_probe_pr(raw["number"]) for raw in open_prs], etag='"v2"'
        )

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)
    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)

    orch = _orchestrator(clock, ledger)

    await orch.refresh_prs(force=True)
    assert _tracked_numbers(orch) == {7, 8}
    seeded_list_calls = calls["list"]

    # PR 7 merges: it disappears from the open-PR list GitHub serves.
    open_prs[:] = [_raw_pr(8)]

    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    assert calls["probe"] > 1, (
        "the cheap conditional probe must run on its own fast clock, not inherit "
        "the 15-minute rich-relist interval"
    )
    assert calls["list"] == seeded_list_calls, (
        "pruning must not require the quota-gated GraphQL relist"
    )
    assert _tracked_numbers(orch) == {8}, (
        "the merged PR is still on the board — this is BOU-3095"
    )


@pytest.mark.asyncio
async def test_probe_304_still_confirms_the_cached_open_set(
    monkeypatch: pytest.MonkeyPatch, dashboard_boundaries
) -> None:
    """A 304 authoritatively confirms the cache; nothing may be dropped."""
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, background_hourly_budget=0)
    calls = {"list": 0, "probe": 0}

    def list_open_prs(cwd=None):
        calls["list"] += 1
        return [_raw_pr(7), _raw_pr(8)]

    def probe(owner, repo, *, etag=None, last_modified=None, author=None, cwd=None):
        calls["probe"] += 1
        return github_api.ConditionalPRListProbe(304, [], etag='"v1"')

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)
    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)

    orch = _orchestrator(clock, ledger)
    await orch.refresh_prs(force=True)
    seeded_list_calls = calls["list"]

    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    assert _tracked_numbers(orch) == {7, 8}
    assert calls["list"] == seeded_list_calls


# ---------------------------------------------------------------------------
# Case C — a failed probe must not buy a full reconciliation interval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_probe_retries_on_the_next_validation_window(
    monkeypatch: pytest.MonkeyPatch, dashboard_boundaries
) -> None:
    """A transient probe failure must not suppress reconciliation for 15 minutes.

    Today every attempt — successful or not — stamps ``_metadata_last_attempt``,
    and the due-check keys off ``max(last_success, last_attempt) + interval``.
    A single failure therefore blocks the next attempt for a full interval, and
    ``metadata_read`` is False so nothing was pruned either.
    """
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, background_hourly_budget=0)
    calls = {"probe": 0}
    failing = {"value": True}

    monkeypatch.setattr(
        github_api, "list_open_prs", lambda cwd=None: [_raw_pr(7), _raw_pr(8)]
    )

    def probe(owner, repo, *, etag=None, last_modified=None, author=None, cwd=None):
        calls["probe"] += 1
        if failing["value"]:
            return github_api.ConditionalPRListProbe(
                None, [], etag=etag, error="probe unavailable"
            )
        return github_api.ConditionalPRListProbe(200, [_probe_pr(8)], etag='"v2"')

    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)

    orch = _orchestrator(clock, ledger)
    await orch.refresh_prs(force=True)
    probes_after_seed = calls["probe"]

    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()
    after_first_failure = calls["probe"]
    assert after_first_failure > probes_after_seed, "the probe never ran"

    # A failed probe preserves state — it can never prove a PR was merged.
    assert _tracked_numbers(orch) == {7, 8}

    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()
    assert calls["probe"] > after_first_failure, (
        "a failed probe suppressed the next one for a full metadata interval"
    )

    # Once the probe recovers, the merged PR is pruned from its body.
    failing["value"] = False
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()
    assert _tracked_numbers(orch) == {8}


# ---------------------------------------------------------------------------
# Case D — comment counts must refresh when a PR actually changes
# ---------------------------------------------------------------------------


def test_rest_payload_normalizer_carries_updated_at() -> None:
    """The change signal has to survive the REST->GraphQL field mapping."""
    normalized = github_api._normalize_rest_pr_payload(
        {
            "number": 7,
            "title": "PR 7",
            "user": {"login": "alice"},
            "head": {"sha": "head-1", "ref": "feature/7", "repo": {"owner": {}}},
            "base": {"ref": "main"},
            "html_url": "https://github.com/org/widgets/pull/7",
            "created_at": "2026-08-22T00:00:00Z",
            "updated_at": "2026-08-22T04:45:00Z",
        }
    )

    assert normalized is not None
    assert normalized["updatedAt"] == "2026-08-22T04:45:00Z"
    assert normalized["createdAt"] == "2026-08-22T00:00:00Z"


@pytest.mark.asyncio
async def test_advanced_updated_at_replans_the_review_slice_before_the_hour(
    monkeypatch: pytest.MonkeyPatch, dashboard_boundaries
) -> None:
    """A comment added without a push must not wait out the hourly interval.

    ``review_reconciliation_interval`` is one hour per immutable head, so on an
    unchanged head the REVIEW slice is not due and ``pr.review_comments`` — the
    number the card renders — cannot move. The PR's ``updatedAt`` is the cheap
    change signal the probe already fetches.
    """
    # A real background budget here: this case is about the hourly review
    # interval, not about quota denial, and a zero budget would make the review
    # read inadmissible in every branch and the assertions vacuous.
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, background_hourly_budget=500)
    review_scans: list[int] = []
    updated_at = {"value": "2026-08-22T00:00:00Z"}

    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: [_raw_pr(7, updated_at=updated_at["value"])],
    )
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(
                200, [_probe_pr(7, updated_at=updated_at["value"])], etag='"v2"'
            )
        ),
    )
    monkeypatch.setattr(
        github_api,
        "scan_review_threads_observation",
        lambda number, latest, cwd=None: (
            review_scans.append(number)
            or github_api.ObservationReadResult.observed(([], []))
        ),
    )

    orch = _orchestrator(clock, ledger)
    await orch.refresh_prs(force=True)
    scans_after_seed = len(review_scans)
    assert scans_after_seed > 0, (
        "the seed did not route through scan_review_threads_observation, so the "
        "assertions below would be vacuous"
    )

    # Quiet PR, unchanged head: the hourly floor still applies.
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()
    assert len(review_scans) == scans_after_seed, (
        "a PR with no observed change must not spend a review read every tick"
    )

    # A comment is posted: GitHub advances updatedAt without a new head.
    updated_at["value"] = "2026-08-22T04:45:00Z"
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    assert len(review_scans) > scans_after_seed, (
        "the review slice was not re-planned despite an advanced updatedAt — "
        "the card comment count stays stale for up to an hour"
    )


# ---------------------------------------------------------------------------
# Case E — regression guard: per-root isolation must survive the change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_failure_for_one_root_does_not_touch_another_root(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving the prune onto the probe must not cross repo-root boundaries."""
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, background_hourly_budget=0)
    roots = ["/repos/widgets", "/repos/gadgets"]
    by_root = {
        "/repos/widgets": [_raw_pr(7, repo="org/widgets")],
        "/repos/gadgets": [_raw_pr(9, repo="org/gadgets")],
    }

    monkeypatch.setattr(orchestrator, "_resolve_maintenance_roots", lambda cwd: roots)
    monkeypatch.setattr(
        github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None
    )
    monkeypatch.setattr(
        github_api,
        "get_repo_info",
        lambda cwd=None: ("org", "gadgets" if cwd == "/repos/gadgets" else "widgets"),
    )
    monkeypatch.setattr(
        github_api,
        "batch_fetch_pr_review_and_ci",
        lambda owner, repo, numbers, cwd=None: {},
    )
    monkeypatch.setattr(
        github_api, "get_mergeability", lambda number, cwd=None: ("CLEAN", "MERGEABLE")
    )
    monkeypatch.setattr(
        github_api,
        "get_latest_commit",
        lambda number, cwd=None: ("head-1", "2026-08-22T00:00:00Z"),
    )
    monkeypatch.setattr(github_api, "get_ci_checks", lambda number, cwd=None: [])
    monkeypatch.setattr(
        github_api,
        "get_workflow_queue_health",
        lambda number, cwd=None: ([], [], RunnerExecutionSummary()),
    )
    monkeypatch.setattr(
        github_api, "scan_review_threads", lambda number, latest, cwd=None: ([], [])
    )
    monkeypatch.setattr(
        orchestrator, "find_worktree_for_branch", lambda branch, root=None: None
    )
    monkeypatch.setattr(
        github_api, "list_open_prs", lambda cwd=None: list(by_root.get(cwd, []))
    )

    def probe(owner, repo, *, etag=None, last_modified=None, author=None, cwd=None):
        if cwd == "/repos/widgets":
            return github_api.ConditionalPRListProbe(
                None, [], etag=etag, error="probe unavailable"
            )
        return github_api.ConditionalPRListProbe(
            200, [_probe_pr(9)], etag='"gadgets-v2"'
        )

    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)

    orch = _orchestrator(clock, ledger)
    await orch.refresh_prs(force=True)
    assert _tracked_numbers(orch) == {7, 9}

    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    assert _tracked_numbers(orch) == {7, 9}, (
        "one root's failed probe must never drop another root's PRs"
    )
