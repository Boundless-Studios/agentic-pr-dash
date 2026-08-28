"""Pure projection and compact rendering of delivery completion evidence."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from .lifecycle_models import (
    ChecklistItemIdV1,
    ChecklistItemStateV1,
    DeliveryChecklistItemV1,
    DeliveryChecklistV1,
    LocalDeliveryEvidenceV1,
    MaintenanceBlockerV1,
    MaintenanceKeyV1,
    MaintenanceSnapshotV1,
    MaintenanceTargetV1,
    MergeabilityStateV1,
    ObservationHealthV1,
    RequiredCIStateV1,
    ReviewStateV1,
    SnapshotReadStatusV1,
)
from .lifecycle_store import read_maintenance_snapshot


def _item(
    item_id: ChecklistItemIdV1,
    state: ChecklistItemStateV1,
    authority: str,
    summary: str,
    *next_actions: str,
) -> DeliveryChecklistItemV1:
    return DeliveryChecklistItemV1(
        item_id=item_id,
        state=state,
        authority=authority,
        summary=summary,
        next_actions=next_actions,
    )


def _unknown_remote_items() -> tuple[DeliveryChecklistItemV1, ...]:
    return tuple(
        _item(
            item_id,
            ChecklistItemStateV1.UNKNOWN,
            "agentic-pr-dash lifecycle snapshot",
            "no exact-head remote observation is available",
            "wait for asynchronous lifecycle observation",
        )
        for item_id in (
            ChecklistItemIdV1.PULL_REQUEST_OPEN,
            ChecklistItemIdV1.REQUIRED_CI,
            ChecklistItemIdV1.MERGEABILITY,
            ChecklistItemIdV1.REVIEW_SETTLEMENT,
            ChecklistItemIdV1.DISCUSSION_SETTLEMENT,
        )
    )


def _ci_item(snapshot: MaintenanceSnapshotV1) -> DeliveryChecklistItemV1:
    if MaintenanceBlockerV1.REQUIRED_CI_FAILED in snapshot.blockers:
        state = ChecklistItemStateV1.BLOCKED
    else:
        state = {
            RequiredCIStateV1.PASSING: ChecklistItemStateV1.SATISFIED,
            RequiredCIStateV1.NOT_REQUIRED: ChecklistItemStateV1.SATISFIED,
            RequiredCIStateV1.PENDING: ChecklistItemStateV1.REQUIRED,
            RequiredCIStateV1.FAILING: ChecklistItemStateV1.BLOCKED,
            RequiredCIStateV1.UNKNOWN: ChecklistItemStateV1.UNKNOWN,
            RequiredCIStateV1.UNAVAILABLE: ChecklistItemStateV1.UNKNOWN,
        }[snapshot.required_ci_state]
    action = {
        ChecklistItemStateV1.REQUIRED: "wait for required CI",
        ChecklistItemStateV1.BLOCKED: "fix failing required CI",
        ChecklistItemStateV1.UNKNOWN: "retry remote observation",
    }.get(state)
    return _item(
        ChecklistItemIdV1.REQUIRED_CI,
        state,
        "GitHub required checks",
        f"required CI is {snapshot.required_ci_state.value}",
        *((action,) if action else ()),
    )


def _mergeability_item(snapshot: MaintenanceSnapshotV1) -> DeliveryChecklistItemV1:
    state = {
        MergeabilityStateV1.MERGEABLE: ChecklistItemStateV1.SATISFIED,
        MergeabilityStateV1.CONFLICTING: ChecklistItemStateV1.BLOCKED,
        MergeabilityStateV1.UNKNOWN: ChecklistItemStateV1.UNKNOWN,
        MergeabilityStateV1.UNAVAILABLE: ChecklistItemStateV1.UNKNOWN,
    }[snapshot.mergeability]
    action = (
        "resolve the merge conflict"
        if state is ChecklistItemStateV1.BLOCKED
        else "retry remote observation"
    )
    return _item(
        ChecklistItemIdV1.MERGEABILITY,
        state,
        "GitHub mergeability",
        f"mergeability is {snapshot.mergeability.value}",
        *((action,) if state is not ChecklistItemStateV1.SATISFIED else ()),
    )


def _review_item(snapshot: MaintenanceSnapshotV1) -> DeliveryChecklistItemV1:
    if snapshot.policy_unsettled_finding_count:
        state = ChecklistItemStateV1.BLOCKED
    elif MaintenanceBlockerV1.REVIEW_FINDINGS in snapshot.blockers:
        state = ChecklistItemStateV1.BLOCKED
    elif snapshot.review_state is ReviewStateV1.PENDING:
        state = ChecklistItemStateV1.REQUIRED
    elif snapshot.observation_health is ObservationHealthV1.PARTIAL:
        state = ChecklistItemStateV1.UNKNOWN
    else:
        state = {
            ReviewStateV1.CLEAN: ChecklistItemStateV1.SATISFIED,
            ReviewStateV1.CHANGES_REQUESTED: ChecklistItemStateV1.BLOCKED,
            ReviewStateV1.PENDING: ChecklistItemStateV1.REQUIRED,
            ReviewStateV1.UNKNOWN: ChecklistItemStateV1.UNKNOWN,
            ReviewStateV1.UNAVAILABLE: ChecklistItemStateV1.UNKNOWN,
        }[snapshot.review_state]
    stabilization_pending = (
        state is ChecklistItemStateV1.SATISFIED
        and snapshot.required_ci_state
        in {RequiredCIStateV1.PASSING, RequiredCIStateV1.NOT_REQUIRED}
        and snapshot.mergeability is MergeabilityStateV1.MERGEABLE
        and snapshot.unaddressed_thread_count == 0
        and not snapshot.settled
    )
    if stabilization_pending:
        state = ChecklistItemStateV1.REQUIRED
    watch_missing = (
        state is ChecklistItemStateV1.SATISFIED and snapshot.review_watch is None
    )
    if watch_missing:
        state = ChecklistItemStateV1.REQUIRED
    if watch_missing:
        action = "arm durable late-review monitoring"
    elif stabilization_pending:
        action = "wait for lifecycle stabilization"
    elif state is ChecklistItemStateV1.BLOCKED:
        action = "address or disposition review findings"
    elif snapshot.observation_health is ObservationHealthV1.PARTIAL:
        action = "retry remote review observation"
    elif snapshot.review_state is ReviewStateV1.PENDING:
        action = "wait for or obtain the required review"
    elif state is ChecklistItemStateV1.UNKNOWN:
        action = "retry remote review observation"
    else:
        action = None
    watch_summary = (
        "review watch is unarmed"
        if snapshot.review_watch is None
        else f"review watch is {snapshot.review_watch.status.value}; "
        f"next check {snapshot.review_watch.next_check_at.isoformat()}"
    )
    return _item(
        ChecklistItemIdV1.REVIEW_SETTLEMENT,
        state,
        "agent-review-coordinator and GitHub review",
        f"review is {snapshot.review_state.value}; "
        f"{snapshot.policy_unsettled_finding_count} policy finding(s) unsettled; "
        f"{watch_summary}",
        *((action,) if action else ()),
    )


def _discussion_item(snapshot: MaintenanceSnapshotV1) -> DeliveryChecklistItemV1:
    state = (
        ChecklistItemStateV1.SATISFIED
        if snapshot.unaddressed_thread_count == 0
        else ChecklistItemStateV1.BLOCKED
    )
    return _item(
        ChecklistItemIdV1.DISCUSSION_SETTLEMENT,
        state,
        "GitHub review threads",
        f"{snapshot.raw_unresolved_thread_count} unresolved, "
        f"{snapshot.unaddressed_thread_count} unaddressed",
        *(
            ("reply to or resolve every unaddressed thread",)
            if state is ChecklistItemStateV1.BLOCKED
            else ()
        ),
    )


def project_checklist(
    *,
    local: LocalDeliveryEvidenceV1 | None,
    snapshot: MaintenanceSnapshotV1 | None,
    target: MaintenanceKeyV1 | None = None,
    observed_at: datetime | None = None,
) -> DeliveryChecklistV1:
    """Compose provider-owned local evidence with one exact-head PR snapshot."""

    snapshot_matches_target = (
        snapshot is None
        or target is None
        or (
            snapshot.key.normalized_repository == target.normalized_repository
            and snapshot.key.pr_number == target.pr_number
            and snapshot.key.head_sha == target.head_sha
            and snapshot.key.workflow_type == target.workflow_type
        )
    )
    if observed_at is not None:
        projection_time = observed_at
    elif snapshot is not None:
        projection_time = snapshot.observed_at
    elif local is not None:
        projection_time = local.observed_at
    else:
        projection_time = datetime.fromtimestamp(0, tz=UTC)
    if local is None:
        local_items = tuple(
            _item(
                item_id,
                ChecklistItemStateV1.UNKNOWN,
                "repository adapter",
                "local evidence is unavailable",
                "restore local lifecycle evidence",
            )
            for item_id in tuple(ChecklistItemIdV1)[:5]
        )
    else:
        local_items = local.items

    expected_key = target or (snapshot.key if snapshot is not None else None)
    if (
        local is not None
        and expected_key is not None
        and (
            local.repository.casefold() != expected_key.repository.casefold()
            or local.head_sha != expected_key.head_sha
        )
    ):
        local_items = tuple(
            item.model_copy(
                update={
                    "state": ChecklistItemStateV1.BLOCKED,
                    "summary": "local evidence does not match the requested exact head",
                    "next_actions": ("refresh evidence for the same exact head",),
                }
            )
            for item in local.items
        )

    if snapshot is None or not snapshot_matches_target:
        remote_items = _unknown_remote_items()
        key = None
    else:
        key = snapshot.key
        if snapshot.observation_health in {
            ObservationHealthV1.UNKNOWN,
            ObservationHealthV1.UNAVAILABLE,
        }:
            remote_items = _unknown_remote_items()
        else:
            remote_items = (
                _item(
                    ChecklistItemIdV1.PULL_REQUEST_OPEN,
                    ChecklistItemStateV1.SATISFIED,
                    "agentic-pr-dash lifecycle snapshot",
                    f"PR #{key.pr_number} is open for the exact head",
                ),
                _ci_item(snapshot),
                _mergeability_item(snapshot),
                _review_item(snapshot),
                _discussion_item(snapshot),
            )

    items = local_items + remote_items
    complete = key is not None and all(
        item.state is ChecklistItemStateV1.SATISFIED for item in items
    )
    return DeliveryChecklistV1(
        key=key,
        observed_at=projection_time,
        items=items,
        complete=complete,
    )


def render_checklist(checklist: DeliveryChecklistV1) -> str:
    """Render one compact line per item and one actionable tail line."""

    lines = [
        f"[{item.state.value}] {item.item_id.value}: {_compact_text(item.summary)}"
        for item in checklist.items
    ]
    action = next(
        (
            item.next_actions[0]
            for item in checklist.items
            if item.state is not ChecklistItemStateV1.SATISFIED and item.next_actions
        ),
        "none",
    )
    lines.append(f"next: {_compact_text(action)}")
    return "\n".join(lines)


def _compact_text(value: str) -> str:
    """Collapse physical line breaks without changing other compact text."""

    return " ".join(value.splitlines())


def main(argv: list[str] | None = None) -> int:
    """Read existing evidence and print the composed checklist without mutation."""

    parser = argparse.ArgumentParser(prog="agentic-pr-dash delivery-checklist")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--workflow-type", required=True)
    parser.add_argument("--state-root")
    parser.add_argument("--local-evidence", type=Path)
    parser.add_argument("--max-snapshot-age-seconds", type=float, default=90.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    target = MaintenanceTargetV1.exact(
        key=MaintenanceKeyV1(
            repository=args.repository,
            pr_number=args.pr_number,
            head_sha=args.head_sha,
            workflow_type=args.workflow_type,
        )
    )
    result = read_maintenance_snapshot(
        target,
        root=args.state_root,
        max_age_seconds=args.max_snapshot_age_seconds,
    )
    local = None
    if args.local_evidence is not None:
        local = LocalDeliveryEvidenceV1.model_validate_json(
            args.local_evidence.read_text(encoding="utf-8")
        )
    snapshot = result.snapshot if result.status is SnapshotReadStatusV1.FRESH else None
    checklist = project_checklist(
        local=local,
        snapshot=snapshot,
        target=target.exact_key,
    )
    if args.json:
        print(json.dumps(checklist.model_dump(mode="json"), indent=2, sort_keys=True))
    else:
        print(render_checklist(checklist))
    return 0 if checklist.complete else 1
