"""BOU-3095 — the board must report how old its GitHub observation is.

When the dashboard fell behind it still rendered as if live: the header "Live"
chip stayed green (it only tracks whether the browser's poll of *localhost*
succeeds, BOU-2193) and the card read "Updated 1h 53m ago" — a timestamp taken
from inside the stale payload, so a two-hour-old card looked plausibly recent.
Nothing on the page reported the age of the observation itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_pr_dash import app, github_api, orchestrator as orchestrator_module
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


def _raw_pr(number: int = 7) -> dict:
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
        "updatedAt": "2026-08-22T00:00:00Z",
    }


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
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr()])


def _orchestrator(clock: ManualClock) -> orchestrator_module.Orchestrator:
    return orchestrator_module.Orchestrator(
        repo_cwd="/repos/widgets",
        observation_controller=ObservationController(clock=clock),
        quota_ledger=QuotaLedger(clock=clock, background_hourly_budget=0),
    )


def _render_header(context: dict) -> str:
    template = app.templates.env.get_template("partials/observation_age.html")
    return template.render(**context)


@pytest.mark.asyncio
async def test_freshness_is_unknown_before_any_observation(boundaries) -> None:
    orch = _orchestrator(ManualClock())

    freshness = orch.open_set_freshness()

    assert freshness.observed_at is None
    assert freshness.complete is False


@pytest.mark.asyncio
async def test_freshness_tracks_the_last_positive_observation(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    clock = ManualClock()
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(304, [], etag='"v1"')
        ),
    )
    orch = _orchestrator(clock)

    await orch.refresh_prs(force=True)
    assert orch.open_set_freshness().age_seconds(clock.current) == 0.0

    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()
    # A cheap 304 is a positive observation of the open set, so the age resets.
    assert orch.open_set_freshness().age_seconds(clock.current) == 0.0

    clock.advance(timedelta(seconds=300))
    assert orch.open_set_freshness().age_seconds(clock.current) == 300.0


@pytest.mark.asyncio
async def test_a_failing_probe_ages_the_observation_and_names_the_reason(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    clock = ManualClock()
    failing = {"value": False}

    def probe(owner, repo, *, etag=None, last_modified=None, author=None, cwd=None):
        if failing["value"]:
            return github_api.ConditionalPRListProbe(
                None, [], etag=etag, error="gh: API rate limit exceeded"
            )
        return github_api.ConditionalPRListProbe(304, [], etag='"v1"')

    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)
    orch = _orchestrator(clock)
    await orch.refresh_prs(force=True)

    failing["value"] = True
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    freshness = orch.open_set_freshness()
    assert freshness.age_seconds(clock.current) == 180.0
    assert "rate limit" in (freshness.degraded_reason or "")


def test_header_reports_the_age_and_flags_it_stale(monkeypatch) -> None:
    now = datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        app.orchestrator, "observation_controller", ObservationController(clock=lambda: now)
    )
    monkeypatch.setattr(
        app.orchestrator,
        "open_set_freshness",
        lambda: orchestrator_module.ObservationFreshness(
            observed_at=now - timedelta(minutes=113),
            degraded_reason="gh: API rate limit exceeded",
            complete=True,
        ),
    )

    context = app._observation_context()
    html = _render_header({"observation": context})

    assert context["stale"] is True
    assert "1h53m" in context["label"]
    assert "observation-age-stale" in html
    assert "rate limit" in html


def test_header_is_not_stale_within_the_validation_windows(monkeypatch) -> None:
    now = datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        app.orchestrator, "observation_controller", ObservationController(clock=lambda: now)
    )
    monkeypatch.setattr(
        app.orchestrator,
        "open_set_freshness",
        lambda: orchestrator_module.ObservationFreshness(
            observed_at=now - timedelta(seconds=45),
            degraded_reason=None,
            complete=True,
        ),
    )

    context = app._observation_context()
    html = _render_header({"observation": context})

    assert context["stale"] is False
    assert context["label"] == "PR data 45s old"
    assert "observation-age-stale" not in html


def test_cold_header_says_loading_not_zero(monkeypatch) -> None:
    now = datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        app.orchestrator, "observation_controller", ObservationController(clock=lambda: now)
    )
    monkeypatch.setattr(
        app.orchestrator,
        "open_set_freshness",
        lambda: orchestrator_module.ObservationFreshness(
            observed_at=None, degraded_reason=None, complete=False
        ),
    )

    context = app._observation_context()

    assert context["known"] is False
    assert context["label"] == "PR data loading"
    assert "0s" not in context["label"]
