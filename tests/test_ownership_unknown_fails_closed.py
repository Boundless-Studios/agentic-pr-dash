"""P1 adoption-theft defect: an unresolvable owner must fail CLOSED, not "unowned".

Reported symptom: "sessions keep adopting each other's PRs." BOU-2223 Stage 4
retired the ``pr-watch.armed`` marker WRITER — the claim is now the sole
ownership-write authority, and the marker is a read-only compatibility shim.
Most ownership readers already flipped onto ``ownership_resolution.resolve_worktree``
(Stage 3) and union it with the marker, which is correct and already covered by
``tests/test_stage3_claim_reads.py``.

The remaining gap is narrower: when the claim STORE itself cannot be read this
tick (a lock timeout under concurrent access from another session's arm/
heartbeat/await, or a corrupt log) AND no local marker exists (the normal
post-Stage-4 state), ``resolve_worktree`` used to collapse that into
``source == "none"`` — indistinguishable, field-for-field, from "both sources
were actually consulted and genuinely agree nobody owns this". Every adoption/
takeover gate downstream then read "none" as "safe to take", so a transient
store hiccup let a sibling session (or the detached loop) adopt or dispatch
against a PR a live session actually holds. This is the same class of bug as
``reconcile._unknown_gh_state_record`` (a `gh` probe failure must not read as
"clean") applied to the ownership-resolution path instead of the PR-state path.

The fix adds a THIRD, explicit answer (``WorktreeOwnership.unknown`` /
``ownership_resolution.ownership_unknown``) and makes the four adoption/
takeover gates (worktree adoption, orphan-PR adoption, the `check`/stop-gate
service gate, and the headless dashboard's dispatch gate) check it and decline,
while a genuinely-unowned worktree (a READABLE, empty store) remains adoptable
exactly as before — covering both directions per the "several fixes tonight
shipped the inverse defect" warning.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os

import pytest

from agentic_pr_dash import config, github_api, orchestrator, ownership
from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash._maintenance import ownership_resolution
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import worktree_check as _worktree_check_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash._maintenance.ownership_resolution import (
    ownership_unknown,
    resolve_worktree,
)
from agentic_pr_dash._maintenance import pr_state as _pr_state_mod
from agentic_pr_dash.models import PRData, PRStatus

SID = "sess-mine"
OTHER_SID = "sess-other"
LIVE_PID = os.getpid()
REPO = "Boundless-Studios/agentic-pr-dash"


def _unknown_snap():
    return ownership.OwnershipSnapshot({}, now=datetime.now(timezone.utc), ok=False)


def _readable_empty_snap():
    return ownership.OwnershipSnapshot({}, now=datetime.now(timezone.utc), ok=True)


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(tmp_path / "claims.jsonl"))
    monkeypatch.setenv("AGENTIC_PR_DASH_STATE_DIR", str(tmp_path / ".gaia"))
    monkeypatch.setattr(config, "_detect_repo", lambda path: REPO)
    return tmp_path


def _mk(tmp_path, name: str) -> str:
    wt = tmp_path / name
    wt.mkdir()
    return str(wt)


# ── 1. resolve_worktree: "unknown" is a THIRD answer, not "none" ────────────


def test_unreadable_store_with_no_marker_is_unknown_not_none(isolated_store):
    """RED (pre-fix): this used to report source == 'none' — indistinguishable
    from a genuinely-unowned worktree. GREEN: `.unknown` is True instead."""
    wt = _mk(isolated_store, "nobody-and-broken-store")

    owned = resolve_worktree(wt, kind="test_divergence", snap=_unknown_snap())

    assert owned.unknown is True
    assert not owned.owned_by(SID)


def test_readable_empty_store_with_no_marker_stays_none_and_known(isolated_store):
    """Direction 2: a genuinely-unowned worktree (the store WAS read, and is
    simply empty) must NOT be flagged unknown — it must stay adoptable."""
    wt = _mk(isolated_store, "genuinely-unowned")

    owned = resolve_worktree(wt, kind="test_divergence", snap=_readable_empty_snap())

    assert owned.source == "none"
    assert owned.unknown is False


def test_unreadable_store_with_a_marker_present_is_not_unknown(isolated_store, legacy_marker_writes):
    """A marker is a trustworthy, store-independent answer on its own — an
    unreadable store must not downgrade a markered worktree to 'unknown'."""
    from agentic_pr_dash._maintenance.markers import _write_arm_marker

    wt = _mk(isolated_store, "markered")
    assert _write_arm_marker(wt, SID, LIVE_PID, 700)

    owned = resolve_worktree(wt, kind="test_divergence", snap=_unknown_snap())

    assert owned.source == "marker"
    assert owned.unknown is False
    assert owned.owned_by(SID)


def test_ownership_unknown_helper_matches_the_field(isolated_store):
    wt = _mk(isolated_store, "helper-check")
    assert ownership_unknown(wt, kind="test_divergence", snap=_unknown_snap()) is True
    assert ownership_unknown(wt, kind="test_divergence", snap=_readable_empty_snap()) is False


# ── 2. worktrees._collect_owned_worktrees: adoption must decline on "unknown" ──


def test_collect_owned_does_not_adopt_when_store_unreadable(tmp_path, monkeypatch):
    """RED (pre-fix): an unmarked worktree with an open non-draft @me PR was
    adopted regardless of whether the store could even be consulted."""
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    monkeypatch.setattr(
        _worktrees_mod, "_iter_worktrees_with_branch",
        lambda cwd: [(str(candidate), "br-candidate")],
    )
    monkeypatch.setattr(
        _worktrees_mod, "_list_my_open_prs",
        lambda cwd, timeout=15: {"br-candidate": (101, False)},
    )
    monkeypatch.setattr(
        _worktrees_mod, "_live_independent_owner_paths", lambda paths, sid: set()
    )
    monkeypatch.setattr(ownership, "snapshot", lambda **kw: _unknown_snap())

    result = mc._collect_owned_worktrees(SID, str(tmp_path), LIVE_PID)

    assert str(candidate) not in result
    assert mc._marker_session_id(str(candidate)) is None


def test_collect_owned_still_adopts_when_store_is_readable_and_empty(tmp_path, monkeypatch):
    """Direction 2 (regression guard): the ordinary missed-arm pickup path
    (BOU-1442) must keep working when the store genuinely has nothing to say."""
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    monkeypatch.setattr(
        _worktrees_mod, "_iter_worktrees_with_branch",
        lambda cwd: [(str(candidate), "br-candidate")],
    )
    monkeypatch.setattr(
        _worktrees_mod, "_list_my_open_prs",
        lambda cwd, timeout=15: {"br-candidate": (101, False)},
    )
    monkeypatch.setattr(
        _worktrees_mod, "_live_independent_owner_paths", lambda paths, sid: set()
    )
    monkeypatch.setattr(ownership, "snapshot", lambda **kw: _readable_empty_snap())

    result = mc._collect_owned_worktrees(SID, str(tmp_path), LIVE_PID)

    # `_write_arm_marker` (the adoption write) only reaches `_emit` on success,
    # so membership here already proves the missed-arm pickup fired. (A second
    # marker-file assertion isn't meaningful — Stage 4 disables that writer by
    # default, and `ownership.snapshot` is mocked here for the read side, so
    # neither would reflect the claim write this makes.)
    assert str(candidate) in result


# ── 3. reconcile._adopt_orphan_prs: same fail-closed gate on a present worktree ─


def test_adopt_orphan_prs_skips_present_worktree_when_store_unreadable(tmp_path, monkeypatch):
    """RED (pre-fix): a dead session's ledger entry whose worktree still exists
    was re-adopted whenever the marker/claim checks came up empty — including
    when they came up empty because the store could not be read, not because
    nobody actually owns it."""
    from agentic_pr_dash import session_ledger

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo_dir, check=True)
    (repo_dir / "agentic-pr-dash.toml").write_text('[project]\ntracker = "none"\n')
    monkeypatch.setattr(config, "_detect_repo", lambda path: REPO)

    monkeypatch.setattr(_reconcile_mod, "_session_is_live", lambda sid, cwd: False)
    monkeypatch.setattr(
        session_ledger, "list_session_ids", lambda: [OTHER_SID]
    )
    entry = session_ledger.LedgerEntry(
        pr=55, branch="main", worktree=str(repo_dir), baseline_sha=None,
        opened_at="2026-01-01T00:00:00Z", repo=REPO,
    )
    monkeypatch.setattr(
        session_ledger, "read",
        lambda sid, repo=None, include_legacy=True: [entry] if sid == OTHER_SID else [],
    )
    monkeypatch.setattr(_reconcile_mod, "_worktree_is_for_entry", lambda path, e, snap=None: True)
    monkeypatch.setattr(ownership, "snapshot", lambda **kw: _unknown_snap())

    claimed: list[int] = []
    monkeypatch.setattr(
        _reconcile_mod, "_claim_pr",
        lambda pr, sid, pid, repo="": claimed.append(pr) or True,
    )

    adopted = _reconcile_mod._adopt_orphan_prs(SID, str(repo_dir), LIVE_PID)

    assert adopted == []
    assert claimed == []


# ── 4. worktree_check._check_worktree: unresolvable ownership defers (code 2) ──


def _pr(**kwargs):
    base = dict(
        number=42, title="needs review", branch="feature/x",
        url="https://github.com/o/r/pull/42", worktree_path="/tmp/wt",
        status=PRStatus.HAS_COMMENTS, review_comments=[],
    )
    base.update(kwargs)
    return PRData(**base)


def test_check_worktree_defers_when_ownership_is_unresolvable(tmp_path, monkeypatch):
    """RED (pre-fix): with no marker and no resolvable live claim, `_check_worktree`
    treated the worktree as unowned and proceeded straight to dispatch — even
    when the reason neither check found an owner was that the store could not
    be read, not that nobody owns it."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _pr(worktree_path=str(worktree))

    monkeypatch.setattr(_worktree_check_mod.markers, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: pr)
    monkeypatch.setattr(_pr_state_mod, "_unresolved_review_threads", lambda n, c: [])
    monkeypatch.setattr(_worktree_check_mod.worktrees, "_live_independent_owner_paths", lambda paths, sid: set())
    monkeypatch.setattr(_worktree_check_mod.markers, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(ownership, "snapshot", lambda **kw: _unknown_snap())

    code, text = mc._check_worktree(str(worktree), SID, claim=False)

    assert code == 2, f"expected unresolvable-ownership defer (2), got {code}: {text!r}"
    assert "unresolvable" in text.lower()


def test_check_worktree_still_services_a_genuinely_unowned_pr(tmp_path, monkeypatch):
    """Direction 2 (regression guard): with a READABLE, empty store and no
    marker, a PR with no owner at all must still be serviced as before."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _pr(worktree_path=str(worktree))

    monkeypatch.setattr(_worktree_check_mod.markers, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: pr)
    monkeypatch.setattr(_pr_state_mod, "_unresolved_review_threads", lambda n, c: [])
    monkeypatch.setattr(_worktree_check_mod.worktrees, "_live_independent_owner_paths", lambda paths, sid: set())
    monkeypatch.setattr(_worktree_check_mod.markers, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(ownership, "snapshot", lambda **kw: _readable_empty_snap())

    code, text = mc._check_worktree(str(worktree), SID, claim=False)

    assert code != 2


# ── 5. orchestrator dashboard dispatch: same gate, headless caller ──────────


def test_dashboard_skips_dispatch_when_ownership_is_unresolvable(tmp_path, monkeypatch):
    """RED (pre-fix): the headless dashboard's session-precedence gate only
    checked the marker and a live claim by name; when the claim store itself
    could not be read and no marker existed, both checks came back "no owner"
    and the dashboard dispatched headless maintenance straight into a worktree
    a live session might actually be editing (BOU-2475's defer contract, from
    the OTHER direction: item 4 of the investigation)."""
    from agentic_pr_dash.models import PRData, PRStatus, ReviewComment

    worktree = tmp_path / "feature-unresolvable"
    worktree.mkdir()
    pr = PRData(
        number=321, title="Fix comments", branch="feature/one",
        url="https://github.com/Boundless-Studios/gaia-free/pull/321",
        worktree_path=str(worktree), status=PRStatus.HAS_COMMENTS,
        # A REAL blocker (not an empty list) so the two directions actually
        # diverge: with no blockers at all `blockers_for_pr` returns nothing
        # regardless of the ownership gate, and the assertion would pass for
        # the wrong reason on both sides of the fix.
        review_comments=[ReviewComment(id=1, author="r", body="still needs work",
                                       created_at="2026-06-11T12:00:00Z")],
    )
    monkeypatch.setattr(github_api, "get_failed_logs", lambda *a, **k: {})
    monkeypatch.setattr(ownership, "snapshot", lambda **kw: _unknown_snap())

    orch = orchestrator.Orchestrator(repo_cwd=None)
    asyncio.run(orch.dispatch_pr_maintenance(pr))

    assert pr.maintenance is None
    assert pr.coordinator_claim is None


def test_dashboard_still_dispatches_a_genuinely_unowned_pr(tmp_path, monkeypatch):
    """Direction 2 (regression guard): with a READABLE, empty store and no
    marker, the dashboard's ordinary unowned-worktree dispatch must still
    fire — this is its main job."""
    from agentic_pr_dash.models import ReviewComment

    worktree = tmp_path / "feature-unowned"
    worktree.mkdir()
    pr = PRData(
        number=322, title="Fix comments", branch="feature/two",
        url="https://github.com/Boundless-Studios/gaia-free/pull/322",
        worktree_path=str(worktree), status=PRStatus.HAS_COMMENTS,
        review_comments=[ReviewComment(id=1, author="r", body="fix",
                                       created_at="2026-06-11T12:00:00Z")],
    )
    monkeypatch.setattr(github_api, "get_failed_logs", lambda *a, **k: {})
    monkeypatch.setattr(ownership, "snapshot", lambda **kw: _readable_empty_snap())

    orch = orchestrator.Orchestrator(repo_cwd=None)
    asyncio.run(orch.dispatch_pr_maintenance(pr))

    assert pr.maintenance is not None
