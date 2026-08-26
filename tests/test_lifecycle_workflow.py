from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from agent_review_coordinator import (
    Finding,
    ReviewLedger,
    ReviewPolicy,
    ReviewResult,
    ReviewStage,
    Severity,
)

from agentic_pr_dash import github_api
from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment
from agentic_pr_dash.lifecycle_models import (
    IntentLifecycleStateV1,
    MaintenanceBlockerV1,
    MaintenanceIntentV1,
    MaintenanceKeyV1,
    MaintenanceNextActionV1,
    MaintenanceTargetV1,
    MergeabilityStateV1,
    RequiredCIStateV1,
    SnapshotReadStatusV1,
)
from agentic_pr_dash.lifecycle_store import LifecycleStore
from agentic_pr_dash.models import CICheck, PRData

OBSERVED_AT = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)
HEAD = "a" * 40
REPOSITORY = "Acme/Widget"


def _intent(
    *, pr_number: int | None = None, head_sha: str = HEAD
) -> MaintenanceIntentV1:
    return MaintenanceIntentV1(
        repository=REPOSITORY,
        pushed_ref="refs/heads/feature/thing",
        head_sha=head_sha,
        workflow_type="pr-maintenance",
        reason="post-push maintenance",
        worktree_path="/tmp/worktree",
        session_id="session-1",
        requested_at=OBSERVED_AT,
        pr_number=pr_number,
    )


def _policy() -> ReviewPolicy:
    return ReviewPolicy.model_validate(
        {
            "version": 1,
            "review": {
                "local": {"reviewer_count": 1},
                "backstop": {"reviewer_count": 1, "trigger": "new_head_sha"},
            },
        }
    )


def _ledger() -> ReviewLedger:
    ledger = ReviewLedger(
        repository=REPOSITORY,
        head_sha=HEAD,
        delivery_id="delivery-1",
        review_charter_version="review-charter-v1",
    )
    for stage in (ReviewStage.LOCAL, ReviewStage.BACKSTOP):
        ledger.submit(
            ReviewResult(
                repository=REPOSITORY,
                head_sha=HEAD,
                stage=stage,
                round_number=1,
                slot_number=1,
                reviewer_execution_id=f"{stage.value}-review",
            )
        )
    return ledger


def _ledger_with_unresolved_finding() -> ReviewLedger:
    ledger = _ledger()
    ledger.submit(
        ReviewResult(
            repository=REPOSITORY,
            head_sha=HEAD,
            stage=ReviewStage.LOCAL,
            round_number=2,
            slot_number=1,
            reviewer_execution_id="local-finding-review",
            findings=[
                Finding(
                    repository=REPOSITORY,
                    head_sha=HEAD,
                    reviewer_execution_id="local-finding-review",
                    severity=Severity.P1,
                    title="Unresolved local finding",
                    explanation="The lifecycle workflow must retain this finding.",
                    path="src/example.py",
                    invariant="local finding invariant",
                )
            ],
        )
    )
    return ledger


def _pr_payload() -> dict[str, object]:
    return {
        "number": 7,
        "title": "Feature thing",
        "headRefName": "feature/thing",
        "headRefOid": HEAD,
        "baseRefName": "main",
        "url": "https://github.com/Acme/Widget/pull/7",
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED",
    }


def _batch(
    *,
    ci_checks: tuple[CICheck, ...] | None = None,
    required_pending: bool = False,
    unresolved_threads: tuple[ReviewThread, ...] = (),
    merge_state: str = "CLEAN",
    mergeable: str = "MERGEABLE",
    review_decision: str = "APPROVED",
) -> github_api.PrMaintenanceSnapshotBatch:
    observed = github_api.PrMaintenanceSnapshot(
        pr_number=7,
        head_sha=HEAD,
        head_committed_at=OBSERVED_AT.isoformat(),
        ci_checks=ci_checks
        or (CICheck(name="tests", status="completed", conclusion="success"),),
        required_pending=required_pending,
        unresolved_threads=unresolved_threads,
        merge_state=merge_state,
        mergeable=mergeable,
        review_decision=review_decision,
    )
    return github_api.PrMaintenanceSnapshotBatch((7,), {7: observed}, ())


@pytest.mark.asyncio
async def test_push_before_pr_no_pr_then_reactivation_promotes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    no_pr_intent = _intent()
    store.enqueue(no_pr_intent)
    monkeypatch.setattr(
        github_api,
        "find_pr_by_head",
        lambda branch, state="open", cwd=None: None,
    )

    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(store, policy=_policy(), ledger=_ledger())
    await workflow.drain()
    assert store.list_intents()[0].state is IntentLifecycleStateV1.NO_PR

    reactivated = no_pr_intent.model_copy(
        update={"pr_number": 7, "reason": "PR was created"}
    )
    store.enqueue(reactivated)
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False: _pr_payload(),
    )
    monkeypatch.setattr(
        github_api, "collect_pr_maintenance_snapshots", lambda *args, **kwargs: _batch()
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )

    await workflow.drain()

    record = store.list_intents()[0]
    assert record.state is IntentLifecycleStateV1.PROMOTED
    assert record.canonical_key is not None
    assert record.canonical_key.pr_number == 7
    result = store.read_snapshot(
        MaintenanceTargetV1.exact(record.canonical_key), now=OBSERVED_AT
    )
    assert result.status is SnapshotReadStatusV1.FRESH


@pytest.mark.asyncio
async def test_pending_ci_is_persisted_without_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=7)
    store.enqueue(intent)
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False: _pr_payload(),
    )
    monkeypatch.setattr(
        github_api,
        "collect_pr_maintenance_snapshots",
        lambda *args, **kwargs: _batch(
            ci_checks=(CICheck(name="tests", status="in_progress"),),
            required_pending=True,
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )

    class DispatchProbe:
        calls = 0

        async def dispatch_pr_maintenance(self, pr: object) -> None:
            self.calls += 1

    dispatch = DispatchProbe()
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=_ledger(),
        orchestrator=dispatch,
    )
    await workflow.drain()

    record = store.list_intents()[0]
    assert record.canonical_key is not None
    snapshot = store.read_snapshot(
        MaintenanceTargetV1.exact(record.canonical_key), now=OBSERVED_AT
    ).snapshot
    assert snapshot is not None
    assert snapshot.required_ci_state is RequiredCIStateV1.PENDING
    assert snapshot.blockers == (MaintenanceBlockerV1.REQUIRED_CI_PENDING,)
    assert snapshot.next_actions == (MaintenanceNextActionV1.WAIT_FOR_CI,)
    assert dispatch.calls == 0


@pytest.mark.asyncio
async def test_pending_ci_is_derived_from_check_status_when_flag_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=7)
    store.enqueue(intent)
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False: _pr_payload(),
    )
    monkeypatch.setattr(
        github_api,
        "collect_pr_maintenance_snapshots",
        lambda *args, **kwargs: _batch(
            ci_checks=(CICheck(name="tests", status="in_progress"),),
            required_pending=False,
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(store, policy=_policy(), ledger=_ledger())
    await workflow.drain()

    record = store.list_intents()[0]
    assert record.canonical_key is not None
    snapshot = store.read_snapshot(
        MaintenanceTargetV1.exact(record.canonical_key), now=OBSERVED_AT
    ).snapshot
    assert snapshot is not None
    assert snapshot.required_ci_state is RequiredCIStateV1.PENDING
    assert snapshot.next_actions == (MaintenanceNextActionV1.WAIT_FOR_CI,)


@pytest.mark.asyncio
async def test_unknown_mergeability_is_unavailable_not_actionable_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    store.enqueue(_intent(pr_number=7))
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False: _pr_payload(),
    )
    monkeypatch.setattr(
        github_api,
        "collect_pr_maintenance_snapshots",
        lambda *args, **kwargs: _batch(merge_state="UNKNOWN", mergeable="UNKNOWN"),
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )

    class DispatchProbe:
        calls = 0

        async def dispatch_pr_maintenance(self, pr: PRData) -> None:
            self.calls += 1

    dispatch = DispatchProbe()
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=_ledger(),
        orchestrator=dispatch,
    )
    await workflow.drain()

    snapshot = store.read_snapshot(
        MaintenanceTargetV1.exact(
            MaintenanceKeyV1(
                repository=REPOSITORY,
                pr_number=7,
                head_sha=HEAD,
                workflow_type="pr-maintenance",
            )
        ),
        now=OBSERVED_AT,
    ).snapshot
    assert snapshot is not None
    assert snapshot.mergeability is MergeabilityStateV1.UNKNOWN
    assert snapshot.blockers == (MaintenanceBlockerV1.OBSERVATION_UNAVAILABLE,)
    assert dispatch.calls == 0


@pytest.mark.asyncio
async def test_actionable_ci_failure_uses_existing_dispatch_seam_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    store.enqueue(_intent(pr_number=7))
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False: _pr_payload(),
    )
    monkeypatch.setattr(
        github_api,
        "collect_pr_maintenance_snapshots",
        lambda *args, **kwargs: _batch(
            ci_checks=(CICheck(name="tests", status="completed", conclusion="failure"),)
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )

    class DispatchProbe:
        def __init__(self) -> None:
            self.calls: list[PRData] = []

        async def dispatch_pr_maintenance(self, pr: PRData) -> None:
            self.calls.append(pr)

    dispatch = DispatchProbe()
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=_ledger(),
        orchestrator=dispatch,
    )
    await workflow.drain()

    assert len(dispatch.calls) == 1
    assert dispatch.calls[0].number == 7
    assert dispatch.calls[0].failing_checks == ["tests"]


@pytest.mark.asyncio
async def test_dispatch_retries_after_coordinator_defers_same_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentic_pr_dash import coordinator
    from agentic_pr_dash.coordinator import DispatchDecision
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    store = LifecycleStore(tmp_path / "state")
    store.enqueue(_intent(pr_number=7))
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False: _pr_payload(),
    )
    monkeypatch.setattr(
        github_api,
        "collect_pr_maintenance_snapshots",
        lambda *args, **kwargs: _batch(
            ci_checks=(CICheck(name="tests", status="completed", conclusion="failure"),)
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )

    class DispatchProbe:
        def __init__(self) -> None:
            self.calls = 0

        async def dispatch_pr_maintenance(self, pr: PRData) -> None:
            self.calls += 1

    decisions = iter(
        (
            DispatchDecision(False, "owner", "live owner"),
            DispatchDecision(True, "none", "ready"),
        )
    )
    decision_calls = 0

    def next_decision(pr: PRData) -> DispatchDecision:
        nonlocal decision_calls
        decision_calls += 1
        return next(decisions)

    monkeypatch.setattr(coordinator, "dispatch_decision_for_pr", next_decision)
    dispatch = DispatchProbe()
    workflow = LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=_ledger(),
        orchestrator=dispatch,
    )

    await workflow.drain()
    await workflow.drain()

    assert dispatch.calls == 1
    assert decision_calls == 2


@pytest.mark.asyncio
async def test_old_intent_head_drift_is_not_promoted_or_observed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    old_intent = _intent(pr_number=7, head_sha="b" * 40)
    store.enqueue(old_intent)
    calls = {"batch": 0}
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False: {
            **_pr_payload(),
            "headRefOid": HEAD,
        },
    )

    def collect(*args, **kwargs):
        calls["batch"] += 1
        return _batch()

    monkeypatch.setattr(github_api, "collect_pr_maintenance_snapshots", collect)
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(store, policy=_policy(), ledger=_ledger())
    result = await workflow.drain()

    record = store.list_intents()[0]
    assert result.deferred == 1
    assert record.state is IntentLifecycleStateV1.PENDING
    assert record.canonical_key is None
    assert calls["batch"] == 0
    drift = store.read_snapshot(
        MaintenanceTargetV1.exact(
            MaintenanceKeyV1(
                repository=REPOSITORY,
                pr_number=7,
                head_sha=old_intent.head_sha,
                workflow_type=old_intent.workflow_type,
            )
        ),
        now=OBSERVED_AT,
    ).snapshot
    assert drift is not None
    assert drift.blockers == (MaintenanceBlockerV1.OBSERVATION_UNAVAILABLE,)


@pytest.mark.asyncio
async def test_resolved_repository_drift_is_not_promoted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=7)
    store.enqueue(intent)
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False: {
            **_pr_payload(),
            "url": "https://github.com/Other/Repository/pull/7",
        },
    )
    monkeypatch.setattr(
        github_api, "collect_pr_maintenance_snapshots", lambda *args, **kwargs: _batch()
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(store, policy=_policy(), ledger=_ledger())
    result = await workflow.drain()

    assert result.deferred == 1
    record = store.list_intents()[0]
    assert record.state is IntentLifecycleStateV1.PENDING
    assert record.canonical_key is None
    degraded = store.read_snapshot(
        MaintenanceTargetV1.exact(
            MaintenanceKeyV1(
                repository=REPOSITORY,
                pr_number=7,
                head_sha=HEAD,
                workflow_type=intent.workflow_type,
            )
        ),
        now=OBSERVED_AT,
    ).snapshot
    assert degraded is not None


@pytest.mark.asyncio
async def test_unavailable_github_is_degraded_and_retryable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=7)
    store.enqueue(intent)

    def unavailable(*args, **kwargs):
        raise RuntimeError("GitHub unavailable")

    monkeypatch.setattr(github_api, "resolve_pr", unavailable)
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(store, policy=_policy(), ledger=_ledger())
    result = await workflow.drain()

    assert result.deferred == 1
    record = store.list_intents()[0]
    assert record.state is IntentLifecycleStateV1.PENDING
    degraded = store.read_snapshot(
        MaintenanceTargetV1.exact(
            MaintenanceKeyV1(
                repository=REPOSITORY,
                pr_number=7,
                head_sha=HEAD,
                workflow_type=intent.workflow_type,
            )
        ),
        now=OBSERVED_AT,
    ).snapshot
    assert degraded is not None
    assert degraded.observation_health.value == "unavailable"


@pytest.mark.asyncio
async def test_known_pr_missing_from_github_is_degraded_and_retryable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=7)
    store.enqueue(intent)
    monkeypatch.setattr(github_api, "resolve_pr", lambda *args, **kwargs: None)
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(store, policy=_policy(), ledger=_ledger())
    result = await workflow.drain()

    assert result.deferred == 1
    record = store.list_intents()[0]
    assert record.state is IntentLifecycleStateV1.PENDING
    degraded = store.read_snapshot(
        MaintenanceTargetV1.exact(
            MaintenanceKeyV1(
                repository=REPOSITORY,
                pr_number=7,
                head_sha=HEAD,
                workflow_type=intent.workflow_type,
            )
        ),
        now=OBSERVED_AT,
    ).snapshot
    assert degraded is not None
    assert degraded.observation_health.value == "unavailable"


@pytest.mark.asyncio
async def test_missing_batch_observation_is_degraded_and_retryable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=7)
    store.enqueue(intent)
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False: _pr_payload(),
    )
    monkeypatch.setattr(
        github_api,
        "collect_pr_maintenance_snapshots",
        lambda *args, **kwargs: github_api.PrMaintenanceSnapshotBatch((7,), {}, (7,)),
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(store, policy=_policy(), ledger=_ledger())
    result = await workflow.drain()

    assert result.deferred == 1
    record = store.list_intents()[0]
    assert record.state is IntentLifecycleStateV1.PENDING
    degraded = store.read_snapshot(
        MaintenanceTargetV1.exact(
            MaintenanceKeyV1(
                repository=REPOSITORY,
                pr_number=7,
                head_sha=HEAD,
                workflow_type=intent.workflow_type,
            )
        ),
        now=OBSERVED_AT,
    ).snapshot
    assert degraded is not None
    assert degraded.observation_health.value == "unavailable"


@pytest.mark.asyncio
async def test_failure_for_one_intent_does_not_stop_the_next(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    failed = _intent(pr_number=7, head_sha="b" * 40).model_copy(
        update={"worktree_path": "/tmp/failed"}
    )
    healthy = _intent(pr_number=8).model_copy(update={"worktree_path": "/tmp/healthy"})
    store.enqueue(failed)
    store.enqueue(healthy)

    def resolve(number, fields, cwd=None, force=False):
        if cwd == failed.worktree_path:
            raise RuntimeError("one intent failed")
        return {**_pr_payload(), "number": 8}

    monkeypatch.setattr(github_api, "resolve_pr", resolve)
    monkeypatch.setattr(
        github_api,
        "collect_pr_maintenance_snapshots",
        lambda *args, **kwargs: _batch().__class__(
            (8,),
            {
                8: _batch()
                .observed[7]
                .__class__(
                    pr_number=8,
                    head_sha=HEAD,
                    head_committed_at=OBSERVED_AT.isoformat(),
                    ci_checks=_batch().observed[7].ci_checks,
                    required_pending=False,
                    unresolved_threads=(),
                    merge_state="CLEAN",
                    mergeable="MERGEABLE",
                    review_decision="APPROVED",
                )
            },
            (),
        ),
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(store, policy=_policy(), ledger=_ledger())
    result = await workflow.drain()

    assert result.deferred == 1
    assert result.progressed == 1
    records = {record.intent.worktree_path: record for record in store.list_intents()}
    assert records[failed.worktree_path].state is IntentLifecycleStateV1.PENDING
    assert records[healthy.worktree_path].state is IntentLifecycleStateV1.PROMOTED


@pytest.mark.asyncio
async def test_drain_is_bounded_by_batch_size(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    intents = [
        _intent(pr_number=7).model_copy(
            update={"repository": "one/repo", "worktree_path": "/tmp/one"}
        ),
        _intent(pr_number=7).model_copy(
            update={"repository": "two/repo", "worktree_path": "/tmp/two"}
        ),
        _intent(pr_number=7).model_copy(
            update={"repository": "three/repo", "worktree_path": "/tmp/three"}
        ),
    ]
    for intent in intents:
        store.enqueue(intent)
    calls = {"resolve": 0}

    def resolve(number, fields, cwd=None, force=False):
        calls["resolve"] += 1
        repo = next(
            intent.repository for intent in intents if intent.worktree_path == cwd
        )
        return {**_pr_payload(), "url": f"https://github.com/{repo}/pull/7"}

    monkeypatch.setattr(github_api, "resolve_pr", resolve)
    monkeypatch.setattr(
        github_api, "collect_pr_maintenance_snapshots", lambda *args, **kwargs: _batch()
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(
        store, policy=_policy(), ledger=_ledger(), batch_size=2
    )
    result = await workflow.drain()

    assert result.examined == 2
    assert calls["resolve"] == 2
    assert (
        sum(
            record.state is IntentLifecycleStateV1.PROMOTED
            for record in store.list_intents()
        )
        == 2
    )


@pytest.mark.asyncio
async def test_drain_batches_observations_for_same_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    first = _intent(pr_number=7).model_copy(
        update={"pushed_ref": "refs/heads/first"}
    )
    second = _intent(pr_number=8).model_copy(
        update={"pushed_ref": "refs/heads/second"}
    )
    store.enqueue(first)
    store.enqueue(second)

    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False: {
            **_pr_payload(),
            "number": number,
        },
    )
    batch_calls: list[list[int]] = []

    def collect(owner, repo, pr_numbers, cwd=None):
        batch_calls.append(list(pr_numbers))
        return github_api.PrMaintenanceSnapshotBatch(
            tuple(pr_numbers),
            {
                number: github_api.PrMaintenanceSnapshot(
                    pr_number=number,
                    head_sha=HEAD,
                    head_committed_at=OBSERVED_AT.isoformat(),
                    ci_checks=(
                        CICheck(
                            name="tests",
                            status="completed",
                            conclusion="success",
                        ),
                    ),
                    required_pending=False,
                    unresolved_threads=(),
                    merge_state="CLEAN",
                    mergeable="MERGEABLE",
                    review_decision="APPROVED",
                )
                for number in pr_numbers
            },
            (),
        )

    monkeypatch.setattr(github_api, "collect_pr_maintenance_snapshots", collect)
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(store, policy=_policy(), ledger=_ledger())
    result = await workflow.drain()

    assert result.progressed == 2
    assert batch_calls == [[7, 8]]


@pytest.mark.asyncio
async def test_first_clean_observation_is_unsettled_then_second_after_interval_settles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Clock:
        current = OBSERVED_AT

        def __call__(self) -> datetime:
            return self.current

    clock = Clock()
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=7)
    store.enqueue(intent)
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False: _pr_payload(),
    )
    monkeypatch.setattr(
        github_api, "collect_pr_maintenance_snapshots", lambda *args, **kwargs: _batch()
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(store, policy=_policy(), ledger=_ledger(), now=clock)
    await workflow.drain()
    record = store.list_intents()[0]
    assert record.canonical_key is not None
    first = store.read_snapshot(
        MaintenanceTargetV1.exact(record.canonical_key), now=OBSERVED_AT
    ).snapshot
    assert first is not None
    assert first.stable_observation_count == 1
    assert not first.settled

    clock.current += timedelta(seconds=29)
    await workflow.drain()
    under_limit = store.read_snapshot(
        MaintenanceTargetV1.exact(record.canonical_key), now=clock.current
    ).snapshot
    assert under_limit is not None
    assert under_limit.stable_observation_count == 1
    assert not under_limit.settled

    clock.current += timedelta(seconds=1)
    await workflow.drain()
    second = store.read_snapshot(
        MaintenanceTargetV1.exact(record.canonical_key), now=clock.current
    ).snapshot
    assert second is not None
    assert second.stable_observation_count == 2
    assert second.settled


@pytest.mark.asyncio
async def test_capability_refusal_never_settles_as_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Clock:
        current = OBSERVED_AT

        def __call__(self) -> datetime:
            return self.current

    clock = Clock()
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=7)
    store.enqueue(intent)
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False: _pr_payload(),
    )
    monkeypatch.setattr(
        github_api, "collect_pr_maintenance_snapshots", lambda *args, **kwargs: _batch()
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.capability_refused(
            "review capability unavailable"
        ),
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(store, policy=_policy(), ledger=_ledger(), now=clock)
    await workflow.drain()
    clock.current += timedelta(seconds=30)
    await workflow.drain()

    record = store.list_intents()[0]
    assert record.canonical_key is not None
    snapshot = store.read_snapshot(
        MaintenanceTargetV1.exact(record.canonical_key), now=clock.current
    ).snapshot
    assert snapshot is not None
    assert snapshot.observation_health.value == "partial"
    assert not snapshot.settled


def _thread() -> ReviewThread:
    return ReviewThread(
        node_id="PRRT_one",
        is_resolved=False,
        is_outdated=False,
        top=ReviewThreadComment(
            database_id=1,
            path="src/feature.py",
            line=10,
            body="[P2] Address this finding",
            author="reviewer",
            created_at=OBSERVED_AT.isoformat(),
            review_id=100,
        ),
    )


@pytest.mark.asyncio
async def test_unaddressed_threads_project_to_snapshot_and_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=7)
    store.enqueue(intent)
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False: _pr_payload(),
    )
    monkeypatch.setattr(
        github_api,
        "collect_pr_maintenance_snapshots",
        lambda *args, **kwargs: _batch(unresolved_threads=(_thread(),)),
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )

    class DispatchProbe:
        def __init__(self) -> None:
            self.calls = 0

        async def dispatch_pr_maintenance(self, pr: PRData) -> None:
            self.calls += 1

    dispatch = DispatchProbe()
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=_ledger(),
        orchestrator=dispatch,
    )
    await workflow.drain()

    record = store.list_intents()[0]
    assert record.canonical_key is not None
    snapshot = store.read_snapshot(
        MaintenanceTargetV1.exact(record.canonical_key), now=OBSERVED_AT
    ).snapshot
    assert snapshot is not None
    assert snapshot.raw_unresolved_thread_count == 1
    assert snapshot.unaddressed_thread_count == 1
    assert snapshot.policy_unsettled_finding_count == 1
    assert snapshot.blockers == (MaintenanceBlockerV1.REVIEW_FINDINGS,)
    assert dispatch.calls == 1


@pytest.mark.asyncio
async def test_policy_finding_reaches_existing_dispatch_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=7)
    store.enqueue(intent)
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False: _pr_payload(),
    )
    monkeypatch.setattr(
        github_api, "collect_pr_maintenance_snapshots", lambda *args, **kwargs: _batch()
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )

    class DispatchProbe:
        def __init__(self) -> None:
            self.calls: list[PRData] = []

        async def dispatch_pr_maintenance(self, pr: PRData) -> None:
            self.calls.append(pr)

    dispatch = DispatchProbe()
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=_ledger_with_unresolved_finding(),
        orchestrator=dispatch,
    )
    await workflow.drain()

    assert len(dispatch.calls) == 1
    assert dispatch.calls[0].review_comments


@pytest.mark.asyncio
async def test_live_owner_defers_existing_orchestrator_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentic_pr_dash import maintenance_check
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow
    from agentic_pr_dash.orchestrator import Orchestrator

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv("AGENTIC_PR_DASH_MARKER_WRITES", "1")
    monkeypatch.setenv(
        "AGENTIC_PR_DASH_COORDINATOR_STORE", str(tmp_path / "claims.jsonl")
    )
    maintenance_check._write_arm_marker(str(worktree), "live-session", os.getpid(), 7)
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=7).model_copy(update={"worktree_path": str(worktree)})
    store.enqueue(intent)
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False: _pr_payload(),
    )
    monkeypatch.setattr(
        github_api,
        "collect_pr_maintenance_snapshots",
        lambda *args, **kwargs: _batch(unresolved_threads=(_thread(),)),
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )
    orchestrator = Orchestrator(repo_cwd=None)
    workflow = LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=_ledger(),
        orchestrator=orchestrator,
    )

    await workflow.drain()

    assert orchestrator.get_pr(7) is None
    record = store.list_intents()[0]
    assert record.canonical_key is not None


@pytest.mark.asyncio
async def test_changed_observation_resets_stability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Clock:
        current = OBSERVED_AT

        def __call__(self) -> datetime:
            return self.current

    clock = Clock()
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=7)
    store.enqueue(intent)
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False: _pr_payload(),
    )
    observations = iter(
        (_batch(), _batch(merge_state="DIRTY", mergeable="CONFLICTING"))
    )
    monkeypatch.setattr(
        github_api,
        "collect_pr_maintenance_snapshots",
        lambda *args, **kwargs: next(observations),
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(store, policy=_policy(), ledger=_ledger(), now=clock)
    await workflow.drain()
    clock.current += timedelta(seconds=30)
    await workflow.drain()

    record = store.list_intents()[0]
    assert record.canonical_key is not None
    snapshot = store.read_snapshot(
        MaintenanceTargetV1.exact(record.canonical_key), now=clock.current
    ).snapshot
    assert snapshot is not None
    assert snapshot.stable_observation_count == 0
    assert not snapshot.settled
    assert snapshot.blockers == (MaintenanceBlockerV1.MERGE_CONFLICT,)


@pytest.mark.asyncio
async def test_same_pr_number_in_two_repositories_stays_isolated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    first = _intent(pr_number=7).model_copy(
        update={"repository": "one/repo", "worktree_path": "/tmp/one"}
    )
    second = _intent(pr_number=7).model_copy(
        update={"repository": "two/repo", "worktree_path": "/tmp/two"}
    )
    store.enqueue(first)
    store.enqueue(second)

    def resolve(number, fields, cwd=None, force=False):
        repo = "one/repo" if cwd == first.worktree_path else "two/repo"
        return {
            **_pr_payload(),
            "url": f"https://github.com/{repo}/pull/7",
        }

    monkeypatch.setattr(github_api, "resolve_pr", resolve)
    monkeypatch.setattr(
        github_api, "collect_pr_maintenance_snapshots", lambda *args, **kwargs: _batch()
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    # The fixed review ledger is deliberately only used to test key isolation;
    # both repository observations have identical clean facts.
    workflow = LifecycleWorkflow(store, policy=_policy(), ledger=_ledger())
    await workflow.drain()

    records = store.list_intents()
    assert len(records) == 2
    assert {
        record.canonical_key.repository for record in records if record.canonical_key
    } == {
        "one/repo",
        "two/repo",
    }


@pytest.mark.asyncio
async def test_orchestrator_refresh_drains_lifecycle_under_refresh_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentic_pr_dash.orchestrator import Orchestrator

    class DrainProbe:
        def __init__(self) -> None:
            self.orchestrator: Orchestrator | None = None
            self.calls = 0

        async def drain(self) -> None:
            assert self.orchestrator is not None
            assert self.orchestrator._refresh_lock.locked()
            self.calls += 1

    lifecycle = DrainProbe()
    orchestrator = Orchestrator(repo_cwd=None, lifecycle_workflow=lifecycle)
    lifecycle.orchestrator = orchestrator
    monkeypatch.setattr(
        github_api, "get_weekly_runner_execution_summary", lambda *args: None
    )
    orchestrator._refresh_repo = AsyncMock()

    await orchestrator.refresh_prs()

    assert lifecycle.calls == 1


def test_default_review_context_loader_reads_worktree_policy_and_ledger(
    tmp_path: Path,
) -> None:
    from agentic_pr_dash.lifecycle_models import MaintenanceIntentRecordV1
    from agentic_pr_dash.lifecycle_workflow import load_review_context

    policy_path = tmp_path / "config" / "review-policy.yaml"
    policy_path.parent.mkdir()
    policy_path.write_text(
        "version: 1\n"
        "review:\n"
        "  local:\n"
        "    reviewer_count: 1\n"
        "  backstop:\n"
        "    reviewer_count: 1\n"
        "    trigger: new_head_sha\n",
        encoding="utf-8",
    )
    ledger_path = tmp_path / ".agentic-review" / "ledger.json"
    ledger_path.parent.mkdir()
    ledger_path.write_text(_ledger().model_dump_json(), encoding="utf-8")
    intent = _intent().model_copy(update={"worktree_path": str(tmp_path)})
    record = MaintenanceIntentRecordV1(
        ingress_id="intent",
        intent=intent,
        state=IntentLifecycleStateV1.PENDING,
    )

    context = load_review_context(record)

    assert context is not None
    assert context[0].review.local.reviewer_count == 1
    assert context[1].repository == REPOSITORY
