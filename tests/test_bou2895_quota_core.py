"""RED-first coverage for BOU-2895 quota-aware GitHub observation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from agentic_pr_dash import github_api
from agentic_pr_dash.quota import (
    QuotaCaller,
    QuotaContext,
    QuotaDecisionReason,
    QuotaLedger,
    QuotaWorkClass,
    ledger_from_environment,
)


class ManualClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 8, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _graphql_payload(numbers: list[int], *, cost: int = 37) -> dict:
    repo: dict[str, dict] = {}
    for number in numbers:
        repo[f"pr_{number}"] = {
            "headRefOid": f"sha-{number}",
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
            "reviewDecision": "APPROVED",
            "reviewThreads": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [],
            },
            "commits": {
                "nodes": [{
                    "commit": {
                        "oid": f"sha-{number}",
                        "committedDate": "2026-08-08T00:00:00Z",
                        "statusCheckRollup": {
                            "contexts": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [],
                            },
                        },
                    },
                }],
            },
        }
    return {
        "data": {
            "rateLimit": {
                "cost": cost,
                "remaining": 4900,
                "resetAt": "2026-08-08T01:00:00Z",
                "limit": 5000,
            },
            "repository": repo,
        },
    }


def test_quota_ledger_records_rate_limit_costs_and_telemetry() -> None:
    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    ledger = QuotaLedger(
        clock=lambda: now,
        background_hourly_budget=500,
        maintenance_reserve=1000,
    )

    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=125,
        remaining=4_800,
        reset_at="2026-08-08T01:00:00Z",
        limit=5_000,
    )
    ledger.record_graphql(
        caller=QuotaCaller.MAINTENANCE,
        work_class=QuotaWorkClass.MAINTENANCE_GATE,
        cost=75,
        remaining=4_725,
        reset_at="2026-08-08T01:00:00Z",
        limit=5_000,
    )
    ledger.record_cache_hit(QuotaCaller.DASHBOARD, QuotaWorkClass.BACKGROUND_OBSERVATION)
    ledger.record_backoff(timedelta(seconds=12), reason="rate-limit")

    telemetry = ledger.telemetry()
    assert telemetry.latest is not None
    assert telemetry.latest.cost == 75
    assert telemetry.latest.remaining == 4_725
    assert telemetry.latest.limit == 5_000
    assert telemetry.rolling_cost_by_caller[QuotaCaller.DASHBOARD] == 125
    assert telemetry.rolling_cost_by_work_class[QuotaWorkClass.MAINTENANCE_GATE] == 75
    assert telemetry.request_count == 2
    assert telemetry.cache_hit_count == 1
    assert telemetry.cache_hit_rate == pytest.approx(1 / 3)
    assert telemetry.background_hourly_spend == 125
    assert telemetry.backoff_until == now + timedelta(seconds=12)
    assert telemetry.degraded is True


def test_rolling_costs_expire_and_rate_limit_reset_reopens_background_work() -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, background_hourly_budget=100)
    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=100,
        remaining=4_000,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )

    clock.advance(timedelta(hours=1))
    telemetry = ledger.telemetry()

    assert telemetry.rolling_cost_by_caller == {}
    assert telemetry.rolling_cost_by_work_class == {}
    assert telemetry.background_hourly_spend == 0
    assert ledger.allow(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
    ).allowed is True


def test_background_budget_is_separate_from_reserve_and_protected_callers() -> None:
    ledger = QuotaLedger(
        clock=lambda: datetime(2026, 8, 8, tzinfo=timezone.utc),
        background_hourly_budget=100,
        maintenance_reserve=1000,
    )
    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=100,
        remaining=1_500,
        reset_at="2026-08-08T01:00:00Z",
        limit=5_000,
    )

    background = ledger.allow(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
    )
    assert background.allowed is False
    assert background.reason is QuotaDecisionReason.BACKGROUND_HOURLY_BUDGET

    explicit = ledger.allow(
        caller=QuotaCaller.OPERATOR,
        work_class=QuotaWorkClass.EXPLICIT_OPERATOR,
    )
    assert explicit.allowed is True

    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=1,
        remaining=1_000,
        reset_at="2026-08-08T01:00:00Z",
        limit=5_000,
    )
    assert ledger.allow(
        caller=QuotaCaller.OPERATOR,
        work_class=QuotaWorkClass.EXPLICIT_OPERATOR,
    ).allowed is True
    assert ledger.allow(
        caller=QuotaCaller.MAINTENANCE,
        work_class=QuotaWorkClass.MAINTENANCE_GATE,
    ).allowed is True

    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=1,
        remaining=0,
        reset_at="2026-08-08T01:00:00Z",
        limit=5_000,
    )
    assert ledger.allow(
        caller=QuotaCaller.OPERATOR,
        work_class=QuotaWorkClass.EXPLICIT_OPERATOR,
    ).reason is QuotaDecisionReason.NO_REMAINING_QUOTA
    assert ledger.allow(
        caller=QuotaCaller.MAINTENANCE,
        work_class=QuotaWorkClass.MAINTENANCE_GATE,
    ).reason is QuotaDecisionReason.NO_REMAINING_QUOTA
    assert ledger.telemetry().degraded is True


def test_environment_defaults_override_constructor_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APD_GRAPHQL_BACKGROUND_HOURLY_BUDGET", "321")
    monkeypatch.setenv("APD_GRAPHQL_MAINTENANCE_RESERVE", "654")

    ledger = QuotaLedger()

    assert ledger.background_hourly_budget == 321
    assert ledger.maintenance_reserve == 654


def test_production_ledger_factory_accepts_long_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APD_GRAPHQL_BACKGROUND_HOURLY_BUDGET", raising=False)
    monkeypatch.delenv("APD_GRAPHQL_MAINTENANCE_RESERVE", raising=False)
    monkeypatch.setenv(
        "AGENTIC_PR_DASH_GRAPHQL_BACKGROUND_HOURLY_BUDGET", "432"
    )
    monkeypatch.setenv("AGENTIC_PR_DASH_GRAPHQL_MAINTENANCE_RESERVE", "765")

    ledger = ledger_from_environment()

    assert ledger.background_hourly_budget == 432
    assert ledger.maintenance_reserve == 765


def test_failed_observation_stays_degraded_and_retains_reason() -> None:
    ledger = QuotaLedger(
        clock=lambda: datetime(2026, 8, 8, tzinfo=timezone.utc),
    )

    ledger.record_failure(reason="graphql_request_failed")
    telemetry = ledger.telemetry()

    assert telemetry.request_count == 1
    assert telemetry.degraded is True
    assert telemetry.degraded_reason == "graphql_request_failed"
    assert telemetry.backoff_reason == "graphql_request_failed"


def test_failed_request_is_in_cache_hit_rate_denominator() -> None:
    ledger = QuotaLedger(
        clock=lambda: datetime(2026, 8, 8, tzinfo=timezone.utc),
    )
    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=10,
        remaining=4_000,
        reset_at="2026-08-08T01:00:00Z",
        limit=5_000,
    )
    ledger.record_failure(reason="graphql_response_invalid")
    ledger.record_cache_hit(QuotaCaller.DASHBOARD, QuotaWorkClass.BACKGROUND_OBSERVATION)

    telemetry = ledger.telemetry()

    assert telemetry.request_count == 2
    assert telemetry.cache_hit_count == 1
    assert telemetry.cache_hit_rate == pytest.approx(1 / 3)


def test_expired_backoff_clears_only_transient_degraded_state() -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock)

    ledger.record_backoff(timedelta(seconds=30), reason="rate-limit")
    clock.advance(timedelta(seconds=31))

    telemetry = ledger.telemetry()
    assert telemetry.backoff_until is None
    assert telemetry.backoff_reason is None
    assert telemetry.degraded is False
    assert telemetry.degraded_reason is None

    ledger.record_failure(reason="graphql_request_failed")
    clock.advance(timedelta(seconds=31))

    telemetry = ledger.telemetry()
    assert telemetry.degraded is True
    assert telemetry.degraded_reason == "graphql_request_failed"


def test_batch_gate_uses_conservative_explicit_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, maintenance_reserve=1_000)
    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=1,
        remaining=1_010,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )
    calls: list[list[str]] = []

    def fake_run(cmd, cwd=None, timeout_s=30):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(_graphql_payload([1], cost=1)),
            stderr="",
        )

    monkeypatch.setattr(github_api, "_run", fake_run)
    result = github_api.batch_fetch_pr_review_and_ci(
        "acme",
        "widgets",
        [1],
        quota_context=QuotaContext(
            ledger=ledger,
            caller=QuotaCaller.DASHBOARD,
            work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
            estimated_cost=50,
        ),
    )

    assert result == {}
    assert calls == []
    assert ledger.telemetry().last_denial_reason is QuotaDecisionReason.MAINTENANCE_RESERVE


@pytest.mark.parametrize("prior_cost", [0, 1])
def test_batch_shorthand_uses_query_estimate_at_reserve_edge(
    monkeypatch: pytest.MonkeyPatch,
    prior_cost: int,
) -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, maintenance_reserve=1_000)
    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=prior_cost,
        remaining=1_001,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )
    calls: list[list[str]] = []

    def fake_run(cmd, cwd=None, timeout_s=30):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(_graphql_payload([1], cost=37)),
            stderr="",
        )

    monkeypatch.setattr(github_api, "_run", fake_run)
    result = github_api.batch_fetch_pr_review_and_ci(
        "acme",
        "widgets",
        [1],
        quota_ledger=ledger,
    )

    assert result == {}
    assert calls == []
    assert ledger.telemetry().last_denial_reason is QuotaDecisionReason.MAINTENANCE_RESERVE


@pytest.mark.parametrize("reset_at", [123, [], {}])
def test_batch_malformed_reset_at_fails_closed_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    reset_at: object,
) -> None:
    ledger = QuotaLedger()

    def fake_run(cmd, cwd=None, timeout_s=30):
        payload = _graphql_payload([1])
        payload["data"]["rateLimit"]["resetAt"] = reset_at
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(github_api, "_run", fake_run)
    result = github_api.batch_fetch_pr_review_and_ci(
        "acme",
        "widgets",
        [1],
        quota_ledger=ledger,
    )

    assert result == {}
    telemetry = ledger.telemetry()
    assert telemetry.request_count == 1
    assert telemetry.background_hourly_spend == (
        github_api.BATCH_GRAPHQL_ESTIMATED_COST
    )
    assert telemetry.degraded_reason == "graphql_rate_limit_invalid"


def test_batch_records_rate_limit_before_malformed_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = QuotaLedger()

    def fake_run(cmd, cwd=None, timeout_s=30):
        payload = _graphql_payload([1])
        payload["data"]["repository"] = None
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(github_api, "_run", fake_run)
    result = github_api.batch_fetch_pr_review_and_ci(
        "acme",
        "widgets",
        [1],
        quota_ledger=ledger,
    )

    assert result == {}
    telemetry = ledger.telemetry()
    assert telemetry.latest is not None
    assert telemetry.latest.cost == 37
    assert telemetry.request_count == 1
    assert telemetry.degraded_reason == "graphql_repository_invalid"


def test_failed_batch_activates_backoff_and_suppresses_immediate_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = QuotaLedger()
    calls = 0

    def fake_run(cmd, cwd=None, timeout_s=30):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr="GraphQL: API rate limit already exceeded",
        )

    monkeypatch.setattr(github_api, "_run", fake_run)

    first = github_api.batch_fetch_pr_review_and_ci(
        "acme", "widgets", [1], quota_ledger=ledger
    )
    second = github_api.batch_fetch_pr_review_and_ci(
        "acme", "widgets", [1], quota_ledger=ledger
    )

    assert first.denied is True
    assert second.denied is True
    assert calls == 1
    telemetry = ledger.telemetry()
    assert telemetry.backoff_active is True
    assert telemetry.backoff_reason == "graphql_request_failed"


def test_malformed_batch_activates_backoff_and_suppresses_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = QuotaLedger()
    calls = 0

    def fake_run(cmd, cwd=None, timeout_s=30):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(cmd, 0, stdout="{", stderr="")

    monkeypatch.setattr(github_api, "_run", fake_run)

    first = github_api.batch_fetch_pr_review_and_ci(
        "acme", "widgets", [1], quota_ledger=ledger
    )
    second = github_api.batch_fetch_pr_review_and_ci(
        "acme", "widgets", [1], quota_ledger=ledger
    )

    assert first.denied is True
    assert first.error == "graphql_response_invalid"
    assert second.denied is True
    assert calls == 1
    telemetry = ledger.telemetry()
    assert telemetry.backoff_active is True
    assert telemetry.backoff_reason == "graphql_response_invalid"
    assert telemetry.request_count == 1
    assert telemetry.background_hourly_spend == (
        github_api.BATCH_GRAPHQL_ESTIMATED_COST
    )


def test_estimated_success_clears_stale_failure_degradation() -> None:
    ledger = QuotaLedger(maintenance_reserve=0)
    ledger.record_failure(reason="rich_metadata_unavailable")
    assert ledger.telemetry().degraded is True

    ledger.record_estimated(
        QuotaCaller.DASHBOARD,
        QuotaWorkClass.BACKGROUND_OBSERVATION,
        25,
    )

    telemetry = ledger.telemetry()
    assert telemetry.degraded is False
    assert telemetry.degraded_reason is None
    assert telemetry.backoff_reason is None


def test_review_boundary_marks_graphql_spend_before_strict_rest_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_after_graphql(number, latest, cwd=None, *, strict=False):
        raise github_api._ReviewLevelReadError("REST reviews failed")

    monkeypatch.setattr(github_api, "scan_review_threads", fail_after_graphql)

    result = github_api.scan_review_threads_observation(
        7,
        "2026-08-08T00:00:00Z",
        "/repos/widgets",
    )

    assert result.observable is False
    assert result.graphql_observed is True


def test_review_boundary_marks_graphql_spend_for_valid_non_object_rest_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentic_pr_dash._maintenance import deferred_review

    monkeypatch.setattr(
        deferred_review,
        "deferred_threads_for_pr",
        lambda cwd, number: set(),
    )
    monkeypatch.setattr(
        github_api,
        "get_review_threads",
        lambda number, cwd=None, *, strict=False: [],
    )
    monkeypatch.setattr(
        github_api,
        "_run",
        lambda cmd, cwd=None: subprocess.CompletedProcess(
            cmd,
            0,
            stdout="[]\n",
            stderr="",
        ),
    )

    result = github_api.scan_review_threads_observation(
        7,
        "2026-08-08T00:00:00Z",
        "/repos/widgets",
    )

    assert result.observable is False
    assert result.graphql_observed is True


def test_background_budget_degraded_state_survives_protected_work_and_expires() -> None:
    clock = ManualClock()
    ledger = QuotaLedger(
        clock=clock,
        background_hourly_budget=100,
        maintenance_reserve=1_000,
    )
    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=100,
        remaining=4_000,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )

    telemetry = ledger.telemetry()
    assert telemetry.degraded is True
    assert telemetry.degraded_reason is QuotaDecisionReason.BACKGROUND_HOURLY_BUDGET

    assert ledger.allow(
        QuotaCaller.OPERATOR,
        QuotaWorkClass.EXPLICIT_OPERATOR,
    ).allowed is True
    assert ledger.allow(
        QuotaCaller.MAINTENANCE,
        QuotaWorkClass.MAINTENANCE_GATE,
    ).allowed is True
    ledger.record_cache_hit(
        QuotaCaller.OPERATOR,
        QuotaWorkClass.EXPLICIT_OPERATOR,
    )
    telemetry = ledger.telemetry()
    assert telemetry.degraded is True
    assert telemetry.degraded_reason is QuotaDecisionReason.BACKGROUND_HOURLY_BUDGET
    assert ledger.allow(
        QuotaCaller.DASHBOARD,
        QuotaWorkClass.BACKGROUND_OBSERVATION,
    ).reason is QuotaDecisionReason.BACKGROUND_HOURLY_BUDGET

    clock.advance(timedelta(hours=1))
    telemetry = ledger.telemetry()
    assert telemetry.degraded is False
    assert telemetry.degraded_reason is None


def test_concurrent_background_reservations_protect_maintenance_reserve() -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, maintenance_reserve=1_000)
    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=1,
        remaining=1_050,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        reservations = list(
            executor.map(
                lambda _index: ledger.reserve(
                    QuotaCaller.DASHBOARD,
                    QuotaWorkClass.BACKGROUND_OBSERVATION,
                    estimated_cost=50,
                ),
                range(2),
            )
        )

    active = [reservation for reservation in reservations if reservation is not None]
    assert len(active) == 1
    ledger.release(active[0])


def test_concurrent_background_reservations_protect_hourly_budget() -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, background_hourly_budget=50)

    with ThreadPoolExecutor(max_workers=2) as executor:
        reservations = list(
            executor.map(
                lambda _index: ledger.reserve(
                    QuotaCaller.DASHBOARD,
                    QuotaWorkClass.BACKGROUND_OBSERVATION,
                    estimated_cost=50,
                ),
                range(2),
            )
        )

    active = [reservation for reservation in reservations if reservation is not None]
    assert len(active) == 1
    ledger.release(active[0])


def test_reservation_release_clears_derived_maintenance_degradation() -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, maintenance_reserve=1_000)
    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=1,
        remaining=1_050,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )

    reservation = ledger.reserve(
        QuotaCaller.DASHBOARD,
        QuotaWorkClass.BACKGROUND_OBSERVATION,
        estimated_cost=50,
    )
    assert reservation is not None
    assert ledger.telemetry().degraded_reason is QuotaDecisionReason.MAINTENANCE_RESERVE

    assert ledger.release(reservation) is True
    telemetry = ledger.telemetry()
    assert telemetry.degraded is False
    assert telemetry.degraded_reason is None


def test_reservation_release_clears_derived_no_remaining_degradation() -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, maintenance_reserve=0)
    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=1,
        remaining=50,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )

    reservation = ledger.reserve(
        QuotaCaller.DASHBOARD,
        QuotaWorkClass.BACKGROUND_OBSERVATION,
        estimated_cost=50,
    )
    assert reservation is not None
    assert ledger.allow(
        QuotaCaller.OPERATOR,
        QuotaWorkClass.EXPLICIT_OPERATOR,
        estimated_cost=1,
    ).reason is QuotaDecisionReason.NO_REMAINING_QUOTA

    assert ledger.release(reservation) is True
    telemetry = ledger.telemetry()
    assert telemetry.degraded is False
    assert telemetry.degraded_reason is None


def test_failure_then_allow_stays_degraded_until_successful_observation() -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock)
    ledger.record_failure(reason="graphql_request_failed")

    assert ledger.allow(
        QuotaCaller.OPERATOR,
        QuotaWorkClass.EXPLICIT_OPERATOR,
    ).allowed is True
    telemetry = ledger.telemetry()
    assert telemetry.degraded is True
    assert telemetry.degraded_reason == "graphql_request_failed"

    ledger.record_graphql(
        caller=QuotaCaller.OPERATOR,
        work_class=QuotaWorkClass.EXPLICIT_OPERATOR,
        cost=1,
        remaining=4_000,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )
    assert ledger.telemetry().degraded is False


def test_denied_allow_preserves_active_failure_reason() -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock)
    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=1,
        remaining=0,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )
    ledger.record_failure(reason="graphql_request_failed", count_request=False)

    decision = ledger.allow(
        QuotaCaller.OPERATOR,
        QuotaWorkClass.EXPLICIT_OPERATOR,
    )

    assert decision.allowed is False
    assert decision.reason is QuotaDecisionReason.NO_REMAINING_QUOTA
    telemetry = ledger.telemetry()
    assert telemetry.degraded is True
    assert telemetry.degraded_reason == "graphql_request_failed"


def test_insufficient_quota_denial_stays_degraded_until_new_sample() -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, maintenance_reserve=0)
    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=1,
        remaining=20,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )

    decision = ledger.allow(
        QuotaCaller.OPERATOR,
        QuotaWorkClass.EXPLICIT_OPERATOR,
        estimated_cost=50,
    )

    assert decision.reason is QuotaDecisionReason.NO_REMAINING_QUOTA
    telemetry = ledger.telemetry()
    assert telemetry.degraded is True
    assert telemetry.degraded_reason is QuotaDecisionReason.NO_REMAINING_QUOTA

    ledger.record_graphql(
        caller=QuotaCaller.OPERATOR,
        work_class=QuotaWorkClass.EXPLICIT_OPERATOR,
        cost=1,
        remaining=100,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )
    telemetry = ledger.telemetry()
    assert telemetry.degraded is False
    assert telemetry.degraded_reason is None


def test_cheap_success_does_not_clear_inadmissible_denied_estimate() -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, maintenance_reserve=0)
    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=1,
        remaining=20,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )
    assert ledger.allow(
        QuotaCaller.OPERATOR,
        QuotaWorkClass.EXPLICIT_OPERATOR,
        estimated_cost=50,
    ).reason is QuotaDecisionReason.NO_REMAINING_QUOTA

    ledger.record_graphql(
        caller=QuotaCaller.MAINTENANCE,
        work_class=QuotaWorkClass.MAINTENANCE_GATE,
        cost=1,
        remaining=19,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )

    telemetry = ledger.telemetry()
    assert telemetry.degraded is True
    assert telemetry.degraded_reason is QuotaDecisionReason.NO_REMAINING_QUOTA


def test_cache_hit_does_not_clear_inadmissible_denied_estimate() -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, maintenance_reserve=0)
    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=1,
        remaining=20,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )
    assert ledger.allow(
        QuotaCaller.OPERATOR,
        QuotaWorkClass.EXPLICIT_OPERATOR,
        estimated_cost=50,
    ).reason is QuotaDecisionReason.NO_REMAINING_QUOTA

    ledger.record_cache_hit(
        QuotaCaller.MAINTENANCE,
        QuotaWorkClass.MAINTENANCE_GATE,
    )

    telemetry = ledger.telemetry()
    assert telemetry.degraded is True
    assert telemetry.degraded_reason is QuotaDecisionReason.NO_REMAINING_QUOTA


def test_later_smaller_denial_preserves_larger_inadmissible_estimate() -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, maintenance_reserve=0)
    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=1,
        remaining=20,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )

    assert ledger.allow(
        QuotaCaller.OPERATOR,
        QuotaWorkClass.EXPLICIT_OPERATOR,
        estimated_cost=50,
    ).reason is QuotaDecisionReason.NO_REMAINING_QUOTA
    assert ledger.allow(
        QuotaCaller.OPERATOR,
        QuotaWorkClass.EXPLICIT_OPERATOR,
        estimated_cost=30,
    ).reason is QuotaDecisionReason.NO_REMAINING_QUOTA

    ledger.record_graphql(
        caller=QuotaCaller.MAINTENANCE,
        work_class=QuotaWorkClass.MAINTENANCE_GATE,
        cost=1,
        remaining=35,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )

    telemetry = ledger.telemetry()
    assert telemetry.degraded is True
    assert telemetry.degraded_reason is QuotaDecisionReason.NO_REMAINING_QUOTA


def test_maintenance_reserve_denial_stays_degraded_until_admissible() -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, maintenance_reserve=1_000)
    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=1,
        remaining=1_040,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )

    decision = ledger.allow(
        QuotaCaller.DASHBOARD,
        QuotaWorkClass.BACKGROUND_OBSERVATION,
        estimated_cost=50,
    )

    assert decision.reason is QuotaDecisionReason.MAINTENANCE_RESERVE
    telemetry = ledger.telemetry()
    assert telemetry.degraded is True
    assert telemetry.degraded_reason is QuotaDecisionReason.MAINTENANCE_RESERVE


def test_background_budget_denial_stays_degraded_until_admissible() -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock, background_hourly_budget=100)
    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=80,
        remaining=4_000,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )

    decision = ledger.allow(
        QuotaCaller.DASHBOARD,
        QuotaWorkClass.BACKGROUND_OBSERVATION,
        estimated_cost=50,
    )

    assert decision.reason is QuotaDecisionReason.BACKGROUND_HOURLY_BUDGET
    telemetry = ledger.telemetry()
    assert telemetry.degraded is True
    assert telemetry.degraded_reason is QuotaDecisionReason.BACKGROUND_HOURLY_BUDGET


def test_zero_estimated_cost_is_rejected_at_typed_boundaries() -> None:
    ledger = QuotaLedger()

    with pytest.raises(ValueError):
        ledger.allow(
            QuotaCaller.DASHBOARD,
            QuotaWorkClass.BACKGROUND_OBSERVATION,
            estimated_cost=0,
        )
    with pytest.raises(ValueError):
        ledger.reserve(
            QuotaCaller.DASHBOARD,
            QuotaWorkClass.BACKGROUND_OBSERVATION,
            estimated_cost=0,
        )
    with pytest.raises(ValueError):
        QuotaContext(
            ledger=ledger,
            caller=QuotaCaller.DASHBOARD,
            work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
            estimated_cost=0,
        )


def test_batch_releases_reservation_after_failed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock()
    ledger = QuotaLedger(clock=clock)
    responses = [
        subprocess.CompletedProcess(
            [],
            1,
            stdout="",
            stderr="network failure",
        ),
        subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(_graphql_payload([1], cost=1)),
            stderr="",
        ),
    ]

    def fake_run(cmd, cwd=None, timeout_s=30):
        response = responses.pop(0)
        return subprocess.CompletedProcess(
            cmd,
            response.returncode,
            stdout=response.stdout,
            stderr=response.stderr,
        )

    monkeypatch.setattr(github_api, "_run", fake_run)
    assert github_api.batch_fetch_pr_review_and_ci(
        "acme",
        "widgets",
        [1],
        quota_ledger=ledger,
    ) == {}
    assert ledger.reservation_count == 0
    assert ledger.telemetry().backoff_active is True

    clock.advance(timedelta(seconds=31))
    result = github_api.batch_fetch_pr_review_and_ci(
        "acme",
        "widgets",
        [1],
        quota_ledger=ledger,
    )
    assert len(result) == 1
    assert ledger.reservation_count == 0


def test_batch_releases_reservation_when_request_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = QuotaLedger()

    def fake_run(cmd, cwd=None, timeout_s=30):
        raise RuntimeError("unexpected transport exception")

    monkeypatch.setattr(github_api, "_run", fake_run)
    with pytest.raises(RuntimeError, match="unexpected transport exception"):
        github_api.batch_fetch_pr_review_and_ci(
            "acme",
            "widgets",
            [1],
            quota_ledger=ledger,
        )

    assert ledger.reservation_count == 0


@pytest.mark.parametrize("count, expected_chunks", [(1, 1), (15, 1), (50, 4)])
def test_batch_fetch_records_one_quota_sample_per_chunk(
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    expected_chunks: int,
) -> None:
    calls: list[list[str]] = []
    ledger = QuotaLedger()

    def fake_run(cmd, cwd=None, timeout_s=30):
        calls.append(cmd)
        numbers = [
            int(part.removeprefix("pr_"))
            for part in json.loads(json.dumps(cmd)).__str__().split()
            if part.startswith("pr_") and part.removeprefix("pr_").isdigit()
        ]
        # The query command is opaque to the test; derive the expected chunk
        # from the sequential invocation count and return all requested PRs.
        start = (len(calls) - 1) * 15 + 1
        chunk = list(range(start, min(count, start + 14) + 1))
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(_graphql_payload(chunk)),
            stderr="",
        )

    monkeypatch.setattr(github_api, "_run", fake_run)
    result = github_api.batch_fetch_pr_review_and_ci(
        "acme",
        "widgets",
        list(range(1, count + 1)),
        quota_ledger=ledger,
    )

    assert len(calls) == expected_chunks
    assert len(result) == count
    query = next(
        argument.removeprefix("query=")
        for argument in calls[0]
        if argument.startswith("query=")
    )
    assert "rateLimit { cost remaining resetAt limit }" in query
    telemetry = ledger.telemetry()
    assert telemetry.request_count == expected_chunks
    assert telemetry.rolling_cost_by_work_class[QuotaWorkClass.BACKGROUND_OBSERVATION] == expected_chunks * 37


def test_estimated_reads_reduce_sampled_remaining_until_next_snapshot() -> None:
    """GraphQL reads without rateLimit data must still protect the reserve."""

    clock = ManualClock()
    ledger = QuotaLedger(
        clock=clock,
        background_hourly_budget=500,
        maintenance_reserve=1_000,
    )
    ledger.record_graphql(
        caller=QuotaCaller.DASHBOARD,
        work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        cost=1,
        remaining=1_050,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )

    reservation = ledger.reserve(
        QuotaCaller.DASHBOARD,
        QuotaWorkClass.BACKGROUND_OBSERVATION,
        estimated_cost=50,
    )
    assert reservation is not None
    ledger.record_estimated(
        QuotaCaller.DASHBOARD,
        QuotaWorkClass.BACKGROUND_OBSERVATION,
        50,
        reservation=reservation,
    )

    decision = ledger.allow(
        QuotaCaller.DASHBOARD,
        QuotaWorkClass.BACKGROUND_OBSERVATION,
        estimated_cost=1,
    )
    assert decision.reason is QuotaDecisionReason.MAINTENANCE_RESERVE

    ledger.record_graphql(
        caller=QuotaCaller.MAINTENANCE,
        work_class=QuotaWorkClass.MAINTENANCE_GATE,
        cost=1,
        remaining=1_200,
        reset_at=clock.current + timedelta(hours=1),
        limit=5_000,
    )
    assert ledger.allow(
        QuotaCaller.DASHBOARD,
        QuotaWorkClass.BACKGROUND_OBSERVATION,
        estimated_cost=1,
    ).allowed is True
