"""BOU-2895 quota telemetry API and dashboard contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_pr_dash import app
from agentic_pr_dash.quota import (
    QuotaCaller,
    QuotaDecision,
    QuotaDecisionReason,
    QuotaTelemetry,
    QuotaWorkClass,
    RateLimitSnapshot,
)


def _telemetry(*, observed: bool, degraded: bool = False) -> QuotaTelemetry:
    now = datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc)
    latest = None
    if observed:
        latest = RateLimitSnapshot(
            cost=37,
            remaining=4321,
            reset_at=now + timedelta(minutes=42),
            limit=5000,
            observed_at=now,
            caller=QuotaCaller.DASHBOARD,
            work_class=QuotaWorkClass.BACKGROUND_OBSERVATION,
        )
    reason = QuotaDecisionReason.MAINTENANCE_RESERVE if degraded else None
    return QuotaTelemetry(
        latest=latest,
        rolling_cost_by_caller={QuotaCaller.DASHBOARD: 79},
        rolling_cost_by_work_class={QuotaWorkClass.BACKGROUND_OBSERVATION: 79},
        request_count=3,
        cache_hit_count=7,
        cache_hit_rate=0.7,
        background_hourly_spend=79,
        background_hourly_budget=500,
        maintenance_reserve=1000,
        backoff_until=now + timedelta(minutes=5) if degraded else None,
        backoff_reason="quota pressure" if degraded else None,
        degraded=degraded,
        degraded_reason=reason,
        last_decision=QuotaDecision(
            allowed=not degraded,
            reason=reason or QuotaDecisionReason.ALLOWED,
            degraded=degraded,
        ),
        last_denial_reason=reason,
    )


def test_quota_context_exposes_unobserved_state_without_synthesizing_remaining() -> None:
    context = app._quota_context(_telemetry(observed=False))

    assert context["observed"] is False
    assert context["remaining"] is None
    assert context["limit"] is None
    assert context["label"] == "GitHub quota unobserved"
    assert context["background_hourly_budget"] == 500
    assert context["maintenance_reserve"] == 1000


def test_quota_context_serializes_cost_attribution_cache_and_degradation() -> None:
    context = app._quota_context(_telemetry(observed=True, degraded=True))

    assert context["label"] == "GitHub 4,321 / 5,000"
    assert context["latest_cost"] == 37
    assert context["rolling_cost_by_caller"] == {"dashboard": 79}
    assert context["rolling_cost_by_work_class"] == {"background_observation": 79}
    assert context["request_count"] == 3
    assert context["cache_hit_count"] == 7
    assert context["cache_hit_rate"] == 0.7
    assert context["degraded"] is True
    assert context["degraded_reason"] == "maintenance_reserve"
    assert context["backoff_active"] is True


def test_dashboard_polls_a_quota_surface_with_required_labels() -> None:
    template_root = Path(app.BASE_DIR) / "templates"
    dashboard = (template_root / "dashboard.html").read_text()
    partial = (template_root / "partials" / "quota_status.html").read_text()

    assert 'hx-get="/partials/quota"' in dashboard
    for label in (
        "Latest cost",
        "Reset",
        "Rolling caller cost",
        "Rolling class cost",
        "Cache hit rate",
        "Background budget",
        "Maintenance reserve",
    ):
        assert label in partial
    rendered = app.templates.get_template("partials/quota_status.html").render(
        quota=app._quota_context(_telemetry(observed=True, degraded=True))
    )
    assert "GitHub 4,321 / 5,000" in rendered
    assert "Observation degraded: maintenance_reserve" in rendered
    assert any(route.path == "/api/quota" for route in app.app.routes)
