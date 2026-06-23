"""BOU-1785 + BOU-1788: no defer path in ``_check_worktree`` may report a
blocked owned PR as a clean-looking no-op.

Three silent owner-defer points share the same bug family:
  * agent-coordinator claim (BOU-1785) — active claim, same fingerprint, hides
    still-unresolved review threads; worst when the claim is SELF-owned.
  * marker foreign-owner (BOU-1788) — returns before resolving the PR, so red CI
    is never surfaced.
  * live independent owner — blockers known but not named.

Policy: self/owner SERVICES (exit 10); a non-owner that sees blockers while
deferring to a live owner is WARN-ONLY (exit 0, explicit ``NOT clean`` text,
no claim/dispatch). These tests drive the REAL engine against a REAL
``JsonlClaimStore`` (no mocking of the subject under test); only the PR-resolve
and process boundaries are stubbed.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentic_pr_dash import coordinator, maintenance_check
from agentic_pr_dash.models import PRData, PRStatus, ReviewComment
from agentic_pr_dash._maintenance import markers as _markers_mod
from agentic_pr_dash._maintenance import pr_state as _pr_state_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod

SID = "sess-self"
FOREIGN_SID = "sess-foreign"


def _review_pr(worktree: Path) -> PRData:
    return PRData(
        number=42,
        title="needs review",
        branch="feature/x",
        url="https://github.com/Boundless-Studios/gaia-free/pull/42",
        worktree_path=str(worktree),
        status=PRStatus.HAS_COMMENTS,
        review_comments=[
            ReviewComment(id=7, author="r", body="fix", created_at="2026-06-11T12:00:00Z")
        ],
    )


def _red_ci_pr(worktree: Path) -> PRData:
    return PRData(
        number=43,
        title="ci red",
        branch="feature/ci",
        url="https://github.com/Boundless-Studios/gaia-free/pull/43",
        worktree_path=str(worktree),
        status=PRStatus.CI_FAILING,
        failing_checks=["ci/integration"],
        latest_commit_sha="deadbeef",
    )


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(tmp_path / "claims.jsonl"))


def _stub_gauntlet(
    monkeypatch: pytest.MonkeyPatch,
    pr: PRData,
    *,
    foreign_owner: str | None = None,
    independent: bool = False,
) -> None:
    """Stub the read-only boundaries so ``_check_worktree`` reaches the path
    under test with deterministic inputs. The coordinator store stays REAL."""
    monkeypatch.setattr(
        _markers_mod, "_live_foreign_owner", lambda cwd, sid: foreign_owner
    )
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: pr)
    monkeypatch.setattr(_pr_state_mod, "_unresolved_review_threads", lambda n, cwd: [])
    monkeypatch.setattr(
        _worktrees_mod,
        "_live_independent_owner_paths",
        lambda paths, sid: set(paths) if independent else set(),
    )
    monkeypatch.setattr(_markers_mod, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda cwd: None)


def _seed_active_claim(pr: PRData, *, session_id: str) -> None:
    """Write a live, unexpired active claim for ``pr`` owned by ``session_id``.

    pid is this test process (guaranteed live), lease far in the future, so the
    coordinator reports the claim ACTIVE / not-reclaimable.
    """
    claimed = coordinator.claim_pr(
        pr, session_id=session_id, pid=os.getpid(), agent="seed", lease_seconds=3600
    )
    assert claimed is not None


# ---------------------------------------------------------------------------
# BOU-1785 — agent-coordinator claim defer
# ---------------------------------------------------------------------------

def test_self_owned_active_claim_services_instead_of_deferring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The session that holds the claim must SERVICE its known blockers, not
    defer to its own claim for the whole lease window (BOU-1785 repro)."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _review_pr(worktree)
    _stub_gauntlet(monkeypatch, pr)
    _seed_active_claim(pr, session_id=SID)  # claim owned by the caller

    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)

    assert code == 10, text
    assert "PR_NUMBER=42" in text
    assert "COORDINATOR_CLAIM_ID=" in text


def test_foreign_active_claim_warns_with_explicit_blockers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A different session's active claim (same fingerprint) still defers, but
    the text must name the blockers + owner — not a clean-looking no-op."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _review_pr(worktree)
    _stub_gauntlet(monkeypatch, pr)
    _seed_active_claim(pr, session_id=FOREIGN_SID)

    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)

    assert code == 0, text
    assert "NOT clean" in text
    assert "review_comments" in text
    assert FOREIGN_SID in text
    assert text.strip() != "deferring to agent-coordinator active: claim is active"


# ---------------------------------------------------------------------------
# BOU-1788 — marker foreign-owner defer (returns before resolving the PR today)
# ---------------------------------------------------------------------------

def test_marker_foreign_owner_red_ci_warns_with_blockers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Deferring to a live foreign marker owner must still surface that the
    owned PR is red — not return a bare 'deferring to live owner' no-op."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _red_ci_pr(worktree)
    _stub_gauntlet(monkeypatch, pr, foreign_owner="sess-marker-owner")

    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)

    assert code == 0, text
    assert "NOT clean" in text
    assert "ci_failure" in text
    assert "COORDINATOR_CLAIM_ID=" not in text  # no fix dispatched


def test_marker_foreign_owner_clean_pr_stays_quiet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: a clean PR under a foreign marker owner must keep its quiet
    defer text — we did not make every deferral noisy."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = PRData(
        number=44,
        title="clean",
        branch="feature/clean",
        url="https://github.com/Boundless-Studios/gaia-free/pull/44",
        worktree_path=str(worktree),
        status=PRStatus.CLEAN,
    )
    _stub_gauntlet(monkeypatch, pr, foreign_owner="sess-marker-owner")

    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)

    assert code == 0, text
    assert text == "deferring to live PR-watch owner session sess-marker-owner"


# ---------------------------------------------------------------------------
# live independent owner defer (blockers already computed)
# ---------------------------------------------------------------------------

def test_live_independent_owner_with_blockers_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _red_ci_pr(worktree)
    _stub_gauntlet(monkeypatch, pr, independent=True)

    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)

    assert code == 0, text
    assert "NOT clean" in text
    assert "ci_failure" in text
