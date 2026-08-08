"""Deterministic tests for the BOU-2895 observation controller."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agentic_pr_dash.observation import (
    ObservationController,
    ObservationKey,
    ObservationPlan,
    ObservationReason,
    ObservationSlice,
)


class ManualClock:
    """A UTC clock that advances only when a test asks it to."""

    def __init__(self) -> None:
        self.current = datetime(2026, 8, 8, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _prime(
    controller: ObservationController,
    clock: ManualClock,
    *,
    repo: str = "Acme/Widgets",
    number: int = 7,
    head_sha: str = "head-1",
    ci_pending: bool = False,
) -> None:
    plan = controller.plan_for(
        repo,
        number,
        head_sha,
        now=clock(),
        ci_pending=ci_pending,
    )
    assert plan is not None
    controller.record_refresh(plan, now=clock(), ci_pending=ci_pending)


def test_first_observation_requests_review_and_ci() -> None:
    clock = ManualClock()
    controller = ObservationController(clock=clock)

    plan = controller.plan_for("acme/widgets", 7, "head-1", now=clock())

    assert plan is not None
    assert {ObservationSlice.REVIEW, ObservationSlice.CI} <= plan.slices
    assert plan.reason is ObservationReason.INITIAL


def test_unchanged_terminal_pr_is_not_due_before_reconciliation() -> None:
    clock = ManualClock()
    controller = ObservationController(clock=clock)
    _prime(controller, clock)

    assert (
        controller.plan_for("ACME/WIDGETS", 7, "head-1", now=clock()) is None
    )


def test_new_head_requests_review_and_ci() -> None:
    clock = ManualClock()
    controller = ObservationController(clock=clock)
    _prime(controller, clock)

    plan = controller.plan_for("acme/widgets", 7, "head-2", now=clock())

    assert plan is not None
    assert {ObservationSlice.REVIEW, ObservationSlice.CI} <= plan.slices
    assert plan.reason is ObservationReason.INITIAL


def test_event_before_first_observation_still_requests_all_slices() -> None:
    clock = ManualClock()
    controller = ObservationController(clock=clock)

    controller.handle_event(
        "pull_request_review",
        "acme/widgets",
        7,
        "head-1",
        now=clock(),
    )
    clock.advance(timedelta(seconds=2))

    plan = controller.plan_for("acme/widgets", 7, "head-1", now=clock())

    assert plan is not None
    assert plan.slices == frozenset(
        {
            ObservationSlice.METADATA,
            ObservationSlice.REVIEW,
            ObservationSlice.CI,
        }
    )
    assert plan.reason is ObservationReason.EVENT


def test_pending_ci_is_due_every_poll_interval_until_terminal() -> None:
    clock = ManualClock()
    controller = ObservationController(clock=clock)
    _prime(controller, clock, ci_pending=True)

    clock.advance(timedelta(seconds=29))
    assert (
        controller.plan_for(
            "acme/widgets", 7, "head-1", now=clock(), ci_pending=True
        )
        is None
    )

    clock.advance(timedelta(seconds=1))
    plan = controller.plan_for(
        "acme/widgets", 7, "head-1", now=clock(), ci_pending=True
    )
    assert plan is not None
    assert plan.slices == frozenset({ObservationSlice.CI})
    assert plan.reason is ObservationReason.CI_POLL
    controller.record_refresh(plan, now=clock(), ci_pending=True)

    clock.advance(timedelta(seconds=30))
    next_plan = controller.plan_for(
        "acme/widgets", 7, "head-1", now=clock(), ci_pending=True
    )
    assert next_plan is not None
    assert next_plan.slices == frozenset({ObservationSlice.CI})

    controller.record_refresh(next_plan, now=clock(), ci_pending=False)
    clock.advance(timedelta(seconds=30))
    assert (
        controller.plan_for(
            "acme/widgets", 7, "head-1", now=clock(), ci_pending=False
        )
        is None
    )


def test_hourly_review_reconciliation_outranks_pending_ci_poll() -> None:
    """A stuck pending job must not starve the hourly review repair."""

    clock = ManualClock()
    controller = ObservationController(clock=clock)
    _prime(controller, clock, ci_pending=True)

    clock.advance(timedelta(hours=1))
    plan = controller.plan_for(
        "acme/widgets", 7, "head-1", now=clock(), ci_pending=True
    )

    assert plan is not None
    assert plan.reason is ObservationReason.REVIEW_RECONCILIATION
    assert plan.slices == frozenset(
        {ObservationSlice.REVIEW, ObservationSlice.CI}
    )


def test_review_event_does_not_starve_terminal_ci_reconciliation() -> None:
    """Review freshness must not reset the independent CI deadline."""

    clock = ManualClock()
    controller = ObservationController(clock=clock)
    _prime(controller, clock, ci_pending=False)

    clock.advance(timedelta(minutes=30))
    controller.handle_event(
        "pull_request_review", "acme/widgets", 7, "head-1", now=clock()
    )
    clock.advance(timedelta(seconds=2))
    review_plan = controller.plan_for(
        "acme/widgets", 7, "head-1", now=clock(), ci_pending=False
    )
    assert review_plan is not None
    assert review_plan.slices == frozenset({ObservationSlice.REVIEW})
    controller.record_refresh(review_plan, now=clock(), ci_pending=False)

    clock.advance(timedelta(minutes=29, seconds=58))
    ci_plan = controller.plan_for(
        "acme/widgets", 7, "head-1", now=clock(), ci_pending=False
    )

    assert ci_plan is not None
    assert ci_plan.reason is ObservationReason.REVIEW_RECONCILIATION
    assert ObservationSlice.CI in ci_plan.slices


def test_partial_review_ack_retries_never_observed_ci_immediately() -> None:
    """Each missing initial slice remains due after its sibling succeeds."""

    clock = ManualClock()
    controller = ObservationController(clock=clock)
    initial = controller.plan_for(
        "acme/widgets", 7, "head-1", now=clock(), ci_pending=False
    )
    assert initial is not None
    acknowledged = frozenset(
        {ObservationSlice.METADATA, ObservationSlice.REVIEW}
    )
    controller.record_refresh(
        ObservationPlan(
            initial.key,
            acknowledged,
            initial.reason,
            frozenset(
                (observation_slice, generation)
                for observation_slice, generation in (
                    initial.invalidation_generations
                )
                if observation_slice in acknowledged
            ),
        ),
        now=clock(),
        ci_pending=False,
    )

    retry = controller.plan_for(
        "acme/widgets", 7, "head-1", now=clock(), ci_pending=False
    )
    assert retry is not None
    assert retry.slices == frozenset({ObservationSlice.CI})


@pytest.mark.parametrize(
    ("event_name", "expected_slice"),
    [
        ("pull_request_review", ObservationSlice.REVIEW),
        ("pull_request_review_comment", ObservationSlice.REVIEW),
        ("pull_request_review_thread", ObservationSlice.REVIEW),
        ("check_run", ObservationSlice.CI),
        ("check_suite", ObservationSlice.CI),
    ],
)
def test_event_family_invalidates_only_its_slice(
    event_name: str, expected_slice: ObservationSlice
) -> None:
    clock = ManualClock()
    controller = ObservationController(clock=clock)
    _prime(controller, clock)

    controller.handle_event(
        event_name,
        "acme/widgets",
        7,
        "head-1",
        now=clock(),
    )
    clock.advance(timedelta(seconds=2))

    plan = controller.plan_for("acme/widgets", 7, "head-1", now=clock())
    assert plan is not None
    assert plan.slices == frozenset({expected_slice})
    assert plan.reason is ObservationReason.EVENT


def test_new_same_slice_event_survives_old_plan_acknowledgement() -> None:
    clock = ManualClock()
    controller = ObservationController(clock=clock)
    _prime(controller, clock)

    controller.handle_event(
        "pull_request_review",
        "acme/widgets",
        7,
        "head-1",
        now=clock(),
    )
    clock.advance(timedelta(seconds=2))
    old_plan = controller.plan_for("acme/widgets", 7, "head-1", now=clock())
    assert old_plan is not None

    controller.handle_event(
        "pull_request_review",
        "acme/widgets",
        7,
        "head-1",
        now=clock(),
    )
    controller.record_refresh(old_plan, now=clock(), ci_pending=False)

    assert controller.plan_for("acme/widgets", 7, "head-1", now=clock()) is None
    clock.advance(timedelta(seconds=2))
    newer_plan = controller.plan_for("acme/widgets", 7, "head-1", now=clock())

    assert newer_plan is not None
    assert newer_plan.slices == frozenset({ObservationSlice.REVIEW})
    assert newer_plan.reason is ObservationReason.EVENT


def test_head_changing_pull_request_event_invalidates_all_slices() -> None:
    clock = ManualClock()
    controller = ObservationController(clock=clock)
    _prime(controller, clock)

    controller.handle_event(
        "pull_request",
        "acme/widgets",
        7,
        "head-2",
        action="synchronize",
        now=clock(),
    )
    clock.advance(timedelta(seconds=2))

    plan = controller.plan_for("acme/widgets", 7, "head-2", now=clock())
    assert plan is not None
    assert plan.slices == frozenset(
        {
            ObservationSlice.METADATA,
            ObservationSlice.REVIEW,
            ObservationSlice.CI,
        }
    )
    assert plan.reason is ObservationReason.HEAD_CHANGED


@pytest.mark.parametrize(
    "action",
    ["edited", "converted_to_draft", "ready_for_review"],
)
def test_non_head_changing_pull_request_action_invalidates_metadata(
    action: str,
) -> None:
    clock = ManualClock()
    controller = ObservationController(clock=clock)
    _prime(controller, clock)

    controller.handle_event(
        "pull_request",
        "acme/widgets",
        7,
        "head-1",
        action=action,
        now=clock(),
    )
    clock.advance(timedelta(seconds=2))

    plan = controller.plan_for("acme/widgets", 7, "head-1", now=clock())

    assert plan is not None
    assert plan.slices == frozenset({ObservationSlice.METADATA})
    assert plan.reason is ObservationReason.EVENT


def test_unknown_event_does_not_invalidate_an_observation() -> None:
    clock = ManualClock()
    controller = ObservationController(clock=clock)
    _prime(controller, clock)

    controller.handle_event(
        "workflow_dispatch",
        "acme/widgets",
        7,
        "head-1",
        now=clock(),
    )

    assert controller.plan_for("acme/widgets", 7, "head-1", now=clock()) is None


def test_invalidations_inside_debounce_window_coalesce() -> None:
    clock = ManualClock()
    controller = ObservationController(clock=clock)
    _prime(controller, clock)

    controller.handle_event(
        "pull_request_review",
        "acme/widgets",
        7,
        "head-1",
        now=clock(),
    )
    clock.advance(timedelta(seconds=1))
    controller.handle_event(
        "check_suite",
        "acme/widgets",
        7,
        "head-1",
        now=clock(),
    )

    clock.advance(timedelta(seconds=1, milliseconds=999))
    assert controller.plan_for("acme/widgets", 7, "head-1", now=clock()) is None
    clock.advance(timedelta(milliseconds=1))
    plan = controller.plan_for("acme/widgets", 7, "head-1", now=clock())

    assert plan is not None
    assert plan.slices == frozenset({ObservationSlice.REVIEW, ObservationSlice.CI})


def test_reconciliation_due_times_and_full_review_self_heal() -> None:
    clock = ManualClock()
    controller = ObservationController(clock=clock)
    _prime(controller, clock)

    clock.advance(timedelta(minutes=15))
    metadata_plan = controller.plan_for(
        "acme/widgets", 7, "head-1", now=clock()
    )
    assert metadata_plan is not None
    assert metadata_plan.slices == frozenset({ObservationSlice.METADATA})
    assert metadata_plan.reason is ObservationReason.METADATA_RECONCILIATION
    controller.record_refresh(metadata_plan, now=clock(), ci_pending=False)

    clock.advance(timedelta(minutes=45))
    full_review_plan = controller.plan_for(
        "acme/widgets", 7, "head-1", now=clock()
    )
    assert full_review_plan is not None
    assert full_review_plan.slices == frozenset(
        {ObservationSlice.REVIEW, ObservationSlice.CI}
    )
    assert full_review_plan.reason is ObservationReason.REVIEW_RECONCILIATION


def test_force_requests_all_slices_immediately() -> None:
    clock = ManualClock()
    controller = ObservationController(clock=clock)
    _prime(controller, clock)
    controller.handle_event(
        "check_suite", "acme/widgets", 7, "head-1", now=clock()
    )

    plan = controller.plan_for(
        "acme/widgets", 7, "head-1", now=clock(), force=True
    )

    assert plan is not None
    assert plan.slices == frozenset(
        {
            ObservationSlice.METADATA,
            ObservationSlice.REVIEW,
            ObservationSlice.CI,
        }
    )
    assert plan.reason is ObservationReason.FORCED


def test_prune_removes_closed_and_missing_keys() -> None:
    clock = ManualClock()
    controller = ObservationController(clock=clock)
    _prime(controller, clock, number=7)
    _prime(controller, clock, number=8)

    controller.prune(
        {
            ObservationKey("acme/widgets", 7, "head-1"),
        }
    )

    assert controller.tracked_keys() == frozenset(
        {ObservationKey("ACME/WIDGETS", 7, "head-1")}
    )
