from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agentic_pr_dash import delivery_checklist
from agentic_pr_dash.delivery_checklist import project_checklist, render_checklist
from agentic_pr_dash.lifecycle_models import (
    ChecklistItemIdV1,
    ChecklistItemStateV1,
    DeliveryChecklistItemV1,
    DeliveryChecklistV1,
    LocalDeliveryEvidenceV1,
    MaintenanceKeyV1,
    MaintenanceSnapshotReadResultV1,
    MaintenanceSnapshotV1,
    MergeabilityStateV1,
    ObservationHealthV1,
    RequiredCIStateV1,
    ReviewStateV1,
    SnapshotReadStatusV1,
)

ORDERED_ITEM_IDS = (
    ChecklistItemIdV1.TASK_CRITERIA,
    ChecklistItemIdV1.IMPLEMENTATION,
    ChecklistItemIdV1.CODE_QUALITY,
    ChecklistItemIdV1.TASK_VALIDATION,
    ChecklistItemIdV1.EXACT_HEAD_PUSHED,
    ChecklistItemIdV1.PULL_REQUEST_OPEN,
    ChecklistItemIdV1.REQUIRED_CI,
    ChecklistItemIdV1.MERGEABILITY,
    ChecklistItemIdV1.REVIEW_SETTLEMENT,
    ChecklistItemIdV1.DISCUSSION_SETTLEMENT,
)


def _item(
    item_id: ChecklistItemIdV1,
    state: ChecklistItemStateV1 = ChecklistItemStateV1.SATISFIED,
) -> DeliveryChecklistItemV1:
    return DeliveryChecklistItemV1(
        item_id=item_id,
        state=state,
        authority="test",
        summary=f"{item_id.value} evidence",
        next_actions=() if state is ChecklistItemStateV1.SATISFIED else ("act",),
    )


def _checklist(
    *,
    items: tuple[DeliveryChecklistItemV1, ...] | None = None,
    complete: bool = True,
) -> DeliveryChecklistV1:
    return DeliveryChecklistV1(
        key=MaintenanceKeyV1(
            repository="Boundless-Studios/example",
            pr_number=42,
            head_sha="a" * 40,
            workflow_type="pr_convergence",
        ),
        observed_at=datetime(2026, 8, 28, tzinfo=UTC),
        items=items or tuple(_item(item_id) for item_id in ORDERED_ITEM_IDS),
        complete=complete,
    )


def test_checklist_contract_has_one_stable_order() -> None:
    checklist = _checklist()

    assert tuple(item.item_id for item in checklist.items) == ORDERED_ITEM_IDS
    assert tuple(state.value for state in ChecklistItemStateV1) == (
        "required",
        "satisfied",
        "blocked",
        "unknown",
    )


def test_complete_requires_every_item_to_be_satisfied() -> None:
    items = tuple(
        _item(
            item_id,
            ChecklistItemStateV1.REQUIRED
            if item_id is ChecklistItemIdV1.CODE_QUALITY
            else ChecklistItemStateV1.SATISFIED,
        )
        for item_id in ORDERED_ITEM_IDS
    )

    with pytest.raises(ValidationError, match="complete checklist"):
        _checklist(items=items, complete=True)


def test_missing_local_evidence_remains_unknown() -> None:
    items = tuple(
        _item(
            item_id,
            ChecklistItemStateV1.UNKNOWN
            if item_id is ChecklistItemIdV1.TASK_CRITERIA
            else ChecklistItemStateV1.SATISFIED,
        )
        for item_id in ORDERED_ITEM_IDS
    )

    checklist = _checklist(items=items, complete=False)

    assert checklist.items[0].state is ChecklistItemStateV1.UNKNOWN
    assert not checklist.complete


def test_checklist_rejects_duplicate_or_reordered_items() -> None:
    reordered = tuple(_item(item_id) for item_id in reversed(ORDERED_ITEM_IDS))

    with pytest.raises(ValidationError, match="canonical order"):
        _checklist(items=reordered)


def test_checklist_serializes_exact_maintenance_identity() -> None:
    payload = _checklist().model_dump(mode="json")

    assert payload["key"] == {
        "repository": "Boundless-Studios/example",
        "pr_number": 42,
        "head_sha": "a" * 40,
        "workflow_type": "pr_convergence",
    }


def _local_evidence() -> LocalDeliveryEvidenceV1:
    return LocalDeliveryEvidenceV1(
        repository="Boundless-Studios/example",
        head_sha="a" * 40,
        observed_at=datetime(2026, 8, 28, tzinfo=UTC),
        items=tuple(_item(item_id) for item_id in ORDERED_ITEM_IDS[:5]),
    )


def _snapshot(**updates: object) -> MaintenanceSnapshotV1:
    values: dict[str, object] = {
        "key": _checklist().key,
        "observed_at": datetime(2026, 8, 28, tzinfo=UTC),
        "observation_health": ObservationHealthV1.HEALTHY,
        "blockers": (),
        "next_actions": (),
        "required_ci_state": RequiredCIStateV1.PASSING,
        "mergeability": MergeabilityStateV1.MERGEABLE,
        "review_state": ReviewStateV1.CLEAN,
        "policy_unsettled_finding_count": 0,
        "raw_unresolved_thread_count": 0,
        "unaddressed_thread_count": 0,
        "stable_observation_count": 2,
        "stable_observation_first_at": datetime(2026, 8, 28, tzinfo=UTC),
        "stable_observation_last_at": datetime(2026, 8, 28, tzinfo=UTC),
        "settled": True,
    }
    values.update(updates)
    return MaintenanceSnapshotV1(**values)


def test_projection_combines_local_and_remote_exact_head_evidence() -> None:
    checklist = project_checklist(local=_local_evidence(), snapshot=_snapshot())

    assert checklist.complete
    assert checklist.key == _snapshot().key
    assert (
        tuple(item.state for item in checklist.items)
        == (ChecklistItemStateV1.SATISFIED,) * 10
    )


def test_projection_requires_lifecycle_stabilization_before_completion() -> None:
    snapshot = _snapshot(
        settled=False,
        stable_observation_count=1,
        next_actions=("retry_observation",),
    )

    checklist = project_checklist(local=_local_evidence(), snapshot=snapshot)

    review = next(
        item
        for item in checklist.items
        if item.item_id is ChecklistItemIdV1.REVIEW_SETTLEMENT
    )
    assert review.state is ChecklistItemStateV1.REQUIRED
    assert review.next_actions == ("wait for lifecycle stabilization",)
    assert not checklist.complete


def test_partial_observation_marks_clean_review_evidence_unknown() -> None:
    snapshot = _snapshot(
        observation_health=ObservationHealthV1.PARTIAL,
        settled=False,
        stable_observation_count=0,
    )

    checklist = project_checklist(local=_local_evidence(), snapshot=snapshot)

    review = next(
        item
        for item in checklist.items
        if item.item_id is ChecklistItemIdV1.REVIEW_SETTLEMENT
    )
    assert review.state is ChecklistItemStateV1.UNKNOWN
    assert review.next_actions == ("retry remote review observation",)


def test_pending_review_recommends_obtaining_required_review() -> None:
    snapshot = _snapshot(
        review_state=ReviewStateV1.PENDING,
        settled=False,
        stable_observation_count=0,
    )

    checklist = project_checklist(local=_local_evidence(), snapshot=snapshot)

    review = next(
        item
        for item in checklist.items
        if item.item_id is ChecklistItemIdV1.REVIEW_SETTLEMENT
    )
    assert review.state is ChecklistItemStateV1.REQUIRED
    assert review.next_actions == ("wait for or obtain the required review",)


def test_missing_snapshot_is_visible_without_erasing_local_progress() -> None:
    checklist = project_checklist(local=_local_evidence(), snapshot=None)

    assert not checklist.complete
    assert checklist.key is None
    assert (
        tuple(item.state for item in checklist.items[:5])
        == (ChecklistItemStateV1.SATISFIED,) * 5
    )
    assert (
        tuple(item.state for item in checklist.items[5:])
        == (ChecklistItemStateV1.UNKNOWN,) * 5
    )


def test_projection_is_deterministic_for_the_same_evidence() -> None:
    local = _local_evidence()

    first = project_checklist(local=local, snapshot=None)
    second = project_checklist(local=local, snapshot=None)

    assert first == second
    assert first.observed_at == local.observed_at


def test_requested_identity_blocks_mismatched_local_evidence_without_snapshot() -> None:
    target = _checklist().key.model_copy(update={"head_sha": "b" * 40})

    checklist = project_checklist(local=_local_evidence(), snapshot=None, target=target)

    assert (
        tuple(item.state for item in checklist.items[:5])
        == (ChecklistItemStateV1.BLOCKED,) * 5
    )
    assert checklist.items[0].summary == (
        "local evidence does not match the requested exact head"
    )


@pytest.mark.parametrize(
    ("field", "value", "item_id", "expected"),
    (
        (
            "required_ci_state",
            RequiredCIStateV1.PENDING,
            ChecklistItemIdV1.REQUIRED_CI,
            ChecklistItemStateV1.REQUIRED,
        ),
        (
            "required_ci_state",
            RequiredCIStateV1.FAILING,
            ChecklistItemIdV1.REQUIRED_CI,
            ChecklistItemStateV1.BLOCKED,
        ),
        (
            "mergeability",
            MergeabilityStateV1.CONFLICTING,
            ChecklistItemIdV1.MERGEABILITY,
            ChecklistItemStateV1.BLOCKED,
        ),
        (
            "review_state",
            ReviewStateV1.CHANGES_REQUESTED,
            ChecklistItemIdV1.REVIEW_SETTLEMENT,
            ChecklistItemStateV1.BLOCKED,
        ),
    ),
)
def test_remote_states_map_to_their_own_checklist_item(
    field: str,
    value: object,
    item_id: ChecklistItemIdV1,
    expected: ChecklistItemStateV1,
) -> None:
    snapshot = _snapshot().model_copy(
        update={field: value, "settled": False, "stable_observation_count": 0}
    )

    checklist = project_checklist(local=_local_evidence(), snapshot=snapshot)

    item = next(item for item in checklist.items if item.item_id is item_id)
    assert item.state is expected


def test_replied_unresolved_thread_is_addressed_but_uncommented_thread_blocks() -> None:
    addressed = _snapshot().model_copy(
        update={"raw_unresolved_thread_count": 1, "settled": True}
    )
    unaddressed = addressed.model_copy(
        update={"unaddressed_thread_count": 1, "settled": False}
    )

    addressed_item = project_checklist(
        local=_local_evidence(), snapshot=addressed
    ).items[-1]
    unaddressed_item = project_checklist(
        local=_local_evidence(), snapshot=unaddressed
    ).items[-1]

    assert addressed_item.state is ChecklistItemStateV1.SATISFIED
    assert "1 unresolved, 0 unaddressed" in addressed_item.summary
    assert unaddressed_item.state is ChecklistItemStateV1.BLOCKED


def test_compact_render_shows_one_line_per_item_and_next_action() -> None:
    checklist = project_checklist(local=_local_evidence(), snapshot=None)

    rendered = render_checklist(checklist)

    assert len(rendered.splitlines()) == 11
    assert "[satisfied] task_criteria" in rendered
    assert "[unknown] required_ci" in rendered
    assert rendered.splitlines()[-1].startswith("next: ")


def test_cli_reads_local_evidence_and_reports_missing_snapshot_as_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    local_path = tmp_path / "local.json"
    local_path.write_text(_local_evidence().model_dump_json(), encoding="utf-8")
    seen = {}

    def fake_read(target, **kwargs):
        seen["target"] = target
        seen["root"] = kwargs["root"]
        return MaintenanceSnapshotReadResultV1(status=SnapshotReadStatusV1.MISSING)

    monkeypatch.setattr(delivery_checklist, "read_maintenance_snapshot", fake_read)

    rc = delivery_checklist.main(
        [
            "--repository",
            "Boundless-Studios/example",
            "--pr-number",
            "42",
            "--head-sha",
            "a" * 40,
            "--workflow-type",
            "pr_convergence",
            "--state-root",
            str(tmp_path / "state"),
            "--local-evidence",
            str(local_path),
            "--json",
        ]
    )

    payload = __import__("json").loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["complete"] is False
    assert payload["items"][0]["state"] == "satisfied"
    assert payload["items"][5]["state"] == "unknown"
    assert seen["target"].exact_key.pr_number == 42
    assert seen["root"] == str(tmp_path / "state")
