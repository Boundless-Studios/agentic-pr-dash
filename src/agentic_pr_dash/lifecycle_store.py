"""Small durable filesystem store for provider-neutral PR lifecycle state."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from os import PathLike
from pathlib import Path

from pydantic import ValidationError

from .lifecycle_models import (
    EnqueueResultV1,
    EnqueueStatusV1,
    IntentLifecycleStateV1,
    MaintenanceIntentRecordV1,
    MaintenanceIntentV1,
    MaintenanceKeyV1,
    MaintenanceSnapshotReadResultV1,
    MaintenanceSnapshotV1,
    MaintenanceTargetV1,
    SnapshotReadStatusV1,
)


_MISSING = object()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def lifecycle_state_root(root: PathLike[str] | None = None) -> Path:
    """Resolve the host-global lifecycle root, honoring the explicit override."""

    if root is not None:
        return Path(root).expanduser()
    override = os.environ.get("APD_LIFECYCLE_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    base = (
        Path(state_home).expanduser()
        if state_home
        else Path.home() / ".local" / "state"
    )
    return base / "agentic-pr-dash" / "lifecycle"


def _identity_digest(values: tuple[object, ...]) -> str:
    payload = json.dumps(values, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ingress_identity_hash(intent: MaintenanceIntentV1) -> str:
    """Hash the case-insensitive repository and exact pushed identity."""

    return _identity_digest(
        (
            intent.normalized_repository,
            intent.pushed_ref,
            intent.head_sha,
            intent.workflow_type,
        )
    )


def canonical_job_hash(key: MaintenanceKeyV1) -> str:
    """Hash the exact repository/PR/head/workflow job identity."""

    return _identity_digest(
        (key.normalized_repository, key.pr_number, key.head_sha, key.workflow_type)
    )


def _ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _write_json(path: Path, payload: object) -> None:
    _ensure_directory(path.parent)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary_path = Path(temporary)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> object:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return _MISSING


def _freshness(
    snapshot: MaintenanceSnapshotV1,
    *,
    max_age_seconds: float,
    now: datetime | None,
) -> MaintenanceSnapshotReadResultV1:
    current = _utc(now or datetime.now(timezone.utc))
    age = max(0.0, (current - snapshot.observed_at).total_seconds())
    status = (
        SnapshotReadStatusV1.FRESH
        if age <= max_age_seconds
        else SnapshotReadStatusV1.STALE
    )
    return MaintenanceSnapshotReadResultV1(
        status=status, snapshot=snapshot, age_seconds=age
    )


class LifecycleStore:
    """Filesystem-backed lifecycle store with atomic snapshot replacement."""

    def __init__(
        self,
        root: PathLike[str] | None = None,
        *,
        lock_timeout_seconds: float = 2.0,
    ) -> None:
        if lock_timeout_seconds <= 0:
            raise ValueError("lock_timeout_seconds must be positive")
        self.root = lifecycle_state_root(root)
        self.lock_timeout_seconds = lock_timeout_seconds
        self._intent_dir = self.root / "intents"
        self._snapshot_dir = self.root / "snapshots"
        self._lock_path = self.root / ".lifecycle.lock"

    def intent_path(self, intent: MaintenanceIntentV1) -> Path:
        return self._intent_dir / f"{ingress_identity_hash(intent)}.json"

    def snapshot_path(self, key: MaintenanceKeyV1) -> Path:
        return self._snapshot_dir / f"{canonical_job_hash(key)}.json"

    @contextmanager
    def _transaction_lock(self) -> Iterator[None]:
        _ensure_directory(self.root)
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + self.lock_timeout_seconds
        try:
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("timed out acquiring lifecycle store lock")
                    time.sleep(0.01)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _load_intent(
        self, intent: MaintenanceIntentV1
    ) -> MaintenanceIntentRecordV1 | None:
        raw = _read_json(self.intent_path(intent))
        if raw is _MISSING:
            return None
        try:
            return MaintenanceIntentRecordV1.model_validate(raw)
        except (ValidationError, TypeError) as exc:
            raise ValueError("invalid lifecycle intent record") from exc

    def enqueue(self, intent: MaintenanceIntentV1) -> EnqueueResultV1:
        """Enqueue an intent, deduplicating active records and reviving no-PR records."""

        path = self.intent_path(intent)
        with self._transaction_lock():
            existing = self._load_intent(intent)
            if existing is None:
                state = (
                    IntentLifecycleStateV1.NO_PR
                    if intent.pr_number is None
                    else IntentLifecycleStateV1.PENDING
                )
                record = MaintenanceIntentRecordV1(
                    ingress_id=ingress_identity_hash(intent), intent=intent, state=state
                )
                status = EnqueueStatusV1.ENQUEUED
            elif existing.state is IntentLifecycleStateV1.NO_PR:
                state = (
                    IntentLifecycleStateV1.NO_PR
                    if intent.pr_number is None
                    else IntentLifecycleStateV1.PENDING
                )
                record = MaintenanceIntentRecordV1(
                    ingress_id=existing.ingress_id,
                    intent=intent,
                    state=state,
                    canonical_key=None,
                )
                status = EnqueueStatusV1.REACTIVATED
            else:
                record = existing
                status = EnqueueStatusV1.DUPLICATE
            _write_json(path, record.model_dump(mode="json"))
        return EnqueueResultV1(
            status=status,
            intent=record.intent,
            state=record.state,
            ingress_id=record.ingress_id,
        )

    def promote_intent(
        self,
        target: MaintenanceIntentV1 | MaintenanceTargetV1,
        key: MaintenanceKeyV1,
    ) -> MaintenanceIntentRecordV1:
        """Link an ingress intent to the exact canonical job snapshot key."""

        intent = (
            target
            if isinstance(target, MaintenanceIntentV1)
            else self._intent_for_target(target)
        )
        if not _same_ingress_key(intent, key):
            raise ValueError(
                "promotion key does not match ingress repository, head, or workflow"
            )
        path = self.intent_path(intent)
        with self._transaction_lock():
            record = self._load_intent(intent)
            if record is None:
                raise KeyError("maintenance intent is not enqueued")
            if record.intent.pr_number not in (None, key.pr_number):
                raise ValueError("promotion PR number does not match intent")
            intent_data = record.intent.model_dump()
            intent_data["pr_number"] = key.pr_number
            promoted_intent = MaintenanceIntentV1(**intent_data)
            record = MaintenanceIntentRecordV1(
                ingress_id=record.ingress_id,
                intent=promoted_intent,
                state=IntentLifecycleStateV1.PROMOTED,
                canonical_key=key,
            )
            _write_json(path, record.model_dump(mode="json"))
        return record

    def mark_no_pr(
        self, target: MaintenanceIntentV1 | MaintenanceTargetV1
    ) -> MaintenanceIntentRecordV1:
        """Persist that an ingress event currently has no associated PR."""

        intent = (
            target
            if isinstance(target, MaintenanceIntentV1)
            else self._intent_for_target(target)
        )
        path = self.intent_path(intent)
        with self._transaction_lock():
            existing = self._load_intent(intent)
            if existing is not None and existing.state is IntentLifecycleStateV1.PROMOTED:
                raise ValueError("a promoted intent cannot be marked no_pr")
            data = (existing.intent if existing else intent).model_dump()
            data["pr_number"] = None
            marked_intent = MaintenanceIntentV1(**data)
            record = MaintenanceIntentRecordV1(
                ingress_id=(
                    existing.ingress_id
                    if existing
                    else ingress_identity_hash(intent)
                ),
                intent=marked_intent,
                state=IntentLifecycleStateV1.NO_PR,
            )
            _write_json(path, record.model_dump(mode="json"))
        return record

    def _intent_for_target(self, target: MaintenanceTargetV1) -> MaintenanceIntentV1:
        if target.key is not None or target.pr_number is not None:
            raise ValueError("promotion requires an unresolved ingress target")
        return MaintenanceIntentV1(
            repository=target.repository or "",
            pushed_ref=target.pushed_ref or "",
            head_sha=target.head_sha or "",
            workflow_type=target.workflow_type or "",
            reason="promotion",
            worktree_path="promotion",
            session_id="promotion",
            requested_at=datetime.now(timezone.utc),
        )

    def write_snapshot(self, snapshot: MaintenanceSnapshotV1) -> None:
        _ensure_directory(self.root)
        _write_json(
            self.snapshot_path(snapshot.key), snapshot.model_dump(mode="json")
        )

    def read_snapshot(
        self,
        target: MaintenanceTargetV1,
        *,
        max_age_seconds: float = 90.0,
        now: datetime | None = None,
    ) -> MaintenanceSnapshotReadResultV1:
        """Read and validate an exact snapshot, classifying freshness."""

        if max_age_seconds < 0:
            raise ValueError("max_age_seconds must not be negative")
        try:
            expected = target.exact_key or self._promoted_key(target)
        except (OSError, ValueError):
            return MaintenanceSnapshotReadResultV1(
                status=SnapshotReadStatusV1.INVALID,
                reason="promotion link could not be decoded",
            )
        if expected is None:
            return MaintenanceSnapshotReadResultV1(
                status=SnapshotReadStatusV1.MISSING, reason="target has no promoted key"
            )
        loaded = self._load_snapshot(expected)
        if isinstance(loaded, MaintenanceSnapshotReadResultV1):
            return loaded
        snapshot = loaded
        if not _same_job(snapshot.key, expected):
            return MaintenanceSnapshotReadResultV1(
                status=SnapshotReadStatusV1.MISSING,
                reason="snapshot identity does not match target",
            )
        return _freshness(snapshot, max_age_seconds=max_age_seconds, now=now)

    def _load_snapshot(
        self, key: MaintenanceKeyV1
    ) -> MaintenanceSnapshotV1 | MaintenanceSnapshotReadResultV1:
        try:
            raw = _read_json(self.snapshot_path(key))
        except (OSError, ValueError):
            return MaintenanceSnapshotReadResultV1(
                status=SnapshotReadStatusV1.INVALID,
                reason="snapshot could not be decoded",
            )
        if raw is _MISSING:
            return MaintenanceSnapshotReadResultV1(status=SnapshotReadStatusV1.MISSING)
        try:
            return MaintenanceSnapshotV1.model_validate(raw)
        except (ValidationError, TypeError):
            return MaintenanceSnapshotReadResultV1(
                status=SnapshotReadStatusV1.INVALID,
                reason="snapshot is not valid JSON contract",
            )

    def _promoted_key(self, target: MaintenanceTargetV1) -> MaintenanceKeyV1 | None:
        if target.repository is None:
            return None
        intent = MaintenanceIntentV1(
            repository=target.repository,
            pushed_ref=target.pushed_ref or "",
            head_sha=target.head_sha or "",
            workflow_type=target.workflow_type or "",
            reason="lookup",
            worktree_path="lookup",
            session_id="lookup",
            requested_at=datetime.now(timezone.utc),
        )
        record = self._load_intent(intent)
        if (
            record is None
            or record.state is not IntentLifecycleStateV1.PROMOTED
            or record.canonical_key is None
            or record.intent.pr_number != record.canonical_key.pr_number
        ):
            return None
        if not _same_ingress_key(intent, record.canonical_key):
            return None
        return record.canonical_key


def _same_ingress_key(intent: MaintenanceIntentV1, key: MaintenanceKeyV1) -> bool:
    return (
        intent.normalized_repository == key.normalized_repository
        and intent.head_sha == key.head_sha
        and intent.workflow_type == key.workflow_type
    )


def _same_job(left: MaintenanceKeyV1, right: MaintenanceKeyV1) -> bool:
    return (
        left.normalized_repository == right.normalized_repository
        and left.pr_number == right.pr_number
        and left.head_sha == right.head_sha
        and left.workflow_type == right.workflow_type
    )


def _store_for(
    store: LifecycleStore | PathLike[str] | None,
    root: PathLike[str] | None,
) -> LifecycleStore:
    if isinstance(store, LifecycleStore):
        if root is not None:
            raise ValueError("pass either store or root, not both")
        return store
    return LifecycleStore(root if root is not None else store)


def enqueue_maintenance(
    intent: MaintenanceIntentV1,
    store: LifecycleStore | PathLike[str] | None = None,
    *,
    root: PathLike[str] | None = None,
) -> EnqueueResultV1:
    """Module-level enqueue API for callers that do not retain a store object."""

    return _store_for(store, root).enqueue(intent)


def promote_maintenance_intent(
    target: MaintenanceIntentV1 | MaintenanceTargetV1,
    key: MaintenanceKeyV1,
    store: LifecycleStore | PathLike[str] | None = None,
    *,
    root: PathLike[str] | None = None,
) -> MaintenanceIntentRecordV1:
    return _store_for(store, root).promote_intent(target, key)


def mark_maintenance_intent_no_pr(
    target: MaintenanceIntentV1 | MaintenanceTargetV1,
    store: LifecycleStore | PathLike[str] | None = None,
    *,
    root: PathLike[str] | None = None,
) -> MaintenanceIntentRecordV1:
    return _store_for(store, root).mark_no_pr(target)




def write_maintenance_snapshot(
    snapshot: MaintenanceSnapshotV1,
    store: LifecycleStore | PathLike[str] | None = None,
    *,
    root: PathLike[str] | None = None,
) -> None:
    _store_for(store, root).write_snapshot(snapshot)


def read_maintenance_snapshot(
    target: MaintenanceTargetV1,
    store: LifecycleStore | PathLike[str] | None = None,
    *,
    max_age_seconds: float = 90.0,
    now: datetime | None = None,
    root: PathLike[str] | None = None,
) -> MaintenanceSnapshotReadResultV1:
    return _store_for(store, root).read_snapshot(
        target, max_age_seconds=max_age_seconds, now=now
    )
