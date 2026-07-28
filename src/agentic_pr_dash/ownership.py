"""Fenced-claim ownership façade over ``agent-coordinator`` (BOU-2223 Stage 1).

``agentic-pr-dash`` has historically run two independent ownership systems: the
fenced claims in :mod:`agent_coordinator` (used by ``coordinator.py`` and
``loop.py``) and a hand-rolled ``pr-watch.armed`` marker scheme that re-derives
identity, leases, heartbeats, liveness, and stale-owner reaping less safely. This
module is the single façade that consolidates onto the former.

It models **the PR itself** as the claimed task — ``(repo_slug, pr_number)`` —
because a PR is the thing that can only have one maintainer. Worktrees are
incidental and plural: the same PR can appear under several roots.

Stage 1 is dual-write only. Markers remain authoritative; every marker write also
records a claim here so :mod:`agentic_pr_dash.ownership_parity` can prove the two
views agree before any reader is flipped.

Two properties matter for the readers that will arrive in Stage 2:

* **Liveness matches the marker rule.** ``TaskCoordinator.status()`` calls a claim
  reclaimable the moment its lease expires, even when the owner process is alive.
  The marker scheme deliberately keeps ownership for a live pid with a stale
  heartbeat (a session that owns a PR but runs no waiter stops heartbeating while
  remaining alive). :func:`OwnershipSnapshot.owner_for` encodes that rule.
* **Reads are bounded and fail closed.** The claim store is a JSONL log read in
  full under an exclusive lock, so per-PR ``status()`` calls do not fit the Stop
  hook's deadline. :func:`snapshot` reads once and answers many queries, and a
  failed read yields a snapshot that answers *unknown* rather than *unowned*.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
import time

from agent_coordinator.models import ClaimRecord, OwnerIdentity, TaskIdentity
from agent_coordinator.service import (
    ClaimConflictError,
    TaskCoordinator,
    default_pid_is_live,
)
from agent_coordinator.store import JsonlClaimStore

from ._maintenance._common import _fix_lease_seconds
from .config import load as load_config

# Ownership claims share the coordinator store with ``coordinator.py``'s dispatch
# claims but never collide with them: dispatch keys on the PR's *blocker*
# fingerprint and churns as CI/comments change, whereas ownership is
# blocker-independent and must survive every such transition. Separate task types
# keep both families in one auditable log.
OWNERSHIP_TASK_TYPE = "pr-ownership"

# A constant fingerprint makes ``TaskCoordinator.status()`` an exact hit, so
# ownership needs none of ``coordinator._best_active_claim_for_pr``'s
# fingerprint-agnostic scanning.
OWNERSHIP_FINGERPRINT = "ownership"

AGENT = "pr-watch"

#: Provenance values, carried in ``OwnerIdentity.metadata`` (subsumes BOU-2221's
#: bespoke ``provenance=`` marker field).
PROVENANCE_ARMED = "armed"
PROVENANCE_ADOPTED = "adopted"


def _store_path():
    # Deliberately routed through ``coordinator`` so ownership and dispatch claims
    # always land in the same store, including under
    # ``AGENTIC_PR_DASH_COORDINATOR_STORE`` in tests.
    from . import coordinator  # noqa: PLC0415

    return coordinator.store_path()


#: How long an ownership store operation may wait for the claim-store lock.
#: See :class:`BoundedLockClaimStore` for why this must be bounded.
DEFAULT_LOCK_TIMEOUT_SECONDS = 2.0

_LOCK_POLL_SECONDS = 0.01


def lock_timeout_seconds() -> float:
    raw = os.environ.get("AGENTIC_PR_DASH_OWNERSHIP_LOCK_TIMEOUT_SECONDS", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_LOCK_TIMEOUT_SECONDS
    except ValueError:
        return DEFAULT_LOCK_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_LOCK_TIMEOUT_SECONDS


class BoundedLockClaimStore(JsonlClaimStore):
    """A claim store whose lock wait is bounded.

    ``JsonlClaimStore`` takes ``fcntl.flock(LOCK_EX)`` with no timeout, so an
    operation blocks in the syscall for as long as any other holder keeps the lock
    — the long-running ``pr-maintenance-loop`` daemon, another session's waiter, or
    a process wedged mid-``fsync`` on a slow filesystem.

    That is fine for the daemon. It is NOT fine here: the ownership dual-write runs
    inside ``_write_arm_marker``/``_touch_owner_heartbeat``, both reachable from the
    stop gate — a Stop-hook subprocess with a hard ~108s deadline that fails CLOSED
    when exceeded (BOU-1953). Before this adapter existed, marker writes were
    lock-free (``mkstemp`` + ``os.replace``), so this would be the first blocking
    lock on that path, and the surrounding ``time.monotonic()`` budgets are
    cooperative — they cannot interrupt a thread parked in ``flock``.

    So the lock is acquired non-blockingly with a deadline and raises
    :class:`TimeoutError` instead of waiting. Every caller in this module already
    treats a store error as "fail closed": :func:`snapshot` reports ``known() ==
    False`` and :func:`record_ownership` returns a failed :class:`ClaimOutcome` that
    the marker write logs as a divergence and otherwise ignores.
    """

    def __init__(self, path, *, timeout: float | None = None):
        super().__init__(path)
        self.timeout = lock_timeout_seconds() if timeout is None else timeout

    @contextmanager
    def _bounded_lock(self):
        import fcntl  # noqa: PLC0415

        lock_path = Path(self.lock_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"claim store lock held longer than {self.timeout}s: "
                            f"{lock_path}"
                        ) from None
                    time.sleep(_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass

    def append_event(self, event):
        with self._bounded_lock():
            self._append_event_unlocked(event)

    def read_events(self):
        with self._bounded_lock():
            return self._read_events_unlocked()

    def transact_event(self, build_event):
        with self._bounded_lock():
            events = self._read_events_unlocked()
            event = build_event(events)
            self._append_event_unlocked(event)
            return event


def _coordinator() -> TaskCoordinator:
    return TaskCoordinator(BoundedLockClaimStore(_store_path()))


def ownership_task(repo: str, pr_number: int) -> TaskIdentity:
    """The claimed-task identity for ``(repo, pr)``.

    ``repo`` may be empty when the slug is undetectable; it falls back to a
    sentinel rather than raising, because ``TaskIdentity`` requires a non-empty
    ``task_id`` and an ownership record with an unknown repo is still strictly
    better than none. The sentinel cannot collide with a real ``owner/name``.
    """
    return TaskIdentity(
        task_type=OWNERSHIP_TASK_TYPE,
        task_id=f"github:{repo or 'unknown/unknown'}#{int(pr_number)}",
        fingerprint=OWNERSHIP_FINGERPRINT,
    )


def _parse_task_id(task_id: str) -> tuple[str, int | None]:
    """Parse an ownership ``task_id`` (``github:{repo}#{pr}``) defensively.

    Returns ``("", None)`` on anything that doesn't match the expected shape —
    a malformed or foreign task_id must never raise here; the caller (Stage 2's
    reverse worktree->claim index) has no better fallback than "unknown".
    """
    prefix = "github:"
    if not task_id.startswith(prefix):
        return "", None
    rest = task_id[len(prefix):]
    repo, sep, pr_raw = rest.rpartition("#")
    if not sep or not pr_raw.isdigit():
        return "", None
    return repo, int(pr_raw)


@dataclass(frozen=True)
class OwnerView:
    """Claim-derived ownership of one PR."""

    session_id: str
    pid: int | None
    worktree_path: str | None
    provenance: str
    live: bool
    state: str
    claim_id: str
    lease_epoch: int
    #: Parsed from the claim's ``task.task_id`` (``github:{repo}#{pr}``).
    #: ``""``/``None`` when the task_id doesn't parse — never raises.
    repo: str = ""
    pr_number: int | None = None
    metadata: dict[str, str] = field(default_factory=dict)


def _heartbeat_ttl_seconds(cwd: str | None = None) -> int:
    from ._maintenance.markers import _heartbeat_ttl_seconds as ttl  # noqa: PLC0415

    return ttl(cwd)


#: Lease for a claim that mirrors a marker carrying NO time-based tier — i.e. a
#: freshly-armed marker. ``_write_arm_marker`` writes ``armed_at`` but no
#: ``heartbeat`` and no ``fix_lease_until``, so such a marker satisfies neither of
#: the first two liveness tiers and its ownership rests entirely on the owner pid.
#: A zero lease reproduces that: the claim goes straight to the pid tier.
LEASE_PID_TIER_ONLY = 0


def _lease_seconds_for(cwd: str | None, work_found: bool) -> int:
    """Marker lease tiers, reused verbatim.

    The marker scheme grants a short alive-and-ticking window on a heartbeat and a
    much longer one while a fix is in flight. Mapping both onto the claim lease
    keeps lease expiry meaning the same thing on both sides of the parity check.
    """
    return _fix_lease_seconds() if work_found else _heartbeat_ttl_seconds(cwd)


class OwnershipSnapshot:
    """One store read, many ownership answers.

    ``ok=False`` marks a snapshot whose backing read failed. It answers every query
    with ``None`` *and* reports :meth:`known` as ``False`` so a caller can tell
    "nobody owns this" from "we could not find out" and fail closed.
    """

    def __init__(
        self,
        claims_by_task_id: dict[str, ClaimRecord],
        *,
        now: datetime,
        ok: bool = True,
        pid_is_live=default_pid_is_live,
    ):
        self._claims = claims_by_task_id
        self._now = now
        self.ok = ok
        self._pid_is_live = pid_is_live

    def known(self) -> bool:
        """False when the snapshot could not be read — callers must fail closed."""
        return self.ok

    def _pid_known_alive(self, pid: int | None) -> bool:
        """True only for a pid we can positively probe as running.

        ``default_pid_is_live`` answers ``True`` for ``None``/``<=0`` (nothing to
        probe ⇒ do not declare death). That default is right for the in-lease case
        but wrong for the expired-lease fallback, where an *absent* pid must not
        manufacture ownership — the marker rule reaches tier 3 only via
        ``_pid_alive``, which is ``False`` for a missing pid.
        """
        return pid is not None and pid > 0 and bool(self._pid_is_live(pid))

    def claim_for(self, repo: str, pr_number: int) -> ClaimRecord | None:
        if not self.ok:
            return None
        return self._claims.get(ownership_task(repo, pr_number).task_id)

    def _view_for_claim(self, claim: ClaimRecord) -> OwnerView:
        """Build the :class:`OwnerView` for one claim, applying the marker-parity
        liveness rule. Shared by :meth:`owner_for` and
        :meth:`live_claims_for_session` so the rule lives in exactly one place.
        """
        if claim.status != "active":
            state, live = claim.status, False
        elif self._now < claim.lease_expires_at:
            # Within the lease — the marker equivalent of a fresh heartbeat or an
            # active fix-lease. Those tiers grant ownership UNCONDITIONALLY: the
            # marker never consults the pid while the heartbeat is fresh, which
            # ``test_dead_pid_with_fresh_heartbeat_still_defers`` pins. That is not
            # an oversight — the marker's ``pid`` is stamped once at arm time by
            # ``_resolve_owner_pid`` and never rewritten, while a detached waiter or
            # the maintenance loop keeps heartbeating under the same session id, so
            # the recorded pid can die under a session that is still working.
            #
            # ``TaskCoordinator.status()`` would answer OWNER_DEAD here. Applying
            # that would make the parity harness log divergences for an ordinary
            # loop-covered PR, muddying the very signal Stage 2 has to trust.
            live, state = True, "active"
        else:
            # Lease expired. The marker scheme's third liveness tier keeps
            # ownership for a still-alive owner pid: a session with no waiter
            # running stops heartbeating but is not gone, and letting the
            # machine-wide loop claim its PR would override "live in-session
            # session wins". ``TaskCoordinator.status()`` alone would call this
            # reclaimable, so the rule is applied here instead — and, like the
            # marker's ``_pid_alive``, an absent pid does NOT satisfy it.
            live = self._pid_known_alive(claim.owner.pid)
            state = "owner_pid_live" if live else "expired"
        repo, pr_number = _parse_task_id(claim.task.task_id)
        return OwnerView(
            session_id=claim.owner.session_id,
            pid=claim.owner.pid,
            worktree_path=claim.owner.worktree_path,
            provenance=claim.owner.metadata.get("provenance", PROVENANCE_ARMED),
            live=live,
            state=state,
            claim_id=claim.claim_id,
            lease_epoch=claim.lease_epoch,
            repo=repo,
            pr_number=pr_number,
            metadata=dict(claim.owner.metadata),
        )

    def owner_for(self, repo: str, pr_number: int) -> OwnerView | None:
        """The claim-derived owner of ``(repo, pr)``, live or not.

        ``None`` means no claim was ever recorded (or the snapshot is unusable —
        check :meth:`known`). A released claim resolves to a view with
        ``live=False`` so a caller can distinguish "released" from "never claimed".
        """
        claim = self.claim_for(repo, pr_number)
        if claim is None:
            return None
        return self._view_for_claim(claim)

    def live_owner_for(self, repo: str, pr_number: int) -> OwnerView | None:
        """The owner only when it is still live; ``None`` otherwise."""
        view = self.owner_for(repo, pr_number)
        return view if view is not None and view.live else None

    def live_claims_for_session(self, session_id: str) -> list[OwnerView]:
        """Every LIVE ownership claim this session holds (BOU-2223 Stage 2).

        The claim store is keyed by ``(repo, pr)``, not by worktree, so a
        stop-gate reader that enumerates worktrees cannot look up "does my
        worktree have a claim" without already knowing the PR — which would be
        circular. ``OwnerIdentity.worktree_path`` (stamped by every dual-write in
        :mod:`agentic_pr_dash._maintenance.markers`) closes the loop: this scans
        every claim once and returns the ones ``session_id`` still holds live,
        each carrying its own ``worktree_path``/``repo``/``pr_number`` so a caller
        can build the worktree->claim reverse index in one pass.

        Reuses :meth:`owner_for`'s liveness rule via the shared
        :meth:`_view_for_claim` helper — it does not reimplement it.
        """
        if not self.ok or not session_id:
            return []
        views: list[OwnerView] = []
        for claim in self._claims.values():
            if claim.owner.session_id != session_id:
                continue
            view = self._view_for_claim(claim)
            if view.live:
                views.append(view)
        return views

    def live_claims_for_worktree(self, worktree_path: str) -> list[OwnerView]:
        """Every LIVE ownership claim naming ``worktree_path`` (BOU-2223 Stage 3).

        The session-scoped :meth:`live_claims_for_session` answers "which
        worktrees do I own", which is the stop gate's question. Stage 3's readers
        ask the transpose — "who owns THIS worktree" — for a path they already
        hold, and several of them (the dashboard card, the post-push hook) have no
        session id to scope by at all. Both directions are the same one-pass scan
        over ``OwnerIdentity.worktree_path``; neither can be a store lookup,
        because the store is keyed by ``(repo, pr)``.

        More than one live claim can legitimately name one worktree — a branch
        reassigned to a new PR leaves the old PR's claim unreleased, since
        dual-write only releases on merge/close. Disambiguating is the caller's
        job (it has the marker's current PR to compare against); this returns all
        of them, in no guaranteed order.

        Reuses :meth:`_view_for_claim`'s liveness rule — it does not reimplement it.
        """
        if not self.ok or not worktree_path:
            return []
        target = os.path.abspath(worktree_path)
        views: list[OwnerView] = []
        for claim in self._claims.values():
            claim_path = claim.owner.worktree_path
            if not claim_path or os.path.abspath(claim_path) != target:
                continue
            view = self._view_for_claim(claim)
            if view.live:
                views.append(view)
        return views


def _unknown_snapshot(now: datetime) -> OwnershipSnapshot:
    return OwnershipSnapshot({}, now=now, ok=False)


def snapshot(
    *, now: datetime | None = None, pid_is_live=default_pid_is_live
) -> OwnershipSnapshot:
    """Read the claim store once and index ownership claims by ``task_id``.

    Never raises: an unreadable or corrupt store yields an *unknown* snapshot
    (``ok=False``) so readers fail closed instead of concluding a PR is unowned.
    """
    timestamp = now or datetime.now(timezone.utc)
    try:
        claims = _coordinator()._claims_by_id()  # noqa: SLF001 — same-package contract
    except Exception:  # noqa: BLE001 — a broken store must not break ownership reads
        return _unknown_snapshot(timestamp)

    by_task: dict[str, ClaimRecord] = {}
    for claim in claims.values():
        if claim.task.task_type != OWNERSHIP_TASK_TYPE:
            continue
        current = by_task.get(claim.task.task_id)
        # Highest lease epoch wins — that is the fencing token. Ties (only
        # possible across distinct claim ids at the same epoch) fall back to
        # recency, matching ``TaskCoordinator._latest_claim_for_task``.
        key = (claim.lease_epoch, claim.claimed_at, claim.heartbeat_at, claim.claim_id)
        if current is None or key > (
            current.lease_epoch,
            current.claimed_at,
            current.heartbeat_at,
            current.claim_id,
        ):
            by_task[claim.task.task_id] = claim
    return OwnershipSnapshot(by_task, now=timestamp, pid_is_live=pid_is_live)


@dataclass(frozen=True)
class ClaimOutcome:
    """What a dual-write attempt did. Never an exception — Stage 1 must not let a
    claim-store problem break the authoritative marker write."""

    ok: bool
    reason: str
    claim_id: str | None = None
    lease_epoch: int | None = None
    conflict_session_id: str | None = None
    #: The refused claim's holder pid/provenance/lease (BOU-2409) — populated
    #: alongside ``conflict_session_id`` so a caller (``arm``'s CLI) can name
    #: the actual refusal instead of a generic "could not write" message.
    conflict_pid: int | None = None
    conflict_provenance: str | None = None
    conflict_lease_expires_at: datetime | None = None


def record_ownership(
    *,
    repo: str,
    pr_number: int,
    session_id: str,
    pid: int | None,
    worktree_path: str | None,
    provenance: str = PROVENANCE_ARMED,
    branch: str = "",
    work_found: bool = False,
    lease_seconds: int | None = None,
    now: datetime | None = None,
) -> ClaimOutcome:
    """Record (or refresh) this session's ownership claim on ``(repo, pr)``.

    ``TaskCoordinator.claim_task`` is idempotent for the *same* session on an
    active claim — it emits a heartbeat and preserves the lease epoch — so arm and
    heartbeat share this one entry point and the adapter stays stateless: nothing
    has to be persisted next to the marker.

    A foreign session holding an active claim raises ``ClaimConflictError``, which
    is reported rather than raised. While markers are authoritative that is a
    parity *divergence*, not a failure.

    Stage 2 caveat: ``claim_task`` guards only against an ACTIVE foreign claim. A
    foreign claim whose lease expired while its owner pid is still live is
    reclaimable to the coordinator but still *live* to :meth:`owner_for` (and to
    the marker rule it mirrors), so a Stage-2 caller must consult ``owner_for``
    before claiming rather than relying on ``claim_task`` to refuse. Stage 1 is
    unaffected: the only callers are marker writes, already gated upstream by
    ``_live_foreign_owner``.

    BOU-2409: an ``armed`` attempt outranks a foreign session's ``adopted``
    claim. Adoption is a best-effort backstop for a PR nobody else was
    servicing — it must never fence out the session that actually owns the
    branch/worktree and is doing the real work, which is exactly what an
    explicit ``armed`` attempt represents. On conflict with a foreign
    ``adopted`` claim, this releases it and retries ONCE. A foreign ``armed``
    claim (or an ``adopted`` attempt against anything) is never overridden —
    only ``armed`` outranks ``adopted``, nothing outranks ``armed``.
    """
    if not session_id:
        return ClaimOutcome(False, "no session_id")
    metadata = {"provenance": provenance or PROVENANCE_ARMED}
    if branch:
        metadata["branch"] = branch
    owner = OwnerIdentity(
        session_id=session_id,
        pid=int(pid) if pid is not None else None,
        agent=AGENT,
        worktree_path=worktree_path,
        metadata=metadata,
    )
    resolved_lease_seconds = (
        _lease_seconds_for(worktree_path, work_found)
        if lease_seconds is None
        else lease_seconds
    )

    def _conflict_outcome(exc: ClaimConflictError) -> ClaimOutcome:
        claim = exc.decision.claim
        holder = claim.owner if claim is not None else None
        return ClaimOutcome(
            False,
            "claim held by another session",
            conflict_session_id=holder.session_id if holder is not None else None,
            conflict_pid=holder.pid if holder is not None else None,
            conflict_provenance=(
                (holder.metadata or {}).get("provenance") if holder is not None else None
            ),
            conflict_lease_expires_at=claim.lease_expires_at if claim is not None else None,
        )

    # Promote this session's OWN adopted claim before claiming. `claim_task`
    # treats an active same-session claim as an idempotent heartbeat and keeps
    # the original owner metadata, so it raises no ClaimConflictError and the
    # reclaim branch below never runs. A session that auto-adopted a PR and
    # later explicitly ARMS it would therefore be told it succeeded while the
    # authoritative claim still said `provenance=adopted` — and with marker
    # writes off by default (Stage 4) nothing else records the arm. The stop
    # gate and the await loop both exclude adopted worktrees as
    # maintenance-loop-owned, so the explicit arm bought no coverage at all.
    # Releasing first makes the subsequent claim_task mint a fresh claim
    # carrying `armed`.
    if (provenance or PROVENANCE_ARMED) == PROVENANCE_ARMED and session_id:
        try:
            _self = snapshot(now=now)
            _own = _self.claim_for(repo, pr_number) if _self.known() else None
            if (
                _own is not None
                and _own.status == "active"
                and _own.owner.session_id == session_id
                and (_own.owner.metadata or {}).get("provenance") == PROVENANCE_ADOPTED
            ):
                release_ownership(
                    repo=repo, pr_number=pr_number, session_id=session_id,
                    reason="promoted_adopted_to_armed", now=now,
                    claim_id=_own.claim_id, lease_epoch=_own.lease_epoch,
                    expected_provenance=PROVENANCE_ADOPTED,
                )
        except Exception:  # noqa: BLE001 — promotion is best-effort; never break the claim
            pass

    try:
        record = _coordinator().claim_task(
            ownership_task(repo, pr_number), owner,
            lease_seconds=resolved_lease_seconds, now=now,
        )
    except ClaimConflictError as exc:
        holder_claim = exc.decision.claim
        holder = holder_claim.owner if holder_claim is not None else None
        holder_provenance = (holder.metadata or {}).get("provenance") if holder is not None else None
        can_reclaim = (
            (provenance or PROVENANCE_ARMED) == PROVENANCE_ARMED
            and holder is not None
            and holder.session_id != session_id
            and holder_provenance == PROVENANCE_ADOPTED
        )
        if not can_reclaim:
            return _conflict_outcome(exc)
        released = release_ownership(
            repo=repo, pr_number=pr_number, session_id=holder.session_id,
            reason="reclaimed_by_armed_session", now=now,
            claim_id=holder_claim.claim_id, lease_epoch=holder_claim.lease_epoch,
            expected_provenance=PROVENANCE_ADOPTED,
        )
        if not released.ok:
            return ClaimOutcome(
                False, released.reason,
                conflict_session_id=holder.session_id,
                conflict_pid=holder.pid,
                conflict_provenance=holder_provenance,
                conflict_lease_expires_at=holder_claim.lease_expires_at,
            )
        # Fence the durable cleanup the same way the release above is fenced.
        # Releasing the adopted claim opens a window in which the adopter can
        # re-arm this PR — appending a FRESH ledger entry and taking a new
        # coordinator claim carrying `armed` provenance. An unconditional prune
        # would delete that new entry, so the adopter would go on to win the
        # claim retry below (correctly) but be left without the durable
        # ledger/marker artifacts a session-wide waiter needs to find the
        # worktree after a restart or from another maintenance root. Re-read the
        # store and only retire durable state while the PR is still unclaimed.
        # The recheck must gate on `known()`, not on `claim_for()` alone: a
        # bounded-read failure (lock timeout during a concurrent re-arm) yields
        # an UNKNOWN snapshot whose `claim_for` still answers None. Treating
        # that as "unclaimed" would prune the holder's durable state on exactly
        # the evidence this module promises to fail closed on — and if the
        # preceding release_ownership read failed the same way, the original
        # claim was never released either, so the retry then loses to a live
        # owner whose ledger/marker we just deleted.
        _snap = snapshot(now=now)
        _recheck = _snap.claim_for(repo, pr_number) if _snap.known() else None
        if _snap.known() and (_recheck is None or _recheck.status != "active"):
            try:
                from . import session_ledger  # noqa: PLC0415
                # include_legacy=False deliberately: this reclaim is scoped to
                # ONE repo, and legacy rows are repo-less, so PR numbers collide
                # across repos. `include_legacy=True` would drop every repo-less
                # #N row while reclaiming repo A's #N — including one that
                # represents repo B's still-open #N — and a restarted or
                # cross-root waiter would silently lose coverage for repo B.
                # This is the same hazard session_ledger.prune's own docstring
                # cites for non-anchor roots (codex PR #30).
                session_ledger.prune(
                    holder.session_id, {pr_number}, repo=repo, include_legacy=False,
                )
                if holder.worktree_path:
                    marker_path = load_config(holder.worktree_path).watch_marker_for(
                        holder.worktree_path
                    )
                    fields: dict[str, str] = {}
                    with open(marker_path, encoding="utf-8") as fh:
                        for line in fh:
                            key, _, value = line.strip().partition("=")
                            fields[key] = value
                    if (
                        fields.get("session_id") == holder.session_id
                        and fields.get("pr") == str(pr_number)
                        and fields.get("provenance") == PROVENANCE_ADOPTED
                    ):
                        os.remove(marker_path)
            except (OSError, ValueError):
                pass
        try:
            record = _coordinator().claim_task(
                ownership_task(repo, pr_number), owner,
                lease_seconds=resolved_lease_seconds, now=now,
            )
        except ClaimConflictError as exc2:
            # Someone else won the race between the release and the retry —
            # report THAT holder, not the one we already released.
            return _conflict_outcome(exc2)
        except Exception as exc2:  # noqa: BLE001 — never break the marker write
            return ClaimOutcome(False, f"claim store error: {type(exc2).__name__}: {exc2}")
    except Exception as exc:  # noqa: BLE001 — never break the marker write
        return ClaimOutcome(False, f"claim store error: {type(exc).__name__}: {exc}")
    # Postcondition: an ARMED request must leave an ARMED claim. The promotion
    # above is best-effort, and `release_ownership` deliberately reports success
    # for "no claim to release" — which is also what an unreadable snapshot
    # produces. So the release can silently no-op, after which the same-session
    # `claim_task` is an idempotent heartbeat that PRESERVES `provenance=adopted`
    # and we would return "claimed" for an arm that never took. Since both the
    # stop gate and the await loop exclude adopted worktrees, that success would
    # be a lie about coverage. Verify the result rather than trusting the path.
    _got = (record.owner.metadata or {}).get("provenance")
    if (provenance or PROVENANCE_ARMED) == PROVENANCE_ARMED and _got != PROVENANCE_ARMED:
        return ClaimOutcome(
            False,
            f"claim still records provenance={_got!r} after an armed request; "
            "the adopted->armed promotion did not take",
        )
    return ClaimOutcome(True, "claimed", record.claim_id, record.lease_epoch)


def release_ownership(
    *,
    repo: str,
    pr_number: int,
    session_id: str,
    reason: str = "released",
    now: datetime | None = None,
    claim_id: str | None = None,
    lease_epoch: int | None = None,
    expected_provenance: str | None = None,
) -> ClaimOutcome:
    """Release this session's ownership claim on ``(repo, pr)``.

    Resolves ``claim_id`` and ``lease_epoch`` from a fresh snapshot, so callers
    never carry fencing state. Releasing a claim owned by a different session is
    refused (the coordinator would raise ``PermissionError``); releasing a claim
    that does not exist is a no-op.
    """
    if not session_id:
        return ClaimOutcome(False, "no session_id")
    snap = snapshot(now=now)
    claim = snap.claim_for(repo, pr_number)
    if claim is None:
        # Also the ``ok=False`` path: an unreadable store yields no claim. Reporting
        # success is right either way — there is nothing we can release, and a
        # release failure must not propagate into marker pruning.
        return ClaimOutcome(True, "no claim to release")
    if claim.owner.session_id != session_id:
        return ClaimOutcome(
            False,
            "claim owned by another session",
            conflict_session_id=claim.owner.session_id,
        )
    if claim_id is not None and (
        claim.claim_id != claim_id or claim.lease_epoch != lease_epoch
    ):
        return ClaimOutcome(False, "claim changed before release")
    if expected_provenance is not None and (
        (claim.owner.metadata or {}).get("provenance") != expected_provenance
    ):
        return ClaimOutcome(False, "claim provenance changed before release")
    if claim.status != "active":
        # Already released under some reason — releasing again would raise.
        return ClaimOutcome(True, "already released", claim.claim_id, claim.lease_epoch)
    try:
        _coordinator().release_claim(
            claim.claim_id,
            owner_session_id=session_id,
            lease_epoch=claim.lease_epoch,
            reason=reason,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 — never break the marker prune
        return ClaimOutcome(False, f"release error: {type(exc).__name__}: {exc}")
    return ClaimOutcome(True, "released", claim.claim_id, claim.lease_epoch)


def dual_write_enabled() -> bool:
    """Whether marker writes should also record a claim.

    On by default. ``AGENTIC_PR_DASH_OWNERSHIP_DUAL_WRITE=0`` is the kill switch for
    the bake period, so a misbehaving claim store can be taken out of the write
    path without a package rollback.
    """
    raw = os.environ.get("AGENTIC_PR_DASH_OWNERSHIP_DUAL_WRITE", "").strip().lower()
    return raw not in ("0", "false", "no", "off")
