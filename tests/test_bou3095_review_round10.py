"""BOU-3095 PR #169 review round 10 (codex).

When unrelated-author activity moves the repo-wide ETag but the filtered
tracked-author projection is identical, the cheap path returned without storing
the new validator. Every later probe then sent the obsolete one and downloaded
another full 200 body instead of settling back to a 304 — once per validation
window, until a rich reconciliation happened to refresh it.

Adopting the validator is safe precisely BECAUSE the projection was confirmed
unchanged: the tracked PRs are identical, so the ETag is not being accepted
against an un-ingested list. It is not adopted when the projection changed
(a later 304 would then hide the pending change) or when the page was truncated
(nothing was confirmed).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_pr_dash import github_api, orchestrator as orchestrator_module
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


def _raw_pr(number: int = 7, *, updated_at: str = "2026-08-22T00:00:00Z") -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "headRefName": f"feature/{number}",
        "headRefOid": "head-1",
        "baseRefName": "main",
        "url": f"https://github.com/org/widgets/pull/{number}",
        "isDraft": False,
        "reviewDecision": "",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "labels": [],
        "createdAt": "2026-08-22T00:00:00Z",
        "updatedAt": updated_at,
    }


def _probe_pr(number: int, *, updated_at: str = "2026-08-22T00:00:00Z") -> dict:
    raw = _raw_pr(number, updated_at=updated_at)
    raw["author"] = {"login": "alice"}
    return raw


@pytest.fixture
def boundaries(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        orchestrator_module, "_resolve_maintenance_roots", lambda cwd: ["/repos/widgets"]
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
        orchestrator_module, "find_worktree_for_branch", lambda branch, root=None: None
    )


def _orchestrator(clock: ManualClock, *, budget: int = 500):
    return orchestrator_module.Orchestrator(
        repo_cwd="/repos/widgets",
        observation_controller=ObservationController(clock=clock),
        quota_ledger=QuotaLedger(clock=clock, background_hourly_budget=budget),
    )


@pytest.mark.asyncio
async def test_unchanged_projection_adopts_the_new_validator(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """Otherwise every window re-downloads a full body instead of a 304."""
    clock = ManualClock()
    sent_etags: list[str | None] = []
    served: list[str] = []
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr(7)])

    def probe(owner, repo, *, etag=None, last_modified=None, author=None, cwd=None):
        sent_etags.append(etag)
        # Another author keeps moving the page; our projection never changes.
        # A NEW etag each call, so "did we adopt it?" is actually observable.
        fresh = f'"churn-{len(served)}"'
        served.append(fresh)
        return github_api.ConditionalPRListProbe(
            200, [_probe_pr(7)], etag=fresh, truncated=False
        )

    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)

    orch = _orchestrator(clock)
    await orch.refresh_prs(force=True)

    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    assert sent_etags[-1] == served[-2], (
        "the confirmed-unchanged 200's validator was discarded, so every "
        f"window re-sends a stale one and downloads a full body: sent={sent_etags} "
        f"served={served}"
    )


@pytest.mark.asyncio
async def test_changed_projection_does_not_adopt_the_validator(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """A pending, unreconciled change must not be hidden behind a later 304."""
    clock = ManualClock()
    sent_etags: list[str | None] = []
    served: list[str] = []
    open_prs = [_raw_pr(7), _raw_pr(8)]
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: list(open_prs))

    def probe(owner, repo, *, etag=None, last_modified=None, author=None, cwd=None):
        sent_etags.append(etag)
        fresh = f'"changed-{len(sent_etags)}"'
        served.append(fresh)
        return github_api.ConditionalPRListProbe(
            200,
            [_probe_pr(p["number"]) for p in open_prs],
            etag=fresh,
            truncated=False,
        )

    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)

    orch = _orchestrator(clock, budget=0)  # relist denied: change stays pending
    await orch.refresh_prs(force=True)

    open_prs[:] = [_raw_pr(7)]
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    assert sent_etags[-1] != served[-2], (
        "an unreconciled change adopted its validator, so a later 304 would "
        f"confirm a board that is still out of date: sent={sent_etags} served={served}"
    )


@pytest.mark.asyncio
async def test_truncated_unchanged_projection_does_not_adopt_the_validator(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """A truncated page confirms nothing, so its validator is not adopted."""
    clock = ManualClock()
    sent_etags: list[str | None] = []
    served: list[str] = []
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr(7)])

    def probe(owner, repo, *, etag=None, last_modified=None, author=None, cwd=None):
        sent_etags.append(etag)
        fresh = f'"truncated-{len(sent_etags)}"'
        served.append(fresh)
        return github_api.ConditionalPRListProbe(
            200, [_probe_pr(7)], etag=fresh, truncated=True
        )

    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)

    orch = _orchestrator(clock, budget=0)
    await orch.refresh_prs(force=True)

    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    assert sent_etags[-1] != served[-2]
