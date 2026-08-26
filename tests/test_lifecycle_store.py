from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from stat import S_IMODE

import pytest
from pydantic import ValidationError

from agentic_pr_dash.lifecycle_models import (
    EnqueueStatusV1,
    IntentLifecycleStateV1,
    MaintenanceIntentRecordV1,
    MaintenanceIntentV1,
    MaintenanceKeyV1,
    MaintenanceSnapshotV1,
    MaintenanceTargetV1,
    ObservationHealthV1,
    RequiredCIStateV1,
    SnapshotReadStatusV1,
)
from agentic_pr_dash.lifecycle_store import (
    LifecycleStore,
    canonical_job_hash,
    enqueue_maintenance,
    ingress_identity_hash,
    mark_maintenance_intent_no_pr,
    read_maintenance_snapshot,
    write_maintenance_snapshot,
)


UTC = timezone.utc
OBSERVED_AT = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)


def _intent(
    *,
    repository: str = "Acme/Widget",
    head_sha: str = "a" * 40,
    workflow_type: str = "pr-maintenance",
    reason: str = "post-push maintenance",
    requested_at: datetime = OBSERVED_AT,
    pr_number: int | None = 42,
) -> MaintenanceIntentV1:
    return MaintenanceIntentV1(
        repository=repository,
        pushed_ref="refs/heads/feature/thing",
        head_sha=head_sha,
        workflow_type=workflow_type,
        reason=reason,
        worktree_path="/tmp/worktree",
        session_id="session-1",
        requested_at=requested_at,
        pr_number=pr_number,
    )


def _key(
    *,
    repository: str = "Acme/Widget",
    head_sha: str = "a" * 40,
    workflow_type: str = "pr-maintenance",
    pr_number: int = 42,
) -> MaintenanceKeyV1:
    return MaintenanceKeyV1(
        repository=repository,
        pr_number=pr_number,
        head_sha=head_sha,
        workflow_type=workflow_type,
    )


def _snapshot(
    key: MaintenanceKeyV1 | None = None,
    *,
    observed_at: datetime = OBSERVED_AT,
    health: ObservationHealthV1 = ObservationHealthV1.HEALTHY,
    settled: bool = False,
    raw_unresolved_thread_count: int = 0,
) -> MaintenanceSnapshotV1:
    return MaintenanceSnapshotV1(
        key=key or _key(),
        observed_at=observed_at,
        observation_health=health,
        blockers=[],
        next_actions=[],
        required_ci_state=RequiredCIStateV1.PASSING,
        mergeability="mergeable",
        review_state="clean",
        policy_unsettled_finding_count=0,
        raw_unresolved_thread_count=raw_unresolved_thread_count,
        unaddressed_thread_count=0,
        stable_observation_count=2,
        stable_observation_first_at=observed_at - timedelta(seconds=10),
        stable_observation_last_at=observed_at,
        settled=settled,
    )


def test_repository_is_trimmed_for_display_and_casefolded_for_identity() -> None:
    intent = _intent(repository="  Acme/Widget  ")
    equivalent = _intent(repository="acme/widget")

    assert intent.repository == "Acme/Widget"
    assert intent.normalized_repository == "acme/widget"
    assert ingress_identity_hash(intent) == ingress_identity_hash(equivalent)


def test_models_reject_blank_fields_and_nonpositive_pr() -> None:
    for field in (
        "repository",
        "pushed_ref",
        "head_sha",
        "workflow_type",
        "reason",
        "worktree_path",
        "session_id",
    ):
        data = _intent().model_dump()
        data[field] = "  "
        with pytest.raises(ValidationError):
            MaintenanceIntentV1(**data)
    with pytest.raises(ValidationError):
        _intent(pr_number=0)

    key_data = _key().model_dump()
    for field in ("repository", "head_sha", "workflow_type"):
        with pytest.raises(ValidationError):
            MaintenanceKeyV1(**{**key_data, field: "  "})
    with pytest.raises(ValidationError):
        MaintenanceKeyV1(**{**key_data, "pr_number": 0})


def test_snapshot_models_are_frozen_and_extra_forbid() -> None:
    intent = _intent()
    key = _key()
    snapshot = _snapshot()

    for model in (intent, key, snapshot):
        with pytest.raises(ValidationError):
            model.__class__.model_validate({**model.model_dump(), "extra": 1})
        with pytest.raises(ValidationError):
            model.repository = "other/repo"  # type: ignore[misc]


def test_settled_snapshot_rejects_unhealthy_or_unknown_facts() -> None:
    with pytest.raises(ValidationError):
        _snapshot(health=ObservationHealthV1.UNKNOWN, settled=True)


def test_settled_snapshot_rejects_raw_unresolved_threads() -> None:
    with pytest.raises(ValidationError):
        _snapshot(raw_unresolved_thread_count=1, settled=True)


def test_snapshot_requires_explicit_settlement_evidence_fields() -> None:
    complete = _snapshot().model_dump()
    for field in (
        "observation_health",
        "blockers",
        "next_actions",
        "required_ci_state",
        "mergeability",
        "review_state",
        "policy_unsettled_finding_count",
        "raw_unresolved_thread_count",
        "unaddressed_thread_count",
        "stable_observation_count",
        "stable_observation_first_at",
        "stable_observation_last_at",
        "settled",
    ):
        missing = {key: value for key, value in complete.items() if key != field}
        with pytest.raises(ValidationError):
            MaintenanceSnapshotV1(**missing)


def test_v1_contract_exposes_only_canonical_fields_and_enum_members() -> None:
    assert MaintenanceIntentV1.model_fields["pushed_ref"].validation_alias is None
    assert MaintenanceSnapshotV1.model_fields["required_ci_state"].validation_alias is None
    assert tuple(ObservationHealthV1.__members__) == (
        "HEALTHY",
        "UNHEALTHY",
        "PARTIAL",
        "UNKNOWN",
        "UNAVAILABLE",
    )


def test_enqueue_is_duplicate_for_same_active_ingress_identity(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "state")
    first = enqueue_maintenance(_intent(), store=store)
    second = enqueue_maintenance(_intent(reason="again"), store=store)

    assert first.status is EnqueueStatusV1.ENQUEUED
    assert second.status is EnqueueStatusV1.DUPLICATE
    assert second.intent.requested_at == OBSERVED_AT


def test_same_pr_number_in_different_repositories_is_isolated(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "state")
    enqueue_maintenance(_intent(repository="one/repo"), store=store)
    other = enqueue_maintenance(_intent(repository="two/repo"), store=store)

    assert other.status is EnqueueStatusV1.ENQUEUED


def test_same_head_in_different_workflows_is_isolated(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "state")
    enqueue_maintenance(_intent(workflow_type="review"), store=store)
    other = enqueue_maintenance(_intent(workflow_type="ci"), store=store)

    assert other.status is EnqueueStatusV1.ENQUEUED
    assert canonical_job_hash(_key(workflow_type="review")) != canonical_job_hash(
        _key(workflow_type="ci")
    )


def test_no_pr_intent_reactivates_when_pr_is_known(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "state")
    first = enqueue_maintenance(_intent(pr_number=None), store=store)
    second = enqueue_maintenance(
        _intent(
            pr_number=42,
            reason="PR was created",
            requested_at=OBSERVED_AT + timedelta(minutes=1),
        ),
        store=store,
    )

    assert first.status is EnqueueStatusV1.ENQUEUED
    assert second.status is EnqueueStatusV1.REACTIVATED
    assert second.intent.pr_number == 42
    assert second.intent.reason == "PR was created"
    assert second.state is IntentLifecycleStateV1.PENDING


def test_mark_no_pr_exposes_unresolved_lookup_state(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "state")
    intent = _intent()

    enqueue_maintenance(intent, store=store)
    record = mark_maintenance_intent_no_pr(intent, store=store)

    assert record.state is IntentLifecycleStateV1.NO_PR
    assert record.intent.pr_number is None


def test_snapshot_is_fresh_at_ninety_seconds_and_stale_afterward(
    tmp_path: Path,
) -> None:
    store = LifecycleStore(tmp_path / "state")
    snapshot = _snapshot(observed_at=OBSERVED_AT)
    write_maintenance_snapshot(snapshot, store=store)

    fresh = read_maintenance_snapshot(
        MaintenanceTargetV1(key=snapshot.key),
        store=store,
        now=OBSERVED_AT + timedelta(seconds=90),
    )
    stale = read_maintenance_snapshot(
        MaintenanceTargetV1(key=snapshot.key),
        store=store,
        now=OBSERVED_AT + timedelta(seconds=90, microseconds=1),
    )

    assert fresh.status is SnapshotReadStatusV1.FRESH
    assert stale.status is SnapshotReadStatusV1.STALE


def test_corrupt_snapshot_returns_invalid(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "state")
    snapshot = _snapshot()
    write_maintenance_snapshot(snapshot, store=store)
    store.snapshot_path(snapshot.key).write_text("{not-json", encoding="utf-8")

    result = read_maintenance_snapshot(
        MaintenanceTargetV1(key=snapshot.key), store=store, now=OBSERVED_AT
    )

    assert result.status is SnapshotReadStatusV1.INVALID
    assert result.snapshot is None


def test_json_null_snapshot_returns_invalid(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "state")
    snapshot = _snapshot()
    write_maintenance_snapshot(snapshot, store=store)
    store.snapshot_path(snapshot.key).write_text("null", encoding="utf-8")

    result = read_maintenance_snapshot(
        MaintenanceTargetV1(key=snapshot.key), store=store, now=OBSERVED_AT
    )

    assert result.status is SnapshotReadStatusV1.INVALID


def test_old_head_cannot_satisfy_new_head_target(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "state")
    old = _snapshot(_key(head_sha="b" * 40))
    write_maintenance_snapshot(old, store=store)
    result = read_maintenance_snapshot(
        MaintenanceTargetV1(key=_key(head_sha="c" * 40)),
        store=store,
        now=OBSERVED_AT,
    )

    assert result.status is SnapshotReadStatusV1.MISSING
    assert result.snapshot is None


def test_unresolved_target_follows_promotion_link(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=None)
    enqueue_maintenance(intent, store=store)
    snapshot = _snapshot(_key(head_sha=intent.head_sha))
    store.promote_intent(intent, snapshot.key)
    write_maintenance_snapshot(snapshot, store=store)

    result = read_maintenance_snapshot(
        MaintenanceTargetV1(
            repository=intent.repository,
            pushed_ref=intent.pushed_ref,
            head_sha=intent.head_sha,
            workflow_type=intent.workflow_type,
        ),
        store=store,
        now=OBSERVED_AT,
    )

    assert result.status is SnapshotReadStatusV1.FRESH
    assert result.snapshot is not None
    assert result.snapshot.key == snapshot.key


@pytest.mark.parametrize(
    "state", [IntentLifecycleStateV1.PENDING, IntentLifecycleStateV1.NO_PR]
)
def test_unresolved_target_ignores_link_unless_intent_is_promoted(
    tmp_path: Path, state: IntentLifecycleStateV1
) -> None:
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=None)
    enqueue_maintenance(intent, store=store)
    snapshot = _snapshot(_key(head_sha=intent.head_sha))
    linked = MaintenanceIntentRecordV1(
        ingress_id=ingress_identity_hash(intent),
        intent=intent,
        state=state,
        canonical_key=snapshot.key,
    )
    store.intent_path(intent).write_text(
        linked.model_dump_json(), encoding="utf-8"
    )
    write_maintenance_snapshot(snapshot, store=store)

    result = read_maintenance_snapshot(
        MaintenanceTargetV1.unresolved(
            repository=intent.repository,
            pushed_ref=intent.pushed_ref,
            head_sha=intent.head_sha,
            workflow_type=intent.workflow_type,
        ),
        store=store,
        now=OBSERVED_AT,
    )

    assert result.status is SnapshotReadStatusV1.MISSING


def test_corrupt_promotion_link_returns_invalid(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "state")
    intent = _intent(pr_number=None)
    enqueue_maintenance(intent, store=store)
    store.intent_path(intent).write_text("{not-json", encoding="utf-8")

    result = read_maintenance_snapshot(
        MaintenanceTargetV1.unresolved(
            repository=intent.repository,
            pushed_ref=intent.pushed_ref,
            head_sha=intent.head_sha,
            workflow_type=intent.workflow_type,
        ),
        store=store,
        now=OBSERVED_AT,
    )

    assert result.status is SnapshotReadStatusV1.INVALID


def test_default_root_honors_apd_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "override"
    monkeypatch.setenv("APD_LIFECYCLE_STATE_DIR", str(override))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))

    assert LifecycleStore().root == override


def test_default_root_honors_xdg_state_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("APD_LIFECYCLE_STATE_DIR", raising=False)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))

    assert LifecycleStore().root == xdg / "agentic-pr-dash" / "lifecycle"


def test_default_root_falls_back_to_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("APD_LIFECYCLE_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    assert LifecycleStore().root == home / ".local" / "state" / "agentic-pr-dash" / "lifecycle"


def test_atomic_snapshot_replacement_never_exposes_partial_json(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "state")
    first = _snapshot(observed_at=OBSERVED_AT)
    second = _snapshot(observed_at=OBSERVED_AT + timedelta(seconds=1))
    write_maintenance_snapshot(first, store=store)
    errors: list[Exception] = []

    def reader() -> None:
        for _ in range(200):
            try:
                raw = store.snapshot_path(first.key).read_text(encoding="utf-8")
                loaded = MaintenanceSnapshotV1.model_validate_json(raw)
                assert loaded.observed_at in {first.observed_at, second.observed_at}
            except (
                AssertionError,
                OSError,
                UnicodeDecodeError,
                ValidationError,
            ) as exc:  # pragma: no cover - only on a torn read
                errors.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    for _ in range(50):
        write_maintenance_snapshot(second if _ % 2 else first, store=store)
    thread.join()

    assert errors == []
    assert os.stat(store.snapshot_path(first.key)).st_mode & 0o077 == 0


def test_state_directories_and_files_are_private_on_posix(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX permissions are not portable")
    store = LifecycleStore(tmp_path / "state")
    intent = _intent()
    snapshot = _snapshot()
    enqueue_maintenance(intent, store=store)
    write_maintenance_snapshot(snapshot, store=store)

    assert S_IMODE(store.root.stat().st_mode) == 0o700
    assert S_IMODE((store.root / "intents").stat().st_mode) == 0o700
    assert S_IMODE((store.root / "snapshots").stat().st_mode) == 0o700
    assert S_IMODE(store.intent_path(intent).stat().st_mode) == 0o600
    assert S_IMODE(store.snapshot_path(snapshot.key).stat().st_mode) == 0o600
    assert S_IMODE((store.root / ".lifecycle.lock").stat().st_mode) == 0o600


def test_lock_timeout_is_bounded_for_read_modify_write(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("fcntl is not portable")
    import fcntl

    store = LifecycleStore(tmp_path / "state", lock_timeout_seconds=0.03)
    store.root.mkdir(parents=True)
    fd = os.open(store.root / ".lifecycle.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        started = datetime.now().timestamp()
        with pytest.raises(TimeoutError):
            enqueue_maintenance(_intent(), store=store)
        assert datetime.now().timestamp() - started < 1
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_read_modify_write_lock_deduplicates_concurrent_enqueues(tmp_path: Path) -> None:
    store = LifecycleStore(tmp_path / "state")
    barrier = threading.Barrier(8)
    statuses: list[EnqueueStatusV1] = []

    def enqueue() -> None:
        barrier.wait()
        statuses.append(enqueue_maintenance(_intent(), store=store).status)

    threads = [threading.Thread(target=enqueue) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses.count(EnqueueStatusV1.ENQUEUED) == 1
    assert statuses.count(EnqueueStatusV1.DUPLICATE) == 7
