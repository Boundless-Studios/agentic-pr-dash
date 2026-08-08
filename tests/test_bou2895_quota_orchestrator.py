"""Focused quota/conditional metadata coverage for BOU-2895."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import subprocess

import pytest

from agentic_pr_dash import github_api, orchestrator
from agentic_pr_dash.models import CICheck, RunnerExecutionSummary
from agentic_pr_dash.observation import ObservationController
from agentic_pr_dash.quota import (
    QuotaCaller,
    QuotaLedger,
    QuotaWorkClass,
)


class ManualClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 8, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _raw_pr(number: int = 7, *, head: str = "head-1") -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "headRefName": "feature/widgets",
        "headRefOid": head,
        "baseRefName": "main",
        "url": f"https://github.com/org/widgets/pull/{number}",
        "isDraft": False,
        "reviewDecision": "",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "labels": [],
        "createdAt": "2026-08-08T00:00:00Z",
    }


@pytest.fixture
def dashboard_boundaries(monkeypatch: pytest.MonkeyPatch):
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
        lambda number, cwd=None: ("head-1", "2026-08-08T00:00:00Z"),
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


def test_conditional_probe_304_is_typed_and_does_not_need_graphql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        [{"number": 7, "user": {"login": "alice"}, "head": {"sha": "h"}}]
    )
    calls: list[list[str]] = []

    def fake_run(cmd, cwd=None, timeout_s=30):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='HTTP/2 304 Not Modified\r\nETag: "v1"\r\n\r\n',
            stderr="",
        )

    monkeypatch.setattr(github_api, "_run", fake_run)
    result = github_api.probe_open_prs_rest(
        "org", "widgets", etag='"v1"', author="alice"
    )

    assert result.not_modified is True
    assert result.prs == []
    assert result.etag == '"v1"'
    assert len(calls) == 1
    assert "graphql" not in " ".join(calls[0])


def test_primed_batch_without_cwd_is_consumed_without_repo_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    github_api.clear_pr_batch_cache()
    github_api.prime_pr_batch_cache(
        "org/widgets",
        {7: {"threads": []}},
    )
    monkeypatch.setattr(
        github_api,
        "get_repo_info",
        lambda cwd=None: (_ for _ in ()).throw(
            AssertionError("cached batch performed a repository lookup")
        ),
    )

    try:
        assert github_api.get_review_threads(7) == []
    finally:
        github_api.clear_pr_batch_cache()


@pytest.mark.asyncio
async def test_304_reuses_metadata_and_records_cache_hit(
    monkeypatch: pytest.MonkeyPatch, dashboard_boundaries
) -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock)
    calls = {"list": 0, "probe": 0}

    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: calls.__setitem__("list", calls["list"] + 1)
        or [_raw_pr()],
    )

    def probe(*args, **kwargs):
        calls["probe"] += 1
        return github_api.ConditionalPRListProbe(
            304, [], etag='"v1"'
        )

    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)
    orch = _orchestrator(clock, ledger)

    await orch.refresh_prs()
    clock.advance(timedelta(minutes=15))
    await orch.refresh_prs()

    assert calls == {"list": 1, "probe": 2}  # bootstrap + scheduled validator
    assert ledger.telemetry().cache_hit_count == 1


@pytest.mark.asyncio
async def test_event_metadata_invalidation_is_acknowledged_by_304(
    monkeypatch: pytest.MonkeyPatch, dashboard_boundaries
) -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock)
    calls = {"list": 0, "probe": 0}
    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: calls.__setitem__("list", calls["list"] + 1)
        or [_raw_pr()],
    )

    def probe(*args, **kwargs):
        calls["probe"] += 1
        return github_api.ConditionalPRListProbe(304, [], etag='"v1"')

    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)
    orch = _orchestrator(clock, ledger)
    await orch.refresh_prs()

    await orch.handle_github_event(
        "pull_request", "org/widgets", 7, "head-1", action="edited"
    )
    clock.advance(timedelta(seconds=2))
    await orch.refresh_prs()

    assert calls == {"list": 1, "probe": 2}
    assert orch.observation_controller.plan_for(
        "org/widgets", 7, "head-1", now=clock(), ci_pending=False
    ) is None
    assert "/repos/widgets" not in orch._metadata_event_due


@pytest.mark.asyncio
async def test_changed_probe_runs_one_rich_relist(
    monkeypatch: pytest.MonkeyPatch, dashboard_boundaries
) -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock)
    calls = {"list": 0, "probe": 0}
    raw = [_raw_pr()]
    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: calls.__setitem__("list", calls["list"] + 1) or list(raw),
    )

    def probe(*args, **kwargs):
        calls["probe"] += 1
        return github_api.ConditionalPRListProbe(
            200, [], etag=f'"v{calls["probe"]}"'
        )

    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)
    orch = _orchestrator(clock, ledger)
    await orch.refresh_prs()
    clock.advance(timedelta(minutes=15))
    await orch.refresh_prs()

    assert calls == {"list": 2, "probe": 2}


@pytest.mark.asyncio
async def test_probe_failure_retains_cache_for_fifteen_minute_backoff(
    monkeypatch: pytest.MonkeyPatch, dashboard_boundaries
) -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock)
    calls = {"list": 0, "probe": 0}
    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: calls.__setitem__("list", calls["list"] + 1) or [_raw_pr()],
    )

    def probe(*args, **kwargs):
        calls["probe"] += 1
        if calls["probe"] == 1:
            return github_api.ConditionalPRListProbe(200, [], etag='"v1"')
        return github_api.ConditionalPRListProbe(
            None, [], etag='"v2"', error="network down"
        )

    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)
    orch = _orchestrator(clock, ledger)
    await orch.refresh_prs()
    clock.advance(timedelta(minutes=15))
    await orch.refresh_prs()
    clock.advance(timedelta(seconds=15))
    await orch.refresh_prs()

    assert calls == {"list": 1, "probe": 2}
    assert orch.get_pr(7) is not None


@pytest.mark.asyncio
async def test_low_quota_preserves_known_blockers_and_leaves_plan_due(
    monkeypatch: pytest.MonkeyPatch, dashboard_boundaries
) -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, maintenance_reserve=0)
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr()])
    monkeypatch.setattr(
        github_api,
        "get_ci_checks",
        lambda number, cwd=None: [
            CICheck(name="ci", status="completed", conclusion="failure")
        ],
    )
    orch = _orchestrator(clock, ledger)
    await orch.refresh_prs()
    pr = orch.get_pr(7)
    assert pr is not None
    assert pr.failing_checks == ["ci"]

    ledger.record_graphql(
        QuotaCaller.DASHBOARD,
        QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=1,
        remaining=10,
        reset_at=clock.current + timedelta(hours=2),
        limit=5000,
    )
    clock.advance(timedelta(hours=1))
    await orch.refresh_prs()

    pr = orch.get_pr(7)
    assert pr is not None
    assert pr.failing_checks == ["ci"]
    key = orch._observation_keys[("org/widgets", 7)]
    assert orch.observation_controller.plan_for(
        key.repo, key.number, key.head_sha, now=clock.current
    ) is not None


@pytest.mark.asyncio
async def test_explicit_force_uses_operator_class_when_background_budget_is_zero(
    monkeypatch: pytest.MonkeyPatch, dashboard_boundaries
) -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, background_hourly_budget=0)
    calls = {"list": 0}
    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: calls.__setitem__("list", calls["list"] + 1) or [_raw_pr()],
    )
    orch = _orchestrator(clock, ledger)

    await orch.refresh_prs()
    assert calls["list"] == 0
    await orch.refresh_prs(force=True)

    assert calls["list"] == 1
    telemetry = orch.quota_telemetry
    assert telemetry.rolling_cost_by_work_class[QuotaWorkClass.EXPLICIT_OPERATOR] > 0


@pytest.mark.asyncio
async def test_pending_ci_uses_rest_head_path_when_graphql_budget_is_denied(
    monkeypatch: pytest.MonkeyPatch, dashboard_boundaries
) -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, background_hourly_budget=0)
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr()])
    rest_calls: list[tuple[str, int | None]] = []
    graphql_ci_calls: list[int] = []

    async def no_async():
        return None

    monkeypatch.setattr(
        github_api,
        "get_ci_checks_rest_observation",
        lambda head, cwd=None, *, pr_number=None: (
            rest_calls.append((head, pr_number))
            or github_api.ObservationReadResult.observed([])
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_ci_checks_observation",
        lambda number, cwd=None: (
            graphql_ci_calls.append(number)
            or github_api.ObservationReadResult.observed([])
        ),
    )
    orch = _orchestrator(clock, ledger)
    await orch.refresh_prs(force=True)
    await orch.handle_github_event("check_suite", "org/widgets", 7, "head-1")
    clock.advance(timedelta(seconds=2))
    await orch.refresh_prs()

    assert rest_calls[-1] == ("head-1", 7)
    assert graphql_ci_calls == []  # both full fallback and pending CI stay REST-only


@pytest.mark.asyncio
async def test_denied_full_batch_does_not_fall_through_to_graphql_ci(
    monkeypatch: pytest.MonkeyPatch, dashboard_boundaries
) -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, maintenance_reserve=0)
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr()])
    graphql_ci_calls: list[int] = []
    rest_calls: list[str] = []
    monkeypatch.setattr(
        github_api,
        "get_ci_checks_observation",
        lambda number, cwd=None: (
            graphql_ci_calls.append(number)
            or (_ for _ in ()).throw(AssertionError("denied batch used GraphQL CI"))
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_ci_checks_rest_observation",
        lambda head, cwd=None, *, pr_number=None: (
            rest_calls.append(head)
            or github_api.ObservationReadResult.observed([])
        ),
    )
    orch = _orchestrator(clock, ledger)
    await orch.refresh_prs(force=True)
    graphql_ci_calls.clear()
    rest_calls.clear()
    ledger.record_graphql(
        QuotaCaller.DASHBOARD,
        QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=1,
        remaining=10,
        reset_at=clock.current + timedelta(hours=2),
        limit=5000,
    )
    clock.advance(timedelta(hours=1))
    await orch.refresh_prs()

    assert graphql_ci_calls == []
    assert rest_calls == ["head-1"]


@pytest.mark.asyncio
async def test_normal_batch_receives_dashboard_background_attribution(
    monkeypatch: pytest.MonkeyPatch, dashboard_boundaries
) -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock)
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr()])
    seen = []

    def batch(owner, repo, numbers, cwd=None, *, quota_context=None):
        seen.append(quota_context)
        return {}

    monkeypatch.setattr(github_api, "batch_fetch_pr_review_and_ci", batch)
    orch = _orchestrator(clock, ledger)
    await orch.refresh_prs()

    assert seen
    assert seen[0].caller is QuotaCaller.DASHBOARD
    assert seen[0].work_class is QuotaWorkClass.BACKGROUND_OBSERVATION


@pytest.mark.asyncio
async def test_successful_batch_is_applied_after_response_protects_reserve(
    monkeypatch: pytest.MonkeyPatch, dashboard_boundaries
) -> None:
    """A paid batch remains authoritative even when it lowers remaining quota."""

    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, maintenance_reserve=1000)
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr()])
    review_calls: list[int] = []
    monkeypatch.setattr(
        github_api,
        "scan_review_threads_observation",
        lambda number, latest, cwd=None: (
            review_calls.append(number)
            or github_api.ObservationReadResult.observed(([], []))
        ),
    )

    def batch(owner, repo, numbers, cwd=None, *, quota_context=None):
        assert quota_context is not None
        quota_context.ledger.record_graphql(
            quota_context.caller,
            quota_context.work_class,
            cost=37,
            remaining=10,
            reset_at=clock.current + timedelta(hours=2),
            limit=5000,
        )
        return {
            number: {
                "latest_commit": ("head-1", "2026-08-08T00:00:00Z"),
                "ci_checks": [],
                "threads": [],
                "required_pending": False,
                "head_sha": "head-1",
                "merge_state": "CLEAN",
                "mergeable": "MERGEABLE",
                "review_decision": "none",
            }
            for number in numbers
        }

    monkeypatch.setattr(github_api, "batch_fetch_pr_review_and_ci", batch)
    orch = _orchestrator(clock, ledger)
    await orch.refresh_prs()

    pr = orch.get_pr(7)
    assert pr is not None
    assert pr.status.value == "clean"
    assert review_calls == [7]
    assert orch.quota_telemetry.latest is not None
    assert orch.quota_telemetry.latest.remaining == 10


@pytest.mark.asyncio
async def test_admitted_omitted_batch_accounts_review_fallback_and_uses_rest_ci(
    monkeypatch: pytest.MonkeyPatch, dashboard_boundaries
) -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, maintenance_reserve=0)
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr()])
    review_calls: list[int] = []
    rest_calls: list[str] = []
    monkeypatch.setattr(
        github_api,
        "batch_fetch_pr_review_and_ci",
        lambda owner, repo, numbers, cwd=None, *, quota_context=None: {},
    )
    monkeypatch.setattr(
        github_api,
        "scan_review_threads_observation",
        lambda number, latest, cwd=None: (
            review_calls.append(number)
            or github_api.ObservationReadResult.observed(([], []))
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_ci_checks_observation",
        lambda number, cwd=None: (_ for _ in ()).throw(
            AssertionError("omitted batch used unmetered GraphQL CI")
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_ci_checks_rest_observation",
        lambda head, cwd=None, *, pr_number=None: (
            rest_calls.append(head)
            or github_api.ObservationReadResult.observed([])
        ),
    )
    orch = _orchestrator(clock, ledger)
    await orch.refresh_prs()

    assert review_calls == [7]
    assert rest_calls == ["head-1"]
    # Metadata admission plus the explicitly accounted per-PR review fallback.
    assert orch.quota_telemetry.background_hourly_spend >= (
        orchestrator.METADATA_GRAPHQL_ESTIMATED_COST
        + orchestrator.REVIEW_GRAPHQL_ESTIMATED_COST
    )


@pytest.mark.asyncio
async def test_rest_ci_uses_current_plan_head_over_cached_pr_head(
    monkeypatch: pytest.MonkeyPatch, dashboard_boundaries
) -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, maintenance_reserve=0)
    raw_prs = [_raw_pr(head="old-head")]
    latest_commits = iter(
        [
            ("old-head", "2026-08-08T00:00:00Z"),
            ("new-head", "2026-08-08T01:00:00Z"),
        ]
    )
    rest_heads: list[str] = []

    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: list(raw_prs))
    monkeypatch.setattr(
        github_api,
        "get_latest_commit",
        lambda number, cwd=None: next(latest_commits),
    )
    monkeypatch.setattr(
        github_api,
        "get_ci_checks_rest_observation",
        lambda head, cwd=None, *, pr_number=None: (
            rest_heads.append(head)
            or github_api.ObservationReadResult.observed(
                [
                    CICheck(
                        name="ci",
                        status="completed",
                        conclusion="failure" if head == "new-head" else "success",
                    )
                ]
            )
        ),
    )
    orch = _orchestrator(clock, ledger)
    await orch.refresh_prs(force=True)

    raw_prs[:] = [_raw_pr(head="new-head")]
    await orch.refresh_prs(force=True)

    pr = orch.get_pr(7)
    assert pr is not None
    assert rest_heads == ["old-head", "new-head"]
    assert pr.latest_commit_sha == "new-head"
    assert pr.failing_checks == ["ci"]


@pytest.mark.asyncio
async def test_failed_review_fallback_counts_request_without_success_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, maintenance_reserve=0)
    monkeypatch.setattr(
        github_api,
        "scan_review_threads_observation",
        lambda number, latest, cwd=None: (_ for _ in ()).throw(
            RuntimeError("review transport down")
        ),
    )
    orch = _orchestrator(clock, ledger)

    result = await orch._review_fallback_observation(
        7,
        "2026-08-08T00:00:00Z",
        "/repos/widgets",
        force=False,
    )

    assert result.observable is False
    telemetry = ledger.telemetry()
    assert telemetry.request_count == 1
    assert telemetry.background_hourly_spend == 0


@pytest.mark.asyncio
async def test_failed_rich_metadata_counts_request_without_success_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, maintenance_reserve=0)
    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: (_ for _ in ()).throw(RuntimeError("metadata down")),
    )
    orch = _orchestrator(clock, ledger)

    raw_prs, observed = await orch._rich_metadata_list(
        "/repos/widgets",
        clock.current,
        force=False,
    )

    assert raw_prs is None
    assert observed is False
    telemetry = ledger.telemetry()
    assert telemetry.request_count == 1
    assert telemetry.background_hourly_spend == 0
