from __future__ import annotations

from pathlib import Path

import pytest

from agentic_pr_dash import config, coordinator, maintenance_check
from agentic_pr_dash.models import PRData, PRStatus, ReviewComment
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash._maintenance import markers as _markers_mod
from agentic_pr_dash._maintenance import pr_state as _pr_state_mod
from agentic_pr_dash._maintenance import worktree_check as _worktree_check_mod


SID = "sess-owned"


@pytest.fixture(autouse=True)
def _stop_gate_no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    config.load.cache_clear()


def test_stop_gate_does_not_adopt_unmarked_open_pr_worktrees(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owned = tmp_path / "owned"
    unrelated = tmp_path / "unrelated"
    owned.mkdir()
    unrelated.mkdir()

    monkeypatch.setattr(
        _worktrees_mod,
        "_iter_worktrees_with_branch",
        lambda cwd: [(str(owned), "owned-branch"), (str(unrelated), "unrelated-branch")],
    )
    monkeypatch.setattr(
        _markers_mod,
        "_marker_session_id",
        lambda path: SID if Path(path) == owned else None,
    )
    monkeypatch.setattr(
        _worktrees_mod,
        "_list_my_open_prs",
        lambda cwd: {"unrelated-branch": (202, False)},
    )
    adopted: list[str] = []
    monkeypatch.setattr(
        _markers_mod,
        "_write_arm_marker",
        lambda path, session_id, pid, pr_number: adopted.append(path) or True,
    )
    monkeypatch.setattr(
        _worktrees_mod,
        "_live_independent_owner_paths",
        lambda paths, session_id: set(),
    )

    checked: list[str] = []

    def _check(path: str, session_id: str, *, claim: bool = True) -> tuple[int, str]:
        checked.append(path)
        if Path(path) == unrelated:
            return 10, "unrelated blocker\nPR_NUMBER=202"
        return 0, "nothing pending"

    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", _check)

    rc = maintenance_check.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])

    assert rc == 0
    assert checked == [str(owned)]
    assert adopted == []


def test_stop_gate_skips_marker_owned_path_with_live_independent_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()

    monkeypatch.setattr(
        _worktrees_mod,
        "_iter_worktrees_with_branch",
        lambda cwd: [(str(owned), "owned-branch")],
    )
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda path: SID)
    monkeypatch.setattr(_worktrees_mod, "_list_my_open_prs", lambda cwd: {})
    monkeypatch.setattr(
        _worktrees_mod,
        "_live_independent_owner_paths",
        lambda paths, session_id: {str(owned)},
    )

    checked: list[str] = []
    monkeypatch.setattr(
        _worktree_check_mod,
        "_check_worktree",
        lambda path, session_id, *, claim=True: checked.append(path) or (10, "blocker\nPR_NUMBER=1"),
    )

    rc = maintenance_check.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])

    assert rc == 0
    assert checked == []


def _blocked_pr(worktree: Path) -> PRData:
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


def _stub_check_worktree_to_blockers(monkeypatch: pytest.MonkeyPatch, pr: PRData) -> None:
    """Stub the pre-claim gauntlet so _check_worktree reaches the claim block."""
    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: pr)
    # The read-only thread-merge now runs in BOTH claim modes (BOU-1783); keep it
    # hermetic so the passive probe doesn't make a real gh call.
    monkeypatch.setattr(_pr_state_mod, "_unresolved_review_threads", lambda n, c: [])
    monkeypatch.setattr(_worktrees_mod, "_live_independent_owner_paths", lambda paths, sid: set())
    monkeypatch.setattr(_markers_mod, "_touch_owner_heartbeat", lambda cwd, sid, work: None)


def test_passive_stop_gate_probe_does_not_create_coordinator_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The passive stop-gate probe (claim=False) must report pending work without
    creating an agent-coordinator claim, so a later active check still sees it."""
    store = tmp_path / "claims.jsonl"
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(store))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _blocked_pr(worktree)
    _stub_check_worktree_to_blockers(monkeypatch, pr)

    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=False)

    assert code == 10
    assert "PR_NUMBER=42" in text
    assert "COORDINATOR_CLAIM_ID" not in text
    # No claim was written, so an active loop check still finds the work.
    assert coordinator.dispatch_decision_for_pr(pr).should_dispatch is True

    active_code, active_text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
    assert active_code == 10
    assert "COORDINATOR_CLAIM_ID" in active_text
