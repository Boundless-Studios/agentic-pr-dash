"""BOU-1879 + BOU-2475: the loop bounds — but no longer rushes — takeover from a
WAKE-LESS owner.

A "live foreign owner" holds ownership while its marker pid is alive / heartbeat
fresh. But a wake-less owner — a session with no live feedback waiter (e.g. codex,
no wake channel) — can't be woken to service the PR, so deferring to it every tick
strands the PR (the observed "deferring … NOT clean (no fix dispatched)" loop).

BOU-1879 made that grace exactly ONE tick. BOU-2475 replaced the tick with a
wall-clock horizon (``wakeless_takeover_seconds``): ticks are a poor clock because
``--interval`` is configurable and every dashboard ``--once`` run burns one, so
"one tick" meant takeover could land seconds after the owner was first seen. The
bound is preserved — a wake-less owner is still taken over — it is just measured in
time now. A wake-CAPABLE owner still keeps its PR.
"""
from __future__ import annotations

import contextlib
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_pr_dash import maintenance_check
from agentic_pr_dash.models import PRData, PRStatus, ReviewComment
from agentic_pr_dash._maintenance import markers as _markers_mod
from agentic_pr_dash._maintenance import pr_state as _pr_state_mod
from agentic_pr_dash._maintenance import waiter as _waiter_mod
from agentic_pr_dash._maintenance import worktree_check as _wc
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod


@contextlib.contextmanager
def _advance_clock(seconds: float):
    """Run the block with worktree_check's clock moved forward by ``seconds``."""
    with patch.object(_wc.time, "time", return_value=time.time() + seconds):
        yield

SID = "sess-self"
OWNER = "sess-wakeless-owner"


def _independent_owner(
    session_id: str, *, registry_backed: bool
) -> _worktrees_mod.IndependentOwnerIdentity:
    return _worktrees_mod.IndependentOwnerIdentity(
        session_id=session_id,
        registry_backed=registry_backed,
    )


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(tmp_path / "claims.jsonl"))


def _review_pr(worktree: Path) -> PRData:
    return PRData(
        number=42, title="needs review", branch="feature/x",
        url="https://github.com/Boundless-Studios/gaia-free/pull/42",
        worktree_path=str(worktree), status=PRStatus.HAS_COMMENTS,
        review_comments=[ReviewComment(id=7, author="r", body="fix", created_at="2026-06-11T12:00:00Z")],
    )


def _stub(monkeypatch: pytest.MonkeyPatch, pr: PRData, *, await_alive: bool) -> None:
    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda cwd, sid: OWNER)
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, owner: await_alive)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: pr)
    monkeypatch.setattr(_pr_state_mod, "_unresolved_review_threads", lambda n, cwd: [])
    monkeypatch.setattr(_worktrees_mod, "_live_independent_owner_paths", lambda paths, sid: set())
    monkeypatch.setattr(_markers_mod, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda cwd: None)
    # required_checks_pending shells to gh — stub it False.
    from agentic_pr_dash import github_api
    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd: False)


def test_wakeless_owner_defers_until_the_horizon_then_takes_over(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _review_pr(worktree)
    _stub(monkeypatch, pr, await_alive=False)  # owner has NO live waiter → wake-less

    # Tick 1 — within the horizon: defers (warn-only, no dispatch).
    code1, text1 = maintenance_check._check_worktree(str(worktree), SID, claim=True)
    assert code1 == 0, text1
    assert "NOT clean" in text1 and OWNER in text1

    # Tick 2, immediately after — BOU-2475: still defers. Under the old one-tick
    # grace this took over, which is how a live owner lost its PR within seconds.
    code2, text2 = maintenance_check._check_worktree(str(worktree), SID, claim=True)
    assert code2 == 0, f"immediate retry must still defer, got {code2}: {text2}"

    # Past the horizon — take over and SERVICE the blocked PR. The BOU-1879
    # guarantee (a wake-less owner never strands its PR forever) is preserved.
    with _advance_clock(_wc.load_config(str(worktree)).wakeless_takeover_seconds + 1):
        code3, text3 = maintenance_check._check_worktree(str(worktree), SID, claim=True)
    assert code3 == 10, text3
    assert "PR_NUMBER=42" in text3


def test_wake_capable_owner_keeps_deferring(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _review_pr(worktree)
    _stub(monkeypatch, pr, await_alive=True)  # owner HAS a live waiter → wake-capable

    for _ in range(3):
        code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
        assert code == 0, text
        assert "NOT clean" in text  # never takes over from a wake-capable owner


def test_independent_owner_without_waiter_defers_once_then_takes_over(
    monkeypatch, tmp_path
):
    """The late independent-owner gate must not bypass wakeless takeover."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _review_pr(worktree)

    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(_markers_mod, "_live_pr_owner_record", lambda *args: None)
    monkeypatch.setattr(_markers_mod, "_live_pr_owner", lambda *args: None)
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda cwd: None)
    monkeypatch.setattr(_markers_mod, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, owner: False)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: pr)
    monkeypatch.setattr(_pr_state_mod, "_unresolved_review_threads", lambda n, cwd: [])
    monkeypatch.setattr(
        _worktrees_mod,
        "_live_independent_owner_sessions",
        lambda paths, sid: {
            str(worktree): (_independent_owner(OWNER, registry_backed=False),)
        },
    )
    from agentic_pr_dash import github_api

    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd: False)

    code1, text1 = maintenance_check._check_worktree(str(worktree), SID, claim=True)
    assert code1 == 0, text1
    assert "live independent owner" in text1

    # BOU-2475: the grace is now a wall-clock horizon, not one tick. An immediate
    # retry must still defer — this is the regression that let the loop seize a
    # live owner's PR seconds after first seeing it.
    code2, text2 = maintenance_check._check_worktree(str(worktree), SID, claim=True)
    assert code2 == 0, f"immediate retry must still defer, got {code2}: {text2}"

    # Past the horizon, takeover still happens — a wake-less owner cannot be told
    # to act, so the PR must not be stranded forever (BOU-1879 intent preserved).
    with _advance_clock(_wc.load_config(str(worktree)).wakeless_takeover_seconds + 1):
        code3, text3 = maintenance_check._check_worktree(str(worktree), SID, claim=True)
    assert code3 == 10, text3
    assert "PR_NUMBER=42" in text3


def test_independent_owner_with_waiter_keeps_deferring(monkeypatch, tmp_path):
    """A real waiter preserves one-executor ownership on the late gate."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _review_pr(worktree)

    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(_markers_mod, "_live_pr_owner_record", lambda *args: None)
    monkeypatch.setattr(_markers_mod, "_live_pr_owner", lambda *args: None)
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda cwd: None)
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, owner: True)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: pr)
    monkeypatch.setattr(_pr_state_mod, "_unresolved_review_threads", lambda n, cwd: [])
    monkeypatch.setattr(
        _worktrees_mod,
        "_live_independent_owner_sessions",
        lambda paths, sid: {
            str(worktree): (_independent_owner(OWNER, registry_backed=False),)
        },
    )
    from agentic_pr_dash import github_api

    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd: False)

    for _ in range(3):
        code, text = maintenance_check._check_worktree(
            str(worktree), SID, claim=True
        )
        assert code == 0, text
        assert "live independent owner" in text


def test_independent_owner_group_keeps_deferring_when_any_waiter_is_live(
    monkeypatch, tmp_path
):
    """Multiple sessions may share a worktree; any live waiter preserves ownership."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _review_pr(worktree)

    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(_markers_mod, "_live_pr_owner_record", lambda *args: None)
    monkeypatch.setattr(_markers_mod, "_live_pr_owner", lambda *args: None)
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda cwd: None)
    waiter_probes = []

    def _await_alive(cwd, owner):
        waiter_probes.append(owner)
        return owner == "sess-with-waiter"

    monkeypatch.setattr(
        _waiter_mod,
        "_await_alive",
        _await_alive,
    )
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: pr)
    monkeypatch.setattr(_pr_state_mod, "_unresolved_review_threads", lambda n, cwd: [])
    monkeypatch.setattr(
        _worktrees_mod,
        "_live_independent_owner_sessions",
        lambda paths, sid: {
            str(worktree): (
                _independent_owner(OWNER, registry_backed=False),
                _independent_owner("sess-with-waiter", registry_backed=False),
            )
        },
    )
    from agentic_pr_dash import github_api

    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd: False)

    for _ in range(3):
        code, text = maintenance_check._check_worktree(
            str(worktree), SID, claim=True
        )
        assert code == 0, text
        assert "live independent owner" in text
    assert "sess-with-waiter" in waiter_probes


def test_registered_independent_owner_without_waiter_keeps_deferring(
    monkeypatch, tmp_path
):
    """A live registry session remains primary even without a wake waiter."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _review_pr(worktree)

    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(_markers_mod, "_live_pr_owner_record", lambda *args: None)
    monkeypatch.setattr(_markers_mod, "_live_pr_owner", lambda *args: None)
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda cwd: None)
    monkeypatch.setattr(_markers_mod, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, owner: False)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: pr)
    monkeypatch.setattr(_pr_state_mod, "_unresolved_review_threads", lambda n, cwd: [])
    monkeypatch.setattr(
        _worktrees_mod,
        "_live_independent_owner_sessions",
        lambda paths, sid: {
            str(worktree): (_independent_owner(OWNER, registry_backed=True),)
        },
    )
    from agentic_pr_dash import github_api

    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd: False)

    for _ in range(3):
        code, text = maintenance_check._check_worktree(
            str(worktree), SID, claim=True
        )
        assert code == 0, text
        assert "live independent owner" in text
