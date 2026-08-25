"""Ownership-marker read/write helpers."""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

from agentic_pr_dash.config import load as load_config

from ._common import (
    _PID_MAX_DIGITS,
    _current_branch,
    _fix_lease_seconds,
    _parse_iso,
    _pid_alive,
    _repo_slug,
    _resolve_owner_pid,
)

# How long an owner's loop heartbeat stays "fresh" (the ownership lease).
_HEARTBEAT_TTL_SECONDS = 600       # 10 min — alive-and-ticking window (3m loop + slack)
_DEFAULT_FIX_LEASE_SECONDS = 1800  # 30 min — covers a long fix phase; override via env

# Heartbeat write coalescing window.
_DEFAULT_HEARTBEAT_MIN_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class _ClaimWriteResult:
    ok: bool
    created: bool = False
    claim_id: str | None = None
    lease_epoch: int | None = None

    def __bool__(self) -> bool:
        return self.ok


def _marker_path(cwd: str) -> str:
    return str(load_config(cwd).watch_marker_for(cwd))


def _read_marker(cwd: str) -> dict[str, str] | None:
    try:
        with open(_marker_path(cwd), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def _heartbeat_ttl_seconds(cwd: str | None = None) -> int:
    """How long an owner's heartbeat counts as 'fresh'."""
    cfg = load_config(cwd) if cwd else load_config()
    modern = os.environ.get("AGENTIC_PR_DASH_HEARTBEAT_TTL_SECONDS", "")
    if modern.isdigit() and int(modern) > 0:
        return int(modern)
    toml_ttl = cfg.extra.get("heartbeat_ttl_seconds")
    if isinstance(toml_ttl, int) and toml_ttl > 0:
        return toml_ttl
    legacy = os.environ.get("GAIA_PR_WATCH_HEARTBEAT_TTL", "")
    if legacy.isdigit() and int(legacy) > 0:
        return int(legacy)
    return cfg.heartbeat_ttl_seconds


def _heartbeat_fresh(heartbeat: str, cwd: str | None = None) -> bool:
    """True if the owner's loop heartbeat is within the short alive-TTL of now."""
    ts = _parse_iso(heartbeat)
    if ts is None:
        return False
    from datetime import datetime, timezone  # noqa: PLC0415

    return (datetime.now(timezone.utc) - ts).total_seconds() <= _heartbeat_ttl_seconds(cwd)


def _fix_lease_active(lease_until: str) -> bool:
    """True if an in-progress fix lease has not yet expired."""
    if not lease_until:
        return False
    ts = _parse_iso(lease_until)
    if ts is None:
        print(
            f"[pr-watch] warning: unparseable fix_lease_until={lease_until!r}; "
            f"treating as ACTIVE (deferring) to avoid a double-dispatch race",
            file=sys.stderr,
        )
        return True
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.now(timezone.utc) < ts


def _live_foreign_owner(cwd: str, self_session_id: str) -> str | None:
    """Session id of a live in-session owner (a DIFFERENT session), else None.

    A live owner is recognised by, in order: a fresh heartbeat, an active
    fix-lease, or — as the robust fallback — a still-alive owner ``pid``.

    The pid fallback matters because the heartbeat/fix-lease are only refreshed
    by a RUNNING in-session waiter/loop. When a session owns a PR but has no
    waiter running (e.g. the waiter stood down under machine-wide loop
    coverage), its heartbeat goes stale — yet the session is still very much
    alive. Without the pid check the machine-wide dashboard/loop would treat the
    live session as dead and claim the PR out from under it, overriding the
    "live in-session session wins; the loop only services unowned worktrees"
    rule. Pid-liveness makes that ownership durable regardless of heartbeat age.
    """
    fields = _read_marker(cwd)
    if fields is None:
        return None
    owner = fields.get("session_id", "")
    if not owner or owner == (self_session_id or ""):
        return None
    heartbeat_fresh = any(
        _heartbeat_fresh(value, cwd)
        for value in (fields.get("last_heartbeat", ""), fields.get("heartbeat", ""))
        if value
    )
    if heartbeat_fresh:
        return owner
    lease_until = fields.get("fix_lease_until", "")
    if lease_until:
        ts = _parse_iso(lease_until)
        if ts is None:
            print(
                f"[pr-watch] warning: unparseable fix_lease_until={lease_until!r}; "
                "falling back to owner pid-liveness",
                file=sys.stderr,
            )
        else:
            from datetime import datetime, timezone  # noqa: PLC0415

            if datetime.now(timezone.utc) < ts:
                return owner
    # Robust fallback: a still-alive owner pid keeps ownership even with a stale
    # heartbeat and no active fix-lease.
    if _pid_alive(fields.get("pid", "")):
        return owner
    return None


def marker_owner_view(cwd: str) -> dict | None:
    """Read-only marker-derived ownership of the worktree at ``cwd`` (BOU-2223).

    Returns ``None`` when no marker exists. Otherwise a dict with the marker's
    ``session_id`` / ``pid`` / ``pr`` / ``provenance`` plus ``live``, computed with
    the SAME three-tier rule ``_live_foreign_owner`` applies — fresh heartbeat, or
    an unexpired fix-lease, or (the robust fallback) a still-alive owner pid.

    This is an extraction for the ownership parity harness, not a behaviour change:
    ``_live_foreign_owner`` keeps its own inlined logic because it additionally
    filters to a *different* session and emits the unparseable-lease warnings that
    its callers depend on. This view answers "who does the marker name, and is that
    owner live?" for any session, including ourselves.

    The lease tier deliberately reproduces ``_live_foreign_owner``'s inline
    handling rather than calling ``_fix_lease_active``: the two disagree on an
    UNPARSEABLE ``fix_lease_until``. ``_fix_lease_active`` treats it as active (it
    guards a dispatch race, where deferring is the safe error), while the ownership
    reader falls through to the pid tier — which is what
    ``test_corrupt_fix_lease_without_fresh_heartbeat_does_not_defer_forever``
    pins. Mirroring the wrong one would corrupt the parity signal Stage 2 depends
    on for exactly the edge case both call sites went out of their way to handle.
    """
    fields = _read_marker(cwd)
    if fields is None:
        return None
    owner = fields.get("session_id", "")
    live = any(
        _heartbeat_fresh(value, cwd)
        for value in (fields.get("last_heartbeat", ""), fields.get("heartbeat", ""))
        if value
    )
    if not live:
        lease_ts = _parse_iso(fields.get("fix_lease_until", ""))
        if lease_ts is not None:
            from datetime import datetime, timezone  # noqa: PLC0415

            live = datetime.now(timezone.utc) < lease_ts
    if not live:
        live = _pid_alive(fields.get("pid", ""))
    pr_raw = fields.get("pr", "")
    return {
        "session_id": owner,
        "pid": _marker_pid_value(fields.get("pid", "")),
        "pr": int(pr_raw) if str(pr_raw).isdigit() else None,
        "provenance": fields.get("provenance", "") or "armed",
        "live": bool(owner) and live,
    }


def _marker_pid_value(pid_raw: str) -> int | None:
    """A marker ``pid=`` value as an int, or ``None`` when unusable.

    Applies the same guards ``_pid_alive`` does — ASCII digits only and at most
    ``_PID_MAX_DIGITS`` — so a corrupt marker cannot smuggle an oversized pid into
    an ownership claim, where it would only be rejected incidentally by
    ``os.kill``'s pid_t overflow.
    """
    if not (pid_raw.isascii() and pid_raw.isdigit()):
        return None
    if len(pid_raw) > _PID_MAX_DIGITS:
        return None
    value = int(pid_raw)
    return value if value > 0 else None


def _live_pr_owner_record(
    pr_number: int, repo: str, self_session_id: str, cwd: str | None = None
) -> tuple[str, str] | None:
    """``(session_id, owner_context_cwd)`` for a live ledger owner, else None."""
    from agentic_pr_dash import session_ledger  # noqa: PLC0415
    try:
        target = int(pr_number)
    except (TypeError, ValueError):
        return None
    self_sid = self_session_id or ""
    # Collect every LIVE owner, then prefer the most-recent one: a PR re-armed /
    # handed off to session B while A is still alive leaves BOTH sessions' ledger
    # rows (arm only appends for the new owner), so returning the first live
    # session in filesystem order could resolve the STALE previous owner A instead
    # of the current B (PR #61 review, P2). Rank by the matching row's ``opened_at``
    # (ISO ⇒ lexicographic == chronological); latest handoff wins.
    live: list[tuple[str, str, str]] = []  # (opened_at, session_id, owner_context)
    for other in session_ledger.list_session_ids():
        if not other or other == self_sid:
            continue
        try:
            # STRICT repo match (include_legacy=False): a repo-less LEGACY ledger
            # row for PR #N must NOT resolve an unrelated session as the owner of a
            # different repo's PR #N — this cross-session ownership gate defers
            # (or suppresses) work, so a loose legacy match could leave a
            # same-number PR in another repo unserviced (PR #61 review, P1). When
            # `repo` is undetectable ("") strict match yields nothing → no false
            # defer (fail-safe).
            entries = session_ledger.read(other, repo=repo, include_legacy=False)
        except Exception:  # noqa: BLE001 - a corrupt sibling ledger must not break resolution
            continue
        matching = [e for e in entries if e.pr == target]
        if not matching:
            continue
        entry = max(matching, key=lambda e: e.opened_at or "")
        # Resolve liveness in the ENTRY's OWN worktree context when it still
        # exists: a repo may configure its own ``session_registry_path``, so the
        # owner session can be absent from the checker cwd's registry summary.
        # Detached ledger rows can outlive their recorded worktree, though; in
        # that case fall back to the checker cwd so a removed path doesn't hide a
        # live owner session (PR #61 review, P2).
        live_ctx = entry.worktree if entry.worktree and os.path.exists(entry.worktree) else cwd
        if _session_is_live(other, live_ctx):
            live.append((entry.opened_at or "", other, live_ctx or ""))
    if not live:
        return None
    live.sort(key=lambda t: t[0], reverse=True)
    _opened_at, session_id, owner_context = live[0]
    return session_id, owner_context


def _live_pr_owner(
    pr_number: int, repo: str, self_session_id: str, cwd: str | None = None
) -> str | None:
    """Session id of a LIVE OTHER session that owns ``(repo, pr)`` in the durable
    session ledger + registry, else None (BOU-1924).

    The per-worktree marker gate (``_live_foreign_owner``) only sees the marker at
    the queried ``cwd``. A session working several PRs out of ONE repointed
    worktree keeps a marker for only its *current* branch's PR; its other owned
    PRs have no marker anywhere, so marker-only resolution can't attribute them to
    the live session — and a machine-wide loop would then service (or take over) a
    PR a live session is actually working. This resolver closes that gap by
    reading ownership from the worktree-independent ledger and gating on the
    registry's session liveness (mirrors ``_adopt_orphan_prs``' session scan).
    """
    record = _live_pr_owner_record(pr_number, repo, self_session_id, cwd)
    return record[0] if record is not None else None


def _marker_live_foreign_pid(cwd: str, self_session_id: str) -> bool:
    """True if the worktree's marker names a DIFFERENT session whose pid is still alive."""
    fields = _read_marker(cwd)
    if fields is None:
        return False
    owner = fields.get("session_id", "")
    if not owner or owner == (self_session_id or ""):
        return False
    return _pid_alive(fields.get("pid", ""))


def marker_writes_enabled() -> bool:
    """Whether ``pr-watch.armed`` is still WRITTEN (BOU-2223 Stage 4).

    Off by default from Stage 4 on: the claim is the sole ownership authority and
    the marker file is a READ-ONLY compatibility shim, kept for one release so a
    worktree armed by a pre-Stage-4 install does not lose ownership mid-migration.
    Stage 5 deletes the shim and this switch with it.

    ``AGENTIC_PR_DASH_MARKER_WRITES=1`` restores the Stage 1-3 dual-write without a
    package rollback, mirroring ``dual_write_enabled`` /
    ``ownership_resolution.claim_reads_enabled``. Parsing is deliberately the
    inverse of those two — they default ON and accept off-values, this defaults OFF
    and accepts on-values — because re-enabling a retired writer must be explicit.

    Note this gates the *ownership* marker only. ``pr-watch.session`` keeps being
    written: it records WHICH session owns a worktree's terminal, which is session
    identity rather than PR ownership and has no claim equivalent.
    """
    raw = os.environ.get("AGENTIC_PR_DASH_MARKER_WRITES", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _heartbeat_min_interval_seconds() -> int:
    raw = os.environ.get("AGENTIC_PR_DASH_HEARTBEAT_MIN_INTERVAL_SECONDS", "")
    if raw.isdigit() and int(raw) >= 0:
        return int(raw)
    return _DEFAULT_HEARTBEAT_MIN_INTERVAL_SECONDS


def _heartbeat_fields_from_claim(cwd: str, self_session_id: str) -> dict[str, str] | None:
    """Marker-shaped fields rebuilt from this session's live claim (Stage 4).

    Once ``pr-watch.armed`` is no longer written there is nothing on disk for
    :func:`_touch_owner_heartbeat` to read, so the claim supplies the same fields.
    Returning the marker's exact shape keeps the coalescing and lease-tier logic
    below untouched rather than forking it per source.

    ``fix_lease_until`` is synthesised only when the claim's remaining lease is
    longer than a heartbeat TTL, which is what distinguishes the long fix-lease
    tier from an ordinary heartbeat — the same distinction the marker encoded by
    the field's presence. None when this session holds no live claim here.
    """
    try:
        from agentic_pr_dash import ownership  # noqa: PLC0415

        snap = ownership.snapshot()
        if not snap.known():
            return None
        views = [
            v for v in snap.live_claims_for_worktree(cwd)
            if v.session_id == self_session_id and v.pr_number is not None
        ]
        if not views:
            return None
        view = max(views, key=lambda v: v.lease_epoch)
        claim = snap.claim_for(view.repo, view.pr_number)
        if claim is None:
            return None
        fields = {
            "pr": str(view.pr_number),
            "session_id": view.session_id,
            "pid": "" if view.pid is None else str(view.pid),
            "provenance": view.provenance,
        }
        stamp = claim.heartbeat_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        fields["last_heartbeat"] = stamp
        fields["heartbeat"] = stamp
        remaining = (claim.lease_expires_at - claim.heartbeat_at).total_seconds()
        if remaining > _heartbeat_ttl_seconds(cwd):
            fields["fix_lease_until"] = claim.lease_expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        return fields
    except Exception:  # noqa: BLE001 — heartbeating must never break the caller
        return None


def _touch_owner_heartbeat(cwd: str, self_session_id: str, work_found: bool) -> None:
    """Refresh this owner's coordination stamps (owner-only write).

    From Stage 4 the claim is what gets refreshed; the marker file is only
    rewritten while ``marker_writes_enabled()``. Ownership and the PR number are
    still resolved from the marker when one exists — during the shim window a
    legacy marker is the only record for a worktree armed by an older install —
    falling back to the live claim otherwise.
    """
    if not self_session_id:
        return
    fields = _read_marker(cwd)
    if fields is None:
        fields = _heartbeat_fields_from_claim(cwd, self_session_id)
        if fields is None:
            return
    elif fields.get("session_id", "") != self_session_id:
        return
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    prior_heartbeat = fields.get("last_heartbeat", "") or fields.get("heartbeat", "")
    had_lease = "fix_lease_until" in fields

    if not work_found:
        if not had_lease:
            prior_ts = _parse_iso(prior_heartbeat)
            if prior_ts is not None:
                age = (now - prior_ts).total_seconds()
                if 0 <= age < _heartbeat_min_interval_seconds():
                    return

    heartbeat = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    fields["last_heartbeat"] = heartbeat
    fields["heartbeat"] = heartbeat
    if work_found:
        fields["fix_lease_until"] = (now + timedelta(seconds=_fix_lease_seconds())).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    else:
        fields.pop("fix_lease_until", None)
    if marker_writes_enabled():
        import tempfile  # noqa: PLC0415

        content = "".join(f"{k}={v}\n" for k, v in fields.items())
        target = _marker_path(cwd)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".pr-watch.armed.")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(tmp, target)
        except OSError:
            if tmp is not None:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return

    # Mirror the refreshed lease into the claim (BOU-2223 dual-write). Coalescing
    # is inherited: this runs only when the marker was actually rewritten, so the
    # claim heartbeats on exactly the same cadence. ``work_found`` selects the same
    # long fix-lease tier the marker just wrote.
    pr_raw = fields.get("pr", "")
    if str(pr_raw).isdigit():
        _dual_write_ownership_claim(
            cwd,
            self_session_id,
            _marker_pid_value(fields.get("pid", "")),
            int(pr_raw),
            fields.get("provenance", "") or "armed",
            work_found=work_found,
            # No branch probe: a same-session claim_task on an active claim is a
            # heartbeat and keeps the ORIGINAL owner record, so metadata passed
            # here would be discarded anyway. The heartbeat runs on the Stop-hook
            # path, where a needless `git rev-parse` is pure deadline cost.
            branch="",
        )


def _write_arm_marker(
    cwd: str,
    session_id: str,
    pid: int,
    pr_number: int,
    provenance: str = "armed",
    *,
    expected_branch: str | None = None,
    expected_head_sha: str | None = None,
    deadline: float | None = None,
) -> bool:
    """Write the pr-watch.armed ownership marker (the single writer).

    ``provenance`` distinguishes how ownership was acquired (BOU-2221):

    * ``armed`` — the session explicitly armed this PR (it opened it, or a hook
      armed it on its behalf). Full ownership; the stop gate blocks on it.
    * ``adopted`` — auto-adoption claimed an unmarkered or dead-markered sibling
      worktree whose branch happened to have an open ``@me`` PR. The session has
      no context on that work, so the stop gate must not hold it hostage to that
      PR's CI; the maintenance loop services it instead.
    """
    import tempfile  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    state_dir = str(load_config(cwd).state_dir_for(cwd))
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        return False

    # A replacement binding was resolved before this writer ran. Re-read the
    # checkout at the ownership boundary so a concurrent branch switch cannot
    # stamp the new PR onto a different checkout. The caller supplies both
    # identities; missing local evidence is unknown, not a match.
    if not _checkout_matches_expected(
        cwd, expected_branch=expected_branch, expected_head_sha=expected_head_sha,
        deadline=deadline,
    ):
        return False

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fields = {
        "pr": str(pr_number),
        "armed_at": now,
        "session_id": session_id,
        "pid": str(pid),
        "provenance": provenance,
    }
    previous_marker: str | None = None
    if marker_writes_enabled():
        content = "".join(f"{k}={v}\n" for k, v in fields.items())
        target = os.path.join(state_dir, "pr-watch.armed")
        try:
            with open(target, encoding="utf-8") as fh:
                previous_marker = fh.read()
        except OSError:
            pass
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=state_dir, prefix=".pr-watch.armed.")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(content)
            if not _checkout_matches_expected(
                cwd,
                expected_branch=expected_branch,
                expected_head_sha=expected_head_sha,
                deadline=deadline,
            ):
                os.remove(tmp)
                return False
            os.replace(tmp, target)
        except OSError:
            if tmp is not None:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return False

    # Probed once and shared with the ownership dual-write below: both are `git`
    # subprocesses on the Stop-hook path, which has a hard deadline.
    #
    # Guarded because hoisting them OUT of the ledger-append try/except below
    # would otherwise let a probe failure escape ``_write_arm_marker`` — none of
    # its callers wrap it, so on the Stop-hook path that would crash the hook
    # instead of degrading. ``_current_branch`` catches only OSError/Timeout, and
    # ``subprocess.run(text=True)`` can also raise UnicodeDecodeError on a
    # non-UTF-8 ref name or git warning.
    try:
        branch = _bounded_current_branch(cwd, deadline)
    except Exception:  # noqa: BLE001
        branch = ""
    try:
        repo = _bounded_repo_slug(cwd, deadline)
    except Exception:  # noqa: BLE001
        repo = ""

    # The checkout can move while the branch/repository probes above run. Fence
    # the claim itself, not just the marker write, so a replacement checkout
    # cannot acquire durable ownership for the stale PR.
    if not _checkout_matches_expected(
        cwd, expected_branch=expected_branch, expected_head_sha=expected_head_sha,
        deadline=deadline,
    ):
        _rollback_arm_marker(cwd, session_id, pr_number, previous_marker)
        return False

    # An armed marker carries ``armed_at`` but NO ``heartbeat`` and no
    # ``fix_lease_until``, so it satisfies neither time-based liveness tier and its
    # ownership rests entirely on the owner pid. The mirrored claim must start in
    # the same state, or a session that arms and then dies before its first
    # heartbeat would look owned on the claim side and unowned on the marker side.
    claim_result = _dual_write_ownership_claim(
        cwd, session_id, pid, pr_number, provenance, repo=repo, branch=branch,
        pid_tier_only=True, track_creation=True,
    )
    if not _checkout_matches_expected(
        cwd, expected_branch=expected_branch, expected_head_sha=expected_head_sha,
        deadline=deadline,
    ):
        if isinstance(claim_result, _ClaimWriteResult) and claim_result.created:
            _release_dual_write_claim(
                repo, pr_number, session_id, reason="checkout_changed_during_arm",
                claim_id=claim_result.claim_id, lease_epoch=claim_result.lease_epoch,
            )
        elif claim_result is True:  # compatibility for injected legacy writers
            _release_dual_write_claim(
                repo, pr_number, session_id, reason="checkout_changed_during_arm"
            )
        _rollback_arm_marker(cwd, session_id, pr_number, previous_marker)
        return False
    claimed = bool(claim_result)
    marker_authoritative = marker_writes_enabled()
    if not claimed and not marker_authoritative:
        return False

    try:
        with open(os.path.join(state_dir, "pr-watch.session"), "w", encoding="utf-8") as fh:
            fh.write(session_id + "\n")
    except OSError:
        pass

    # The session ledger describes ownership that was actually acquired. Keep
    # it behind the claim fence so a refused replacement cannot be attributed
    # to the losing session.
    try:
        import subprocess  # noqa: PLC0415

        from agentic_pr_dash import session_ledger  # noqa: PLC0415
        baseline = None
        try:
            timeout = _remaining_timeout(deadline, 10)
            if timeout is None:
                raise subprocess.TimeoutExpired("git rev-parse HEAD", 0)
            rev = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"],
                                 capture_output=True, text=True, timeout=timeout,
                                 check=False)
            if rev.returncode == 0:
                baseline = rev.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            baseline = None
        session_ledger.append(session_id, pr_number, branch, cwd, baseline,
                              repo=repo)
    except Exception:  # noqa: BLE001
        pass
    # From Stage 4 the claim IS the ownership write, so its outcome is this
    # function's outcome. That makes arming genuinely fenced: when another LIVE
    # session already holds (repo, pr), `record_ownership` refuses and adoption
    # now declines instead of stamping a marker over the top of it. Under the
    # Stage 1-3 dual-write the marker write always "succeeded" and the losing
    # session went on believing it owned the PR — the BOU-2221 class of bug.
    #
    # With marker writes still enabled the marker remains authoritative, so a
    # claim-store hiccup must not turn a successful arm into a failure.
    if marker_authoritative:
        return True
    return claimed


def _current_head_sha(cwd: str, *, timeout: float = 5) -> str:
    """Return the checkout HEAD, or an empty string when git is unavailable."""
    import subprocess  # noqa: PLC0415

    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=max(0.001, timeout),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _remaining_timeout(deadline: float | None, cap: float) -> float | None:
    remaining = cap if deadline is None else deadline - time.monotonic()
    return min(cap, remaining) if remaining > 0 else None


def _bounded_current_branch(cwd: str, deadline: float | None) -> str:
    import subprocess  # noqa: PLC0415

    timeout = _remaining_timeout(deadline, 5)
    if timeout is None:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _bounded_repo_slug(cwd: str, deadline: float | None) -> str:
    """Resolve the local origin without an unbounded gh fallback."""
    import subprocess  # noqa: PLC0415
    from urllib.parse import urlparse  # noqa: PLC0415

    if deadline is None:
        return _repo_slug(cwd)
    timeout = _remaining_timeout(deadline, 10)
    if timeout is None:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return ""
    url = result.stdout.strip() if result.returncode == 0 else ""
    if "://" in url:
        tail = urlparse(url).path.lstrip("/")
    elif ":" in url:
        tail = url.split(":", 1)[1].lstrip("/")
    else:
        tail = url.lstrip("/")
    tail = tail.removesuffix(".git")
    return tail if tail.count("/") == 1 else ""


def _checkout_matches_expected(
    cwd: str,
    *,
    expected_branch: str | None,
    expected_head_sha: str | None,
    deadline: float | None = None,
) -> bool:
    """Return whether the live checkout still matches an expected binding."""
    if expected_branch is None and expected_head_sha is None:
        return True
    try:
        if _remaining_timeout(deadline, 5) is None:
            return False
        branch = _bounded_current_branch(cwd, deadline)
        timeout = _remaining_timeout(deadline, 5)
        if timeout is None:
            return False
        head_sha = (
            _current_head_sha(cwd)
            if deadline is None
            else _current_head_sha(cwd, timeout=timeout)
        )
    except Exception:  # noqa: BLE001 — fail closed at the arm boundary
        return False
    return bool(
        branch
        and head_sha
        and (expected_branch is None or branch == expected_branch)
        and (expected_head_sha is None or head_sha == expected_head_sha)
    )


def _discard_matching_arm_marker(cwd: str, session_id: str, pr_number: int) -> None:
    """Remove only the marker this failed arm attempt wrote."""
    fields = _read_marker(cwd) or {}
    if fields.get("session_id") != session_id or fields.get("pr") != str(pr_number):
        return
    try:
        os.remove(_marker_path(cwd))
    except OSError:
        pass


def _rollback_arm_marker(
    cwd: str, session_id: str, pr_number: int, previous_marker: str | None
) -> None:
    """Restore the marker replaced by this failed arm, or remove the new one."""
    fields = _read_marker(cwd) or {}
    if fields.get("session_id") != session_id or fields.get("pr") != str(pr_number):
        return
    if previous_marker is None:
        _discard_matching_arm_marker(cwd, session_id, pr_number)
        return
    target = _marker_path(cwd)
    tmp: str | None = None
    try:
        import tempfile  # noqa: PLC0415
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(target), prefix=".pr-watch.armed."
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(previous_marker)
        os.replace(tmp, target)
    except OSError:
        try:
            if tmp is not None:
                os.remove(tmp)
        except OSError:
            pass


def _release_dual_write_claim(
    repo: str, pr_number: int, session_id: str, *, reason: str = "released",
    claim_id: str | None = None, lease_epoch: int | None = None,
) -> None:
    """Best-effort rollback for a claim acquired across a checkout race."""
    try:
        from agentic_pr_dash import ownership

        ownership.release_ownership(
            repo=repo,
            pr_number=pr_number,
            session_id=session_id,
            reason=reason,
            claim_id=claim_id,
            lease_epoch=lease_epoch,
        )
    except Exception:  # noqa: BLE001 - rollback must not mask the fence
        pass


def _dual_write_ownership_claim(
    cwd: str, session_id: str, pid: int | None, pr_number: int, provenance: str,
    *, work_found: bool = False, repo: str | None = None, branch: str | None = None,
    pid_tier_only: bool = False, track_creation: bool = False,
) -> _ClaimWriteResult:
    """Record ownership as an agent-coordinator claim (BOU-2223).

    Stage 1 introduced this as a MIRROR of the authoritative marker write, so the
    parity harness could prove the claim-derived view agreed before any reader was
    flipped. From Stage 4 the relationship is inverted: this is the ownership
    write, and the marker is a read-only compatibility shim.

    Returns True when the claim was recorded. Callers use that as the arm's
    outcome once marker writes are retired, so a refusal (another live session
    holds this ``(repo, pr)``) correctly declines the arm rather than silently
    stamping over the top of it.

    Still never raises: a claim-store problem degrades to False rather than
    breaking the caller, and while marker writes are enabled the marker remains
    authoritative so False is not fatal.

    ``repo``/``branch`` let a caller that already probed git pass the values in;
    ``None`` means "probe here". ``pid_tier_only`` mirrors an arm that carries no
    heartbeat and no fix-lease, so the claim's liveness rests on the owner pid.
    """
    try:
        from agentic_pr_dash import ownership, ownership_parity  # noqa: PLC0415
        if not ownership.dual_write_enabled():
            return _ClaimWriteResult(False)
        before = ownership.snapshot() if track_creation else None
        previous = (
            before.claim_for(_repo_slug(cwd) if repo is None else repo, pr_number)
            if before is not None and before.known()
            else None
        )
        outcome = ownership.record_ownership(
            lease_seconds=ownership.LEASE_PID_TIER_ONLY if pid_tier_only else None,
            repo=_repo_slug(cwd) if repo is None else repo,
            pr_number=pr_number,
            session_id=session_id,
            pid=pid,
            worktree_path=cwd,
            provenance=provenance,
            branch=_current_branch(cwd) if branch is None else branch,
            work_found=work_found,
        )
        if not outcome.ok:
            ownership_parity.log_divergence(
                cwd,
                {
                    "kind": "dual_write_failed",
                    "pr": pr_number,
                    "session_id": session_id,
                    "provenance": provenance,
                    "reason": outcome.reason,
                    "conflict_session_id": outcome.conflict_session_id,
                },
            )
        created = bool(
            before is not None
            and before.known()
            and outcome.ok
            and outcome.claim_id
            and not (
                previous is not None
                and previous.status == "active"
                and previous.owner.session_id == session_id
                and previous.claim_id == outcome.claim_id
                and previous.lease_epoch == outcome.lease_epoch
            )
        )
        return _ClaimWriteResult(
            bool(outcome.ok), created, outcome.claim_id, outcome.lease_epoch
        )
    except Exception:  # noqa: BLE001 — never break the caller
        return _ClaimWriteResult(False)


def _marker_provenance(worktree_path: str) -> str:
    """Return how this worktree's ownership was acquired (BOU-2221).

    ``armed`` (the default) when the field is absent: markers written before
    provenance existed represent real arms, and must keep blocking exactly as
    they did. Only an explicit ``adopted`` stamp relaxes the stop gate.
    """
    marker = _marker_path(worktree_path)
    try:
        with open(marker, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith("provenance="):
                    value = line[len("provenance="):].strip()
                    return value or "armed"
    except OSError:
        return "armed"
    return "armed"


def _marker_session_id(worktree_path: str) -> str | None:
    """Return the ``session_id=`` value from a worktree's pr-watch marker."""
    marker = _marker_path(worktree_path)
    try:
        with open(marker, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith("session_id="):
                    return line[len("session_id="):]
    except OSError:
        return None
    return None


def _read_session_marker(cwd: str) -> str:
    """Best-effort: read the owning session id from pr-watch.session."""
    try:
        with open(
            str(load_config(cwd).session_marker_for(cwd)),
            encoding="utf-8",
        ) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _prune_stale_marker(cwd: str, marker: dict, session_id: str) -> bool:
    """Remove stale ownership artifacts when a marker's PR is authoritatively
    closed/merged. Returns True iff it actually pruned something — the caller
    needs to know this, not merely that it was called with a candidate PR
    number, so a still-open PR (the common no-op case) can be told apart from
    one this call just confirmed dead (BOU-2450: a caller that assumed every
    call pruned let a merged PR's stale number keep feeding a waiter-demand
    set computed before this prune ran)."""
    from .pr_state import _pr_open_state  # noqa: PLC0415
    from .stop_gate import _stop_state_path  # noqa: PLC0415
    pr_raw = marker.get("pr", "")
    if not str(pr_raw).isdigit():
        return False
    pr_number = int(pr_raw)

    state, *_ = _pr_open_state(pr_number, cwd)
    if state not in ("merged", "closed"):
        return False

    # The caller may hold a pre-rebind ownership snapshot for stale PR A while
    # this same tick has already armed replacement PR B. Never delete B's
    # marker while pruning A's ledger/claim artifacts.
    current_marker = _read_marker(cwd) or {}
    if current_marker.get("pr") == str(pr_number):
        try:
            os.remove(_marker_path(cwd))
        except OSError:
            pass

    try:
        os.remove(_stop_state_path(cwd))
    except OSError:
        pass

    try:
        from agentic_pr_dash import session_ledger  # noqa: PLC0415
        target_repo = _repo_slug(cwd)
        session_ledger.prune(session_id, {pr_number}, repo=target_repo)
    except Exception:  # noqa: BLE001
        pass

    try:
        from agentic_pr_dash import maintenance  # noqa: PLC0415
        maintenance.prune_state(cwd, pr_number)
    except Exception:  # noqa: BLE001
        pass

    release_ownership_claims(cwd, {pr_number}, session_id)
    return True


def release_ownership_claims(
    cwd: str, pr_numbers: set[int], session_id: str, *, reason: str = "pr_closed"
) -> None:
    """Release this session's ownership claims on PRs that are authoritatively dead.

    Every path that retires a PR's ownership must call this, not just the marker
    prune. From Stage 4 the claim IS ownership, so a claim left behind by a
    merged/closed PR is not cosmetic residue — it is a live ownership record for a
    dead PR that nothing else will ever clear, and the claim-derived readers will
    keep reporting an owner forever (BOU-2223 Stage 4).

    Deliberately NOT gated on ``dual_write_enabled()``: that switch governs whether
    we still MIRROR marker writes into claims, and turning the mirror off must not
    strand claims written while it was on. Releasing a claim that does not exist is
    a no-op, so the unconditional call is safe.

    Best-effort by construction — cleanup must never break the caller that was
    retiring the PR.
    """
    if not pr_numbers:
        return
    try:
        from agentic_pr_dash import ownership  # noqa: PLC0415
        repo = _repo_slug(cwd)
        for pr_number in pr_numbers:
            ownership.release_ownership(
                repo=repo,
                pr_number=pr_number,
                session_id=session_id,
                reason=reason,
            )
    except Exception:  # noqa: BLE001 — never break the caller
        pass


def _session_is_live(session_id: str, cwd: str | None = None) -> bool:
    """True if `session_id` has a non-terminal, pid-live registry entry."""
    from agentic_pr_dash import session_registry  # noqa: PLC0415
    try:
        reg = session_registry.registry_path(cwd) if cwd else None
        summary = session_registry.summarize_sessions(path=reg)
    except Exception:  # noqa: BLE001
        return False
    state = summary.sessions.get(session_id)
    if state is None or state.is_terminal:
        return False
    return session_registry.pid_is_live(state.pid)


def _claim_pr(pr_number: int, session_id: str, pid: int, repo: str = "") -> bool:
    """Win an exclusive claim on an orphan PR."""
    from agentic_pr_dash import session_ledger, session_registry  # noqa: PLC0415
    with session_ledger.claim_lock(pr_number, repo):
        existing = session_ledger.read_claim(pr_number, repo)
        if existing:
            if existing.get("session_id") == session_id:
                return True
            holder_pid = existing.get("pid")
            try:
                holder_pid = int(holder_pid)
            except (TypeError, ValueError):
                holder_pid = None
            if session_registry.pid_is_live(holder_pid):
                return False
        session_ledger.write_claim(pr_number, session_id, pid, repo)
        return True
