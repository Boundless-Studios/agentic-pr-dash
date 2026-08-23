"""BOU-3095 PR #169 review round 11 (codex).

* ``projection_changed = probe.truncated or ...`` means a repository with more
  than 100 open PRs declares EVERY 200 a tracked change, which bypasses the
  round-6 comparison entirely and recreates the quota drain it exists to
  prevent — in exactly the busy repositories where it costs most;
* the round-5 bootstrap mismatch check compares open NUMBERS only, so a comment
  landing between the rich list and the bootstrap probe stores the post-change
  ETag while discarding the probe's ``updatedAt``. Production rich snapshots
  carry no ``updatedAt``, so later 304s never deliver it and the review slice is
  never re-planned.
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


def _raw_pr(number: int = 7, *, updated_at: str | None = "2026-08-22T00:00:00Z") -> dict:
    raw = {
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
    }
    if updated_at is not None:
        raw["updatedAt"] = updated_at
    return raw


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


# ---------------------------------------------------------------------------
# 1 — truncation is uncertainty, not a declared change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_truncated_pages_do_not_relist_every_window(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """A repo with >100 open PRs is permanently truncated.

    Forcing ``projection_changed`` on truncation therefore declared every 200 a
    tracked change and bypassed the comparison entirely — the round-6 quota
    drain, restored in precisely the busy repositories where it costs most.
    """
    clock = ManualClock()
    calls = {"list": 0}

    def list_open_prs(cwd=None):
        calls["list"] += 1
        return [_raw_pr(7)]

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(
                200, [_probe_pr(7)], etag='"churn"', truncated=True
            )
        ),
    )

    orch = _orchestrator(clock)
    await orch.refresh_prs(force=True)
    seeded = calls["list"]

    for _ in range(5):
        clock.advance(timedelta(seconds=90))
        await orch.refresh_prs()
        clock.advance(timedelta(seconds=orchestrator_module.POLL_INTERVAL_SECONDS))
        await orch.refresh_prs()

    assert calls["list"] == seeded, (
        f"a permanently truncated repo triggered {calls['list'] - seeded} rich "
        "relists on an unchanged projection"
    )


@pytest.mark.asyncio
async def test_truncated_page_still_never_counts_as_an_observation(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """Truncation must keep expressing uncertainty where it belongs.

    Not declaring a change must not become "pretend the page was complete": the
    freshness label still has to age, because PRs outside page 1 are unseen.
    """
    clock = ManualClock()
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr(7)])
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(
                200, [_probe_pr(7)], etag='"churn"', truncated=True
            )
        ),
    )

    orch = _orchestrator(clock)
    await orch.refresh_prs(force=True)

    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    assert orch.open_set_freshness().age_seconds(clock.current) == 90.0, (
        "a truncated probe was treated as confirming the open set"
    )


@pytest.mark.asyncio
async def test_a_real_change_behind_a_truncated_page_still_relists(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    clock = ManualClock()
    open_prs = [_raw_pr(7), _raw_pr(8)]
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: list(open_prs))
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(
                200,
                [_probe_pr(p["number"]) for p in open_prs],
                etag='"v2"',
                truncated=True,
            )
        ),
    )

    orch = _orchestrator(clock)
    await orch.refresh_prs(force=True)
    assert {n for _r, n in orch.prs} == {7, 8}

    open_prs[:] = [_raw_pr(7)]
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()
    clock.advance(timedelta(seconds=orchestrator_module.POLL_INTERVAL_SECONDS))
    await orch.refresh_prs()

    assert {n for _r, n in orch.prs} == {7}


# ---------------------------------------------------------------------------
# 2 — activity landing during the bootstrap window must not be lost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_activity_reaches_the_review_replan(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """A comment between the rich list and the bootstrap probe.

    Open numbers match, so the round-5 mismatch check passes and the
    post-change ETag is stored. Production rich snapshots carry no
    ``updatedAt``, so unless the bootstrap body's timestamps are merged, every
    later probe is a 304 and the review slice is never re-planned.
    """
    clock = ManualClock()
    review_scans: list[int] = []

    # Production shape: the rich list has no updatedAt at all.
    monkeypatch.setattr(
        github_api, "list_open_prs", lambda cwd=None: [_raw_pr(7, updated_at=None)]
    )

    def probe(owner, repo, *, etag=None, last_modified=None, author=None, cwd=None):
        if etag is None:
            # Bootstrap: same open set, but a comment has already landed.
            return github_api.ConditionalPRListProbe(
                200,
                [_probe_pr(7, updated_at="2026-08-22T04:45:00Z")],
                etag='"post-comment"',
                truncated=False,
            )
        return github_api.ConditionalPRListProbe(304, [], etag='"post-comment"')

    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)
    monkeypatch.setattr(
        github_api,
        "scan_review_threads_observation",
        lambda number, latest, cwd=None: (
            review_scans.append(number)
            or github_api.ObservationReadResult.observed(([], []))
        ),
    )

    orch = _orchestrator(clock)
    await orch.refresh_prs(force=True)
    scans_after_seed = len(review_scans)
    assert scans_after_seed > 0

    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()
    clock.advance(timedelta(seconds=orchestrator_module.POLL_INTERVAL_SECONDS))
    await orch.refresh_prs()

    assert len(review_scans) > scans_after_seed, (
        "the comment that landed during the bootstrap window was discarded with "
        "the probe body, so the review slice was never re-planned"
    )
