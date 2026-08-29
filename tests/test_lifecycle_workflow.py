from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from agent_review_coordinator import (
    Disposition,
    Finding,
    FindingSettlementState,
    ReviewLedger,
    ReviewPolicy,
    ReviewResult,
    ReviewStage,
    Severity,
)

from agentic_pr_dash import github_api
from agentic_pr_dash._maintenance import completion, review_settlement
from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment
from agentic_pr_dash.lifecycle_models import (
    IntentLifecycleStateV1,
    MaintenanceBlockerV1,
    MaintenanceIntentV1,
    MaintenanceKeyV1,
    MaintenanceNextActionV1,
    MaintenanceSnapshotV1,
    MaintenanceTargetV1,
    MergeabilityStateV1,
    ObservationHealthV1,
    RequiredCIStateV1,
    ReviewStateV1,
    ReviewWatchStatusV1,
    SnapshotReadStatusV1,
)
from agentic_pr_dash.lifecycle_workflow import _next_review_watch
from agentic_pr_dash.lifecycle_store import LifecycleStore
from agentic_pr_dash.models import CICheck, PRData

OBSERVED_AT = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)
HEAD = "a" * 40
REPOSITORY = "Acme/Widget"


def test_review_watch_arms_and_advances_through_repeating_tail() -> None:
    watch = _next_review_watch(
        None,
        now=OBSERVED_AT,
        head_sha=HEAD,
        ci_state=RequiredCIStateV1.PASSING,
        observation_succeeded=True,
        actionable_count=0,
    )
    assert watch is not None
    assert watch.next_check_at == OBSERVED_AT + timedelta(minutes=1)

    for expected_index, minutes in enumerate((5, 15, 30, 60, 120, 240, 480), 1):
        watch = _next_review_watch(
            watch,
            now=watch.next_check_at,
            head_sha=HEAD,
            ci_state=RequiredCIStateV1.PASSING,
            observation_succeeded=True,
            actionable_count=0,
        )
        assert watch.interval_index == expected_index
        assert watch.reset_at == OBSERVED_AT
        assert watch.next_check_at == OBSERVED_AT + timedelta(minutes=minutes)

    repeated = _next_review_watch(
        watch,
        now=watch.next_check_at,
        head_sha=HEAD,
        ci_state=RequiredCIStateV1.PASSING,
        observation_succeeded=True,
        actionable_count=0,
    )
    assert repeated.interval_index == 8
    assert repeated.next_check_at == OBSERVED_AT + timedelta(minutes=960)


def test_review_watch_records_every_successful_observation() -> None:
    watch = _next_review_watch(
        None,
        now=OBSERVED_AT,
        head_sha=HEAD,
        ci_state=RequiredCIStateV1.PASSING,
        observation_succeeded=True,
        actionable_count=0,
    )
    early = OBSERVED_AT + timedelta(seconds=30)

    observed = _next_review_watch(
        watch,
        now=early,
        head_sha=HEAD,
        ci_state=RequiredCIStateV1.PASSING,
        observation_succeeded=True,
        actionable_count=0,
    )

    assert observed.last_observed_at == early
    assert observed.interval_index == 0
    assert observed.next_check_at == watch.next_check_at


def test_review_watch_skips_deadlines_missed_during_an_outage() -> None:
    watch = _next_review_watch(
        None,
        now=OBSERVED_AT,
        head_sha=HEAD,
        ci_state=RequiredCIStateV1.PASSING,
        observation_succeeded=True,
        actionable_count=0,
    )
    late = OBSERVED_AT + timedelta(minutes=20)

    resumed = _next_review_watch(
        watch,
        now=late,
        head_sha=HEAD,
        ci_state=RequiredCIStateV1.PASSING,
        observation_succeeded=True,
        actionable_count=0,
    )

    assert resumed.interval_index == 3
    assert resumed.next_check_at == OBSERVED_AT + timedelta(minutes=30)


def test_review_watch_resets_on_feedback_and_pauses_without_green_ci() -> None:
    watch = _next_review_watch(
        None,
        now=OBSERVED_AT,
        head_sha=HEAD,
        ci_state=RequiredCIStateV1.PASSING,
        observation_succeeded=True,
        actionable_count=0,
    )
    reset_at = OBSERVED_AT + timedelta(minutes=5)
    reset = _next_review_watch(
        watch,
        now=reset_at,
        head_sha=HEAD,
        ci_state=RequiredCIStateV1.PASSING,
        observation_succeeded=True,
        actionable_count=2,
    )
    assert reset.interval_index == 0
    assert reset.next_check_at == reset_at + timedelta(minutes=1)
    assert reset.unresolved_thread_count == 2

    paused = _next_review_watch(
        reset,
        now=reset_at + timedelta(seconds=30),
        head_sha=HEAD,
        ci_state=RequiredCIStateV1.FAILING,
        observation_succeeded=True,
        actionable_count=0,
    )
    assert paused.status is ReviewWatchStatusV1.PAUSED
    assert paused.next_check_at == reset.next_check_at


def test_failed_review_observation_does_not_advance_due_watch() -> None:
    watch = _next_review_watch(
        None,
        now=OBSERVED_AT,
        head_sha=HEAD,
        ci_state=RequiredCIStateV1.PASSING,
        observation_succeeded=True,
        actionable_count=0,
    )
    due_at = watch.next_check_at

    unchanged = _next_review_watch(
        watch,
        now=due_at,
        head_sha=HEAD,
        ci_state=RequiredCIStateV1.PASSING,
        observation_succeeded=False,
        actionable_count=0,
    )

    assert unchanged.interval_index == 0
    assert unchanged.next_check_at == due_at
    assert unchanged.status is ReviewWatchStatusV1.DUE


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
        "state": "OPEN",
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED",
        "author": {"login": "pr-author"},
    }


@pytest.mark.asyncio
async def test_missing_review_context_uses_policy_neutral_observation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    store.enqueue(_intent(pr_number=7))
    monkeypatch.setattr(github_api, "resolve_pr", lambda *a, **k: _pr_payload())
    monkeypatch.setattr(
        github_api, "collect_pr_maintenance_snapshots", lambda *a, **k: _batch()
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *a, **k: github_api.ObservationReadResult.observed([]),
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    result = await LifecycleWorkflow(store, context_loader=lambda record: None).drain()

    record = store.list_intents()[0]
    assert result.progressed == 1
    assert record.state is IntentLifecycleStateV1.PROMOTED
    assert record.canonical_key is not None


@pytest.mark.asyncio
async def test_review_observation_reads_from_resolved_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    store.enqueue(_intent(pr_number=7))
    observed_repositories: list[str | None] = []

    monkeypatch.setattr(github_api, "resolve_pr", lambda *a, **k: _pr_payload())
    monkeypatch.setattr(
        github_api, "collect_pr_maintenance_snapshots", lambda *a, **k: _batch()
    )

    def observe_reviews(*args, repository=None, **kwargs):
        observed_repositories.append(repository)
        return github_api.ObservationReadResult.observed([])

    monkeypatch.setattr(
        github_api, "get_review_submissions_observation", observe_reviews
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    await LifecycleWorkflow(store, policy=_policy(), ledger=_ledger()).drain()

    assert observed_repositories == [REPOSITORY]


def test_policy_review_comment_contains_actionable_finding_details() -> None:
    from agentic_pr_dash.lifecycle_workflow import _policy_review_comments

    ledger = _ledger_with_unresolved_finding()
    finding = next(
        item
        for item in ledger.current_findings
        if item.title == "Unresolved local finding"
    )
    observation = type(
        "Observation",
        (),
        {
            "review": type(
                "Review",
                (),
                {
                    "finding_states": {
                        finding.fingerprint: FindingSettlementState.UNRESOLVED
                    }
                },
            )()
        },
    )()

    comments = _policy_review_comments(observation, ledger)

    assert len(comments) == 1
    assert comments[0].path == "src/example.py"
    assert "Unresolved local finding" in comments[0].body
    assert "The lifecycle workflow must retain this finding." in comments[0].body
    assert "local finding invariant" in comments[0].body
    assert finding.fingerprint in comments[0].body


@pytest.mark.asyncio
async def test_superseded_exact_head_intent_becomes_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    old_head = "b" * 40
    store.enqueue(_intent(pr_number=7, head_sha=old_head))
    monkeypatch.setattr(github_api, "resolve_pr", lambda *a, **k: _pr_payload())
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    result = await LifecycleWorkflow(store, policy=_policy(), ledger=_ledger()).drain()

    assert result.progressed == 1
    superseded = next(
        record for record in store.list_intents() if record.intent.head_sha == old_head
    )
    assert superseded.state is IntentLifecycleStateV1.SETTLED


@pytest.mark.asyncio
async def test_unresolved_intent_lookup_matches_the_exact_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    store.enqueue(_intent(pr_number=None, head_sha=HEAD))
    observed: list[str | None] = []

    def find_pr_by_head(branch, state, cwd, *, head_oid=None, repository=None):
        observed.append(head_oid)
        return _pr_payload()

    monkeypatch.setattr(github_api, "find_pr_by_head", find_pr_by_head)
    monkeypatch.setattr(
        github_api, "collect_pr_maintenance_snapshots", lambda *a, **k: _batch()
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *a, **k: github_api.ObservationReadResult.observed([]),
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    await LifecycleWorkflow(store, policy=_policy(), ledger=_ledger()).drain()

    assert observed == [HEAD]


@pytest.mark.asyncio
async def test_merged_exact_pr_intent_becomes_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    store.enqueue(_intent(pr_number=7))
    monkeypatch.setattr(
        github_api, "resolve_pr", lambda *a, **k: {**_pr_payload(), "state": "MERGED"}
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    result = await LifecycleWorkflow(store, policy=_policy(), ledger=_ledger()).drain()

    assert result.progressed == 1
    assert store.list_intents()[0].state is IntentLifecycleStateV1.SETTLED


@pytest.mark.asyncio
async def test_lifecycle_excludes_pr_author_from_review_observation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    store.enqueue(_intent(pr_number=7))
    monkeypatch.setattr(github_api, "resolve_pr", lambda *a, **k: _pr_payload())
    monkeypatch.setattr(
        github_api, "collect_pr_maintenance_snapshots", lambda *a, **k: _batch()
    )
    excluded: list[set[str]] = []
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *a, **k: (
            excluded.append(k["excluded_authors"])
            or github_api.ObservationReadResult.observed([])
        ),
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    await LifecycleWorkflow(store, policy=_policy(), ledger=_ledger()).drain()

    assert excluded == [{"pr-author"}]


def _ordered_intents(*, count: int = 2) -> list[MaintenanceIntentV1]:
    intents = [
        _intent(pr_number=7).model_copy(
            update={
                "pushed_ref": f"refs/heads/ordered-{index}",
                "worktree_path": f"/tmp/ordered-{index}",
            }
        )
        for index in range(count)
    ]
    return sorted(
        intents,
        key=lambda intent: LifecycleStore(Path("/tmp/unused")).intent_path(intent).name,
    )


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
        lambda branch, state="open", cwd=None, **kwargs: None,
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
        lambda number, fields, cwd=None, force=False, **kwargs: _pr_payload(),
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
async def test_stale_no_pr_resolution_cannot_overwrite_concurrent_reactivation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    unresolved = _intent()
    store.enqueue(unresolved)

    def resolve_then_reactivate(*args, **kwargs):
        store.enqueue(
            unresolved.model_copy(
                update={"pr_number": 7, "reason": "PR created concurrently"}
            )
        )

    monkeypatch.setattr(github_api, "find_pr_by_head", resolve_then_reactivate)
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    result = await LifecycleWorkflow(store, policy=_policy(), ledger=_ledger()).drain()

    current = store.list_intents()[0]
    assert result.deferred == 1
    assert current.generation == 2
    assert current.state is IntentLifecycleStateV1.PENDING
    assert current.intent.pr_number == 7


@pytest.mark.asyncio
async def test_open_branch_lookup_without_state_field_can_promote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    store.enqueue(_intent())
    branch_payload = _pr_payload()
    branch_payload.pop("state")
    monkeypatch.setattr(
        github_api,
        "find_pr_by_head",
        lambda branch, state="open", cwd=None, head_oid=None, **kwargs: branch_payload,
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

    result = await LifecycleWorkflow(store, policy=_policy(), ledger=_ledger()).drain()

    assert result.progressed == 1
    assert store.list_intents()[0].state is IntentLifecycleStateV1.PROMOTED


@pytest.mark.parametrize(
    ("state", "is_draft"),
    [("OPEN", True), (None, False)],
)
@pytest.mark.asyncio
async def test_only_explicitly_open_nondraft_prs_are_observed_or_dispatched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str | None,
    is_draft: bool,
) -> None:
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=7)
    store.enqueue(intent)
    payload = {**_pr_payload(), "isDraft": is_draft}
    if state is None:
        payload.pop("state")
    else:
        payload["state"] = state
    monkeypatch.setattr(github_api, "resolve_pr", lambda *args, **kwargs: payload)
    observation_calls = 0

    def collect(*args, **kwargs):
        nonlocal observation_calls
        observation_calls += 1
        return _batch()

    monkeypatch.setattr(github_api, "collect_pr_maintenance_snapshots", collect)

    class DispatchProbe:
        calls = 0

        async def dispatch_pr_maintenance(self, pr: PRData) -> None:
            self.calls += 1

    dispatch = DispatchProbe()
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(
        store, policy=_policy(), ledger=_ledger(), orchestrator=dispatch
    )

    result = await workflow.drain()

    assert result.deferred == 1
    assert observation_calls == 0
    assert dispatch.calls == 0
    assert store.list_intents()[0].state is IntentLifecycleStateV1.PENDING


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
        lambda number, fields, cwd=None, force=False, **kwargs: _pr_payload(),
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
        lambda number, fields, cwd=None, force=False, **kwargs: _pr_payload(),
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
        lambda number, fields, cwd=None, force=False, **kwargs: _pr_payload(),
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
        lambda number, fields, cwd=None, force=False, **kwargs: _pr_payload(),
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

    class Clock:
        current = OBSERVED_AT

        def __call__(self) -> datetime:
            return self.current

    clock = Clock()
    store = LifecycleStore(tmp_path / "state")
    store.enqueue(_intent(pr_number=7))
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False, **kwargs: _pr_payload(),
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
        now=clock,
    )

    await workflow.drain()
    await workflow.drain()
    assert dispatch.calls == 0
    assert decision_calls == 1

    clock.current += timedelta(seconds=30)
    await workflow.drain()

    assert dispatch.calls == 1
    assert decision_calls == 2


@pytest.mark.asyncio
async def test_old_intent_head_drift_is_retired_without_observation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    old_intent = _intent(pr_number=7, head_sha="b" * 40)
    store.enqueue(old_intent)
    calls = {"batch": 0, "resolve": 0}

    def resolve(number, fields, cwd=None, force=False, **kwargs):
        calls["resolve"] += 1
        return {
            **_pr_payload(),
            "headRefOid": HEAD,
        }

    monkeypatch.setattr(github_api, "resolve_pr", resolve)

    def collect(*args, **kwargs):
        calls["batch"] += 1
        return _batch()

    monkeypatch.setattr(github_api, "collect_pr_maintenance_snapshots", collect)
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=_ledger(),
        now=lambda: OBSERVED_AT,
    )
    result = await workflow.drain()

    record = next(
        item
        for item in store.list_intents()
        if item.intent.head_sha == old_intent.head_sha
    )
    assert result.progressed == 1
    assert record.state is IntentLifecycleStateV1.SETTLED
    assert record.canonical_key is not None
    assert record.canonical_key.head_sha == old_intent.head_sha
    assert record.next_attempt_at is None
    assert calls["resolve"] == 1
    assert calls["batch"] == 0

    repeated = await workflow.drain()

    adopted = next(
        item for item in store.list_intents() if item.intent.head_sha == HEAD
    )
    assert repeated.examined == 1
    assert adopted.state is IntentLifecycleStateV1.PROMOTED
    assert calls["batch"] == 1


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
        lambda number, fields, cwd=None, force=False, **kwargs: {
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
        lambda number, fields, cwd=None, force=False, **kwargs: _pr_payload(),
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

    def resolve(number, fields, cwd=None, force=False, **kwargs):
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

    def resolve(number, fields, cwd=None, force=False, **kwargs):
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
async def test_drain_prioritizes_oldest_retry_cohort_over_filename_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    intents = [
        _intent(pr_number=number).model_copy(
            update={
                "pushed_ref": f"refs/heads/feature/{number}",
                "head_sha": f"{number:040x}",
                "requested_at": OBSERVED_AT + timedelta(seconds=number),
            }
        )
        for number in range(1, 5)
    ]
    for intent in intents:
        store.enqueue(intent)

    records = list(store.list_intents())
    oldest = max(records, key=lambda record: record.intent.requested_at)
    store.schedule_retry(
        oldest.intent,
        next_attempt_at=OBSERVED_AT - timedelta(seconds=1),
        expected_generation=oldest.generation,
        expected_revision=oldest.revision,
    )
    resolved: list[int] = []

    def resolve(number, fields, cwd=None, force=False, repository=None):
        resolved.append(number)
        return {**_pr_payload(), "number": number}

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

    await LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=_ledger(),
        now=lambda: OBSERVED_AT + timedelta(minutes=1),
        batch_size=1,
    ).drain()

    assert resolved == [oldest.intent.pr_number]


def test_resolve_uses_intent_repository_for_github_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    store = LifecycleStore(tmp_path / "state")
    workflow = LifecycleWorkflow(store, policy=_policy(), ledger=_ledger())
    seen: dict[str, object] = {}

    def resolve(number, fields, cwd=None, force=False, repository=None):
        seen.update(number=number, cwd=cwd, repository=repository)
        return _pr_payload()

    monkeypatch.setattr(github_api, "resolve_pr", resolve)
    workflow._resolve(store.enqueue(_intent(pr_number=7)) and store.list_intents()[0])

    assert seen["repository"] == REPOSITORY


@pytest.mark.asyncio
async def test_promoted_batch_retry_backoff_allows_later_intent_to_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ordered = [
        intent.model_copy(update={"pr_number": 7 + index})
        for index, intent in enumerate(_ordered_intents(count=3))
    ]
    store = LifecycleStore(tmp_path / "state")
    for intent in ordered:
        store.enqueue(intent)

    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False, **kwargs: {
            **_pr_payload(),
            "number": number,
        },
    )

    def collect(owner, repo, pr_numbers, cwd=None):
        baseline = _batch().observed[7]
        return github_api.PrMaintenanceSnapshotBatch(
            tuple(pr_numbers),
            {
                number: baseline.__class__(
                    pr_number=number,
                    head_sha=baseline.head_sha,
                    head_committed_at=baseline.head_committed_at,
                    ci_checks=baseline.ci_checks,
                    required_pending=baseline.required_pending,
                    unresolved_threads=baseline.unresolved_threads,
                    merge_state=baseline.merge_state,
                    mergeable=baseline.mergeable,
                    review_decision=baseline.review_decision,
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

    workflow = LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=_ledger(),
        now=lambda: OBSERVED_AT,
        batch_size=2,
    )

    first = await workflow.drain()
    second = await workflow.drain()

    records = {record.intent.pushed_ref: record for record in store.list_intents()}
    assert first.progressed == 2
    assert second.progressed == 1
    assert all(
        records[intent.pushed_ref].state is IntentLifecycleStateV1.PROMOTED
        for intent in ordered
    )
    assert all(
        records[intent.pushed_ref].next_attempt_at
        == OBSERVED_AT + timedelta(seconds=30)
        for intent in ordered
    )


@pytest.mark.asyncio
async def test_no_pr_retry_eligibility_is_durable_and_does_not_starve_later_intent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first, later = _ordered_intents()
    first = first.model_copy(update={"pr_number": None})
    store = LifecycleStore(tmp_path / "state")
    store.enqueue(first)
    store.enqueue(later)
    monkeypatch.setattr(github_api, "find_pr_by_head", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda *args, **kwargs: _pr_payload(),
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

    workflow = LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=_ledger(),
        now=lambda: OBSERVED_AT,
        batch_size=1,
    )
    first_result = await workflow.drain()

    first_record = next(
        record
        for record in store.list_intents()
        if record.intent.pushed_ref == first.pushed_ref
    )
    assert first_result.no_pr == 1
    assert first_record.next_attempt_at is not None
    assert first_record.next_attempt_at > OBSERVED_AT

    restarted = LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=_ledger(),
        now=lambda: OBSERVED_AT,
        batch_size=1,
    )
    second_result = await restarted.drain()

    assert second_result.progressed == 1
    later_record = next(
        record
        for record in store.list_intents()
        if record.intent.pushed_ref == later.pushed_ref
    )
    assert later_record.state is IntentLifecycleStateV1.PROMOTED


@pytest.mark.asyncio
async def test_watched_clean_record_does_not_starve_later_intent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Clock:
        current = OBSERVED_AT

        def __call__(self) -> datetime:
            return self.current

    clock = Clock()
    first, later = _ordered_intents()
    store = LifecycleStore(tmp_path / "state")
    store.enqueue(first)
    monkeypatch.setattr(github_api, "resolve_pr", lambda *args, **kwargs: _pr_payload())
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
        store,
        policy=_policy(),
        ledger=_ledger(),
        now=clock,
        batch_size=1,
        stabilization_interval=timedelta(0),
    )
    await workflow.drain()
    clock.current += timedelta(seconds=30)
    await workflow.drain()

    first_record = store.list_intents()[0]
    assert first_record.state is IntentLifecycleStateV1.PROMOTED
    assert first_record.next_attempt_at == OBSERVED_AT + timedelta(minutes=1)
    store.enqueue(later)

    restarted = LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=_ledger(),
        now=clock,
        batch_size=1,
    )
    result = await restarted.drain()

    assert result.progressed == 1
    duplicate = store.enqueue(first.model_copy(update={"reason": "new feedback"}))
    assert duplicate.status.value == "duplicate"


@pytest.mark.asyncio
async def test_clean_watch_remains_promoted_after_stabilization(
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
    monkeypatch.setattr(github_api, "resolve_pr", lambda *args, **kwargs: _pr_payload())
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
    clock.current += timedelta(seconds=30)
    await workflow.drain()
    watched = store.list_intents()[0]
    assert watched.state is IntentLifecycleStateV1.PROMOTED
    snapshot = store.read_snapshot(
        MaintenanceTargetV1.exact(watched.canonical_key), now=clock.current
    ).snapshot
    assert snapshot is not None and snapshot.settled
    assert snapshot.review_watch is not None
    assert watched.next_attempt_at == snapshot.review_watch.next_check_at


@pytest.mark.asyncio
async def test_pr_resolution_runs_off_loop_with_bounded_concurrency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    for intent in _ordered_intents(count=4):
        store.enqueue(intent)
    counter_lock = threading.Lock()
    active = 0
    max_active = 0

    def blocking_resolution(*args, **kwargs):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with counter_lock:
            active -= 1

    monkeypatch.setattr(github_api, "resolve_pr", blocking_resolution)
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=_ledger(),
        batch_size=4,
        resolution_concurrency=2,
    )
    drain_task = asyncio.create_task(workflow.drain())
    await asyncio.sleep(0.01)

    assert not drain_task.done()
    await drain_task
    assert max_active == 2


@pytest.mark.asyncio
async def test_drain_batches_observations_for_same_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    first = _intent(pr_number=7).model_copy(update={"pushed_ref": "refs/heads/first"})
    second = _intent(pr_number=8).model_copy(update={"pushed_ref": "refs/heads/second"})
    store.enqueue(first)
    store.enqueue(second)

    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False, **kwargs: {
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
        lambda number, fields, cwd=None, force=False, **kwargs: _pr_payload(),
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
        lambda number, fields, cwd=None, force=False, **kwargs: _pr_payload(),
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
async def test_fresh_non_code_reply_settles_with_thread_permitted_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Clock:
        current = OBSERVED_AT

        def __call__(self) -> datetime:
            return self.current

    thread = ReviewThread(
        node_id="PRRT_visible",
        is_resolved=False,
        is_outdated=False,
        top=ReviewThreadComment(
            database_id=10,
            path="src/feature.py",
            line=10,
            body="[P1] Preserve the lifecycle fence",
            author="reviewer",
            created_at=OBSERVED_AT.isoformat(),
            review_id=100,
        ),
    )
    finding = review_settlement.finding_from_thread(
        thread,
        repository=REPOSITORY,
        head_sha=HEAD,
        reviewer_execution_id="local-visible-review",
    )
    ledger = _ledger()
    ledger.submit(
        ReviewResult(
            repository=REPOSITORY,
            head_sha=HEAD,
            stage=ReviewStage.LOCAL,
            round_number=2,
            slot_number=1,
            reviewer_execution_id="local-visible-review",
            findings=[finding],
        )
    )
    ledger.record_disposition(
        fingerprint=finding.fingerprint,
        disposition=Disposition.REJECT,
        rationale="The reported path cannot reach the lifecycle boundary.",
        evidence="targeted lifecycle boundary reproduction",
    )
    closure = review_settlement.classify_thread_closure(
        thread,
        policy=_policy(),
        ledger=ledger,
    )
    assert closure is not None
    visible_reply = completion.structured_settlement_reply_body(
        marker="<!-- agentic-pr-dash:completed -->",
        finding=closure.finding,
        head_sha=HEAD,
    )
    thread.replies.append(
        ReviewThreadComment(
            database_id=11,
            path=thread.top.path,
            line=thread.top.line,
            body=visible_reply,
            author="maintenance-bot",
            created_at=(OBSERVED_AT + timedelta(seconds=1)).isoformat(),
        )
    )

    clock = Clock()
    store = LifecycleStore(tmp_path / "state")
    store.enqueue(_intent(pr_number=7))
    monkeypatch.setattr(
        github_api,
        "resolve_pr",
        lambda number, fields, cwd=None, force=False, **kwargs: _pr_payload(),
    )
    monkeypatch.setattr(
        github_api,
        "collect_pr_maintenance_snapshots",
        lambda *args, **kwargs: _batch(unresolved_threads=(thread,)),
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *args, **kwargs: github_api.ObservationReadResult.observed([]),
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=ledger,
        maintenance_author="maintenance-bot",
        now=clock,
    )
    await workflow.drain()
    record = store.list_intents()[0]
    assert record.canonical_key is not None
    first = store.read_snapshot(
        MaintenanceTargetV1.exact(record.canonical_key), now=clock.current
    ).snapshot
    assert first is not None
    assert first.raw_unresolved_thread_count == 1
    assert first.unaddressed_thread_count == 0
    assert MaintenanceBlockerV1.REVIEW_FINDINGS not in first.blockers
    assert first.stable_observation_count == 1
    assert not first.settled

    clock.current += timedelta(seconds=30)
    await workflow.drain()
    second = store.read_snapshot(
        MaintenanceTargetV1.exact(record.canonical_key), now=clock.current
    ).snapshot
    assert second is not None
    assert second.raw_unresolved_thread_count == 1
    assert second.unaddressed_thread_count == 0
    assert second.stable_observation_count == 2
    assert second.settled


@pytest.mark.asyncio
async def test_fresh_non_code_reply_is_removed_from_dispatch_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    thread = _thread()
    finding = review_settlement.finding_from_thread(
        thread,
        repository=REPOSITORY,
        head_sha=HEAD,
        reviewer_execution_id="local-visible-review",
    )
    ledger = _ledger()
    ledger.submit(
        ReviewResult(
            repository=REPOSITORY,
            head_sha=HEAD,
            stage=ReviewStage.LOCAL,
            round_number=2,
            slot_number=1,
            reviewer_execution_id="local-visible-review",
            findings=[finding],
        )
    )
    ledger.record_disposition(
        fingerprint=finding.fingerprint,
        disposition=Disposition.REJECT,
        rationale="The reported path cannot reach the lifecycle boundary.",
        evidence="targeted lifecycle boundary reproduction",
    )
    closure = review_settlement.classify_thread_closure(
        thread, policy=_policy(), ledger=ledger
    )
    assert closure is not None
    thread.replies.append(
        ReviewThreadComment(
            database_id=11,
            path=thread.top.path,
            line=thread.top.line,
            body=completion.structured_settlement_reply_body(
                marker="<!-- agentic-pr-dash:completed -->",
                finding=closure.finding,
                head_sha=HEAD,
            ),
            author="maintenance-bot",
            created_at=(OBSERVED_AT + timedelta(seconds=1)).isoformat(),
        )
    )
    store = LifecycleStore(tmp_path / "state")
    store.enqueue(_intent(pr_number=7))
    monkeypatch.setattr(github_api, "resolve_pr", lambda *a, **k: _pr_payload())
    aggregate = _batch(unresolved_threads=(thread,)).observed[7]
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *a, **k: github_api.ObservationReadResult.observed([]),
    )
    from agentic_pr_dash import lifecycle_workflow
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow, _ResolvedPR

    evaluate = lifecycle_workflow.evaluate_pr_snapshot

    def addressed_observation(**kwargs):
        observation = evaluate(**kwargs)
        return observation.model_copy(
            update={
                "addressed_thread_ids": [thread.node_id],
                "unaddressed_thread_ids": [],
            }
        )

    monkeypatch.setattr(
        lifecycle_workflow, "evaluate_pr_snapshot", addressed_observation
    )

    observed = await LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=ledger,
        maintenance_author="maintenance-bot",
    )._observe(
        store.list_intents()[0],
        _ResolvedPR(_pr_payload(), REPOSITORY, 7, HEAD),
        aggregate,
    )

    assert observed is not None
    assert observed.observation.addressed_thread_ids == [thread.node_id]
    assert observed.pr.review_comments == []


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
        lambda number, fields, cwd=None, force=False, **kwargs: _pr_payload(),
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
        lambda number, fields, cwd=None, force=False, **kwargs: _pr_payload(),
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


def test_changes_requested_is_an_actionable_maintenance_blocker() -> None:
    from agentic_pr_dash.lifecycle_workflow import _pr_data, _ResolvedPR
    from agentic_pr_dash.maintenance import blockers_for_pr

    pr = _pr_data(
        _ResolvedPR(_pr_payload(), REPOSITORY, 7, HEAD),
        _batch(review_decision="CHANGES_REQUESTED").observed[7],
        "/tmp/worktree",
    )
    assert blockers_for_pr(pr) == ["changes_requested"]


def test_lifecycle_settlement_key_ignores_ci_connection_order() -> None:
    from dataclasses import replace

    from agentic_pr_dash._maintenance.review_settlement import evaluate_pr_snapshot
    from agentic_pr_dash.lifecycle_workflow import (
        _lifecycle_settlement_key,
        _pr_data,
        _ResolvedPR,
    )

    first = _batch(
        ci_checks=(
            CICheck(name="z", status="completed", conclusion="success"),
            CICheck(name="a", status="completed", conclusion="success"),
        )
    ).observed[7]
    second = replace(first, ci_checks=tuple(reversed(first.ci_checks)))
    resolved = _ResolvedPR(_pr_payload(), REPOSITORY, 7, HEAD)
    pr1 = _pr_data(resolved, first, "/tmp/worktree")
    pr2 = _pr_data(resolved, second, "/tmp/worktree")
    observation = evaluate_pr_snapshot(
        pr=pr1,
        policy=_policy(),
        ledger=_ledger(),
        threads=(),
        deferrals={},
        review_observation=github_api.ObservationReadResult.observed([]),
    )
    assert _lifecycle_settlement_key(observation, pr1) == _lifecycle_settlement_key(
        observation, pr2
    )


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
        lambda number, fields, cwd=None, force=False, **kwargs: _pr_payload(),
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


@pytest.mark.parametrize(
    ("claim_state", "expected_dispatches"),
    [("active", 0), ("released", 1), ("stale", 1)],
)
@pytest.mark.asyncio
async def test_lifecycle_dispatch_respects_claim_exclusion_and_adoption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    claim_state: str,
    expected_dispatches: int,
) -> None:
    from agentic_pr_dash import coordinator
    from agentic_pr_dash.lifecycle_workflow import (
        LifecycleWorkflow,
        _pr_data,
        _ResolvedPR,
    )

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv(
        "AGENTIC_PR_DASH_COORDINATOR_STORE", str(tmp_path / "claims.jsonl")
    )
    monkeypatch.setattr(
        coordinator, "worktree_has_dirty_or_unpushed_changes", lambda path: False
    )
    intent = _intent(pr_number=7).model_copy(update={"worktree_path": str(worktree)})
    observed = _batch(
        ci_checks=(CICheck(name="tests", status="completed", conclusion="failure"),)
    ).observed[7]
    claimed_pr = _pr_data(
        _ResolvedPR(_pr_payload(), REPOSITORY, 7, HEAD),
        observed,
        str(worktree),
    )
    claim = coordinator.claim_pr(
        claimed_pr,
        session_id="live-session",
        pid=os.getpid(),
        agent="codex",
        lease_seconds=0 if claim_state == "stale" else 300,
    )
    if claim_state == "released":
        coordinator.release_claim(claim, "live-session", "session_end")

    store = LifecycleStore(tmp_path / "state")
    store.enqueue(intent)
    monkeypatch.setattr(github_api, "resolve_pr", lambda *args, **kwargs: _pr_payload())
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
        calls = 0

        async def dispatch_pr_maintenance(self, pr: PRData) -> None:
            self.calls += 1

    dispatch = DispatchProbe()
    workflow = LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=_ledger(),
        orchestrator=dispatch,
    )

    await workflow.drain()

    assert dispatch.calls == expected_dispatches


@pytest.mark.asyncio
async def test_lifecycle_dispatch_defers_to_intent_live_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agentic_pr_dash import session_registry
    from agentic_pr_dash.lifecycle_workflow import _dispatch_allowed

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    intent = _intent(pr_number=7).model_copy(
        update={"worktree_path": str(worktree), "session_id": "live-session"}
    )

    class LiveSession:
        session_id = "live-session"

    monkeypatch.setattr(
        session_registry,
        "active_sessions_for_worktree",
        lambda *a, **k: [LiveSession()],
    )

    observed = _batch().observed[7]
    from agentic_pr_dash.lifecycle_workflow import _pr_data, _ResolvedPR

    pr = _pr_data(
        _ResolvedPR(_pr_payload(), REPOSITORY, 7, HEAD), observed, str(worktree)
    )
    assert not _dispatch_allowed(pr, intent)


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
        lambda number, fields, cwd=None, force=False, **kwargs: _pr_payload(),
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
async def test_equal_aggregate_counts_with_changed_settlement_key_reset_stability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Clock:
        current = OBSERVED_AT

        def __call__(self) -> datetime:
            return self.current

    clock = Clock()
    store = LifecycleStore(tmp_path / "state")
    store.enqueue(_intent(pr_number=7))
    monkeypatch.setattr(github_api, "resolve_pr", lambda *a, **k: _pr_payload())
    observations = iter(
        (
            _batch(
                ci_checks=(
                    CICheck(name="tests-a", status="completed", conclusion="success"),
                )
            ),
            _batch(
                ci_checks=(
                    CICheck(name="tests-b", status="completed", conclusion="success"),
                )
            ),
        )
    )
    monkeypatch.setattr(
        github_api,
        "collect_pr_maintenance_snapshots",
        lambda *a, **k: next(observations),
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *a, **k: github_api.ObservationReadResult.observed([]),
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(
        store,
        policy=_policy(),
        ledger=_ledger(),
        now=clock,
        stabilization_interval=timedelta(0),
    )
    await workflow.drain()
    clock.current += timedelta(seconds=30)
    await workflow.drain()

    record = store.list_intents()[0]
    snapshot = store.read_snapshot(
        MaintenanceTargetV1.exact(record.canonical_key), now=clock.current
    ).snapshot
    assert snapshot is not None
    assert snapshot.stable_observation_count == 1
    assert not snapshot.settled


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

    def resolve(number, fields, cwd=None, force=False, **kwargs):
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
async def test_orchestrator_refresh_drains_lifecycle_after_releasing_refresh_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentic_pr_dash.orchestrator import Orchestrator

    class DrainProbe:
        def __init__(self) -> None:
            self.orchestrator: Orchestrator | None = None
            self.calls = 0

        async def drain(self) -> None:
            assert self.orchestrator is not None
            assert not self.orchestrator._refresh_lock.locked()
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


def test_invalid_configured_review_context_raises(tmp_path: Path) -> None:
    from agentic_pr_dash.lifecycle_workflow import load_review_context_for_worktree

    policy_path = tmp_path / "config" / "review-policy.yaml"
    policy_path.parent.mkdir()
    policy_path.write_text("not: [valid", encoding="utf-8")
    ledger_path = tmp_path / ".agentic-review" / "ledger.json"
    ledger_path.parent.mkdir()
    ledger_path.write_text("{}", encoding="utf-8")

    with pytest.raises(Exception, match="review context"):
        load_review_context_for_worktree(tmp_path)


@pytest.mark.parametrize("configured", ("policy", "ledger"))
def test_partially_configured_review_context_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    from agentic_pr_dash.lifecycle_workflow import load_review_context_for_worktree

    if configured == "policy":
        monkeypatch.setenv("AGENTIC_PR_DASH_REVIEW_POLICY", "policy.yaml")
    else:
        monkeypatch.setenv("AGENTIC_PR_DASH_REVIEW_LEDGER", "ledger.json")

    with pytest.raises(Exception, match="review context"):
        load_review_context_for_worktree(tmp_path)


@pytest.mark.asyncio
async def test_policy_neutral_context_matches_repository_case_insensitively(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=7).model_copy(update={"repository": "acme/widget"})
    store.enqueue(intent)
    monkeypatch.setattr(github_api, "resolve_pr", lambda *a, **k: _pr_payload())
    monkeypatch.setattr(
        github_api, "collect_pr_maintenance_snapshots", lambda *a, **k: _batch()
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *a, **k: github_api.ObservationReadResult.observed([]),
    )
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    result = await LifecycleWorkflow(store, context_loader=lambda record: None).drain()

    assert result.progressed == 1
    snapshot = store.read_snapshot(
        MaintenanceTargetV1.exact(store.list_intents()[0].canonical_key),
        now=OBSERVED_AT,
    ).snapshot
    assert snapshot is not None
    assert MaintenanceBlockerV1.REVIEW_FINDINGS not in snapshot.blockers


class _Clock:
    def __init__(self, current: datetime = OBSERVED_AT) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


def _key(*, head_sha: str = HEAD, pr_number: int = 7) -> MaintenanceKeyV1:
    return MaintenanceKeyV1(
        repository=REPOSITORY,
        pr_number=pr_number,
        head_sha=head_sha,
        workflow_type="pr-maintenance",
    )


def _settled_snapshot(key: MaintenanceKeyV1) -> MaintenanceSnapshotV1:
    """Build a pre-review-watch settled snapshot, as older releases persisted."""

    return MaintenanceSnapshotV1(
        key=key,
        observed_at=OBSERVED_AT,
        observation_health=ObservationHealthV1.HEALTHY,
        blockers=(),
        next_actions=(),
        required_ci_state=RequiredCIStateV1.PASSING,
        mergeability=MergeabilityStateV1.MERGEABLE,
        review_state=ReviewStateV1.CLEAN,
        policy_unsettled_finding_count=0,
        raw_unresolved_thread_count=0,
        unaddressed_thread_count=0,
        stable_observation_count=2,
        stable_observation_first_at=OBSERVED_AT - timedelta(seconds=30),
        stable_observation_last_at=OBSERVED_AT,
        settled=True,
    )


def _observe_clean_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_api, "resolve_pr", lambda *a, **k: _pr_payload())
    monkeypatch.setattr(
        github_api, "collect_pr_maintenance_snapshots", lambda *a, **k: _batch()
    )
    monkeypatch.setattr(
        github_api,
        "get_review_submissions_observation",
        lambda *a, **k: github_api.ObservationReadResult.observed([]),
    )


@pytest.mark.asyncio
async def test_closed_pr_intent_becomes_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    store.enqueue(_intent(pr_number=7))
    monkeypatch.setattr(
        github_api, "resolve_pr", lambda *a, **k: {**_pr_payload(), "state": "CLOSED"}
    )
    observations = 0

    def collect(*args: object, **kwargs: object) -> object:
        nonlocal observations
        observations += 1
        return _batch()

    monkeypatch.setattr(github_api, "collect_pr_maintenance_snapshots", collect)

    class DispatchProbe:
        calls = 0

        async def dispatch_pr_maintenance(self, pr: PRData) -> None:
            self.calls += 1

    dispatch = DispatchProbe()
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(
        store, policy=_policy(), ledger=_ledger(), orchestrator=dispatch
    )
    result = await workflow.drain()
    repeated = await workflow.drain()

    assert result.progressed == 1
    assert repeated.examined == 0
    assert observations == 0
    assert dispatch.calls == 0
    assert store.list_intents()[0].state is IntentLifecycleStateV1.SETTLED


@pytest.mark.asyncio
async def test_remote_head_change_adopts_a_replacement_intent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LifecycleStore(tmp_path / "state")
    old_head = "b" * 40
    store.enqueue(_intent(pr_number=7, head_sha=old_head))
    monkeypatch.setattr(github_api, "resolve_pr", lambda *a, **k: _pr_payload())
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    await LifecycleWorkflow(store, policy=_policy(), ledger=_ledger()).drain()

    records = {record.intent.head_sha: record for record in store.list_intents()}
    assert records[old_head].state is IntentLifecycleStateV1.SETTLED
    assert records[HEAD].state is IntentLifecycleStateV1.PENDING
    assert records[HEAD].intent.pr_number == 7


@pytest.mark.asyncio
async def test_failed_observation_persists_a_due_review_watch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = _Clock()
    store = LifecycleStore(tmp_path / "state")
    store.enqueue(_intent(pr_number=7))
    _observe_clean_pr(monkeypatch)
    from agentic_pr_dash.lifecycle_workflow import LifecycleWorkflow

    workflow = LifecycleWorkflow(store, policy=_policy(), ledger=_ledger(), now=clock)
    await workflow.drain()
    clock.current += timedelta(seconds=30)
    await workflow.drain()

    target = MaintenanceTargetV1.exact(store.list_intents()[0].canonical_key)
    armed = store.read_snapshot(
        target, max_age_seconds=10**12, now=clock.current
    ).snapshot
    assert armed is not None and armed.review_watch is not None

    def unavailable(*args: object, **kwargs: object) -> None:
        raise RuntimeError("github is unavailable")

    monkeypatch.setattr(github_api, "collect_pr_maintenance_snapshots", unavailable)
    clock.current = armed.review_watch.next_check_at
    await workflow.drain()

    due = store.read_snapshot(
        target, max_age_seconds=10**12, now=clock.current
    ).snapshot
    assert due is not None and due.review_watch is not None
    assert due.review_watch.status is ReviewWatchStatusV1.DUE
    assert due.review_watch.next_check_at == armed.review_watch.next_check_at
    assert due.review_watch.interval_index == armed.review_watch.interval_index


@pytest.mark.asyncio
async def test_settled_intents_without_a_watch_are_rearmed_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = _Clock()
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=7)
    key = _key()
    store.enqueue(intent)
    store.promote_intent(intent, key, snapshot=_settled_snapshot(key))
    store.settle_intent(intent, key)
    _observe_clean_pr(monkeypatch)
    from agentic_pr_dash.lifecycle_workflow import (
        REVIEW_WATCH_MIGRATION,
        LifecycleWorkflow,
    )

    workflow = LifecycleWorkflow(store, policy=_policy(), ledger=_ledger(), now=clock)
    await workflow.drain()
    clock.current += timedelta(seconds=30)
    await workflow.drain()

    record = store.list_intents()[0]
    assert record.state is IntentLifecycleStateV1.PROMOTED
    snapshot = store.read_snapshot(
        MaintenanceTargetV1.exact(key), max_age_seconds=10**12, now=clock.current
    ).snapshot
    assert snapshot is not None and snapshot.review_watch is not None
    assert store.migration_completed(REVIEW_WATCH_MIGRATION)

    store.settle_intent(record.intent, key, snapshot=_settled_snapshot(key))
    clock.current += timedelta(seconds=30)
    await workflow.drain()

    assert store.list_intents()[0].state is IntentLifecycleStateV1.SETTLED
