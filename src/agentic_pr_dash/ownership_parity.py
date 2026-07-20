"""Marker-vs-claim ownership parity harness (BOU-2223 Stage 1).

While markers stay authoritative, every marker write is mirrored into an
``agent-coordinator`` claim by :mod:`agentic_pr_dash.ownership`. This module
answers the question that gates Stage 2: *do the two views agree?*

Agreement is defined on the thing readers actually consume — the **effective live
owner** of a PR, plus how that ownership was acquired:

* marker side: the session the ``pr-watch.armed`` marker names, when the marker's
  three-tier liveness rule says that owner is live
* claim side: the session the latest ownership claim names, when
  :meth:`OwnershipSnapshot.owner_for` says that owner is live

A worktree with no marker and no claim agrees (nobody owns it). A pruned marker
whose claim was released agrees for the same reason. Anything else is a divergence
and gets logged, with enough context to tell which side is wrong.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os

from .config import load as load_config

PARITY_LOG_NAME = "ownership-parity.jsonl"


@dataclass(frozen=True)
class ParityResult:
    worktree: str
    repo: str
    pr: int | None
    marker_session_id: str | None
    marker_provenance: str | None
    marker_live: bool
    claim_session_id: str | None
    claim_provenance: str | None
    claim_live: bool
    claim_state: str | None
    agrees: bool
    reason: str

    @property
    def marker_owner(self) -> tuple[str, str] | None:
        """``(session_id, provenance)`` of the live marker owner, else ``None``."""
        if not self.marker_live or not self.marker_session_id:
            return None
        return (self.marker_session_id, self.marker_provenance or "armed")

    @property
    def claim_owner(self) -> tuple[str, str] | None:
        """``(session_id, provenance)`` of the live claim owner, else ``None``."""
        if not self.claim_live or not self.claim_session_id:
            return None
        return (self.claim_session_id, self.claim_provenance or "armed")


def _parity_log_path(cwd: str) -> str:
    return str(load_config(cwd).state_dir_for(cwd) / PARITY_LOG_NAME)


def log_divergence(cwd: str, payload: dict) -> None:
    """Append one divergence record to ``<state_dir>/ownership-parity.jsonl``.

    Best-effort and never raises: this is observability for the bake period, and a
    failure to record must not affect the ownership write that triggered it.
    """
    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "worktree": os.path.abspath(cwd),
            **payload,
        }
        path = _parity_log_path(cwd)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 — observability must never break a write path
        pass


def read_divergences(cwd: str) -> list[dict]:
    """Every divergence recorded for this worktree ([] when the log is absent)."""
    try:
        with open(_parity_log_path(cwd), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def compare_worktree(cwd: str, *, snap=None, now: datetime | None = None) -> ParityResult:
    """Compare marker-derived and claim-derived ownership for one worktree.

    ``snap`` lets a caller resolving many worktrees reuse a single
    :func:`agentic_pr_dash.ownership.snapshot` read — the claim store is read in
    full under a lock, so a per-worktree snapshot would be O(worktrees × events).
    """
    from ._maintenance._common import _repo_slug  # noqa: PLC0415
    from ._maintenance.markers import marker_owner_view  # noqa: PLC0415
    from . import ownership  # noqa: PLC0415

    cwd = os.path.abspath(cwd)
    repo = _repo_slug(cwd)
    marker = marker_owner_view(cwd)
    snapshot = snap if snap is not None else ownership.snapshot(now=now)

    pr = marker.get("pr") if marker else None
    claim_view = snapshot.owner_for(repo, pr) if pr is not None else None

    marker_sid = (marker or {}).get("session_id") or None
    marker_live = bool((marker or {}).get("live"))
    marker_prov = (marker or {}).get("provenance") if marker else None

    result_kwargs = {
        "worktree": cwd,
        "repo": repo,
        "pr": pr,
        "marker_session_id": marker_sid,
        "marker_provenance": marker_prov,
        "marker_live": marker_live,
        "claim_session_id": claim_view.session_id if claim_view else None,
        "claim_provenance": claim_view.provenance if claim_view else None,
        "claim_live": bool(claim_view and claim_view.live),
        "claim_state": claim_view.state if claim_view else None,
    }

    if not snapshot.known():
        # An unreadable claim store is not a divergence — we simply could not look.
        # Reported as disagreement-with-a-reason so a bake-period sweep can tell
        # "the two models differ" from "we never got an answer".
        return ParityResult(agrees=False, reason="claim_store_unreadable", **result_kwargs)

    if marker is None and pr is None and claim_view is None:
        return ParityResult(agrees=True, reason="no marker, no claim", **result_kwargs)

    left = (marker_sid, marker_prov or "armed") if (marker_live and marker_sid) else None
    right = (
        (claim_view.session_id, claim_view.provenance or "armed")
        if (claim_view and claim_view.live and claim_view.session_id)
        else None
    )
    if left == right:
        return ParityResult(agrees=True, reason="owners agree", **result_kwargs)

    if left is not None and right is None:
        reason = "claim missing or not live for marker-owned PR"
    elif left is None and right is not None:
        reason = "claim reports a live owner the marker does not"
    elif left[0] != right[0]:
        reason = "different owning session"
    else:
        reason = "different provenance"
    return ParityResult(agrees=False, reason=reason, **result_kwargs)


def audit_worktrees(paths, *, now: datetime | None = None) -> list[ParityResult]:
    """Compare every worktree in ``paths`` against a single claim-store snapshot.

    Divergences are logged to each worktree's own parity log so the bake-period
    evidence lives next to the worktree that produced it.
    """
    from . import ownership  # noqa: PLC0415

    snapshot = ownership.snapshot(now=now)
    results: list[ParityResult] = []
    for path in paths:
        result = compare_worktree(path, snap=snapshot, now=now)
        results.append(result)
        if not result.agrees:
            log_divergence(path, {"kind": "parity_divergence", **asdict(result)})
    return results
