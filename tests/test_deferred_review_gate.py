"""BOU-2567 — the shared check/stop-gate/loop-dispatch engine must not read a
deliberately-deferred review thread as unaddressed work.

``_check_worktree`` (``_maintenance/worktree_check.py``) is the single engine
behind three surfaces that all misread "unresolved" as "unaddressed" before
this fix:

  * ``maintenance_check check`` — read directly by the operator/session.
  * ``maintenance_check stop-gate`` — the Stop-hook gate (this file also pins
    that surface end-to-end via ``mc.main(["stop-gate", ...])``).
  * the pr-maintenance loop — ``loop._service_cwd`` shells out to
    ``agentic-pr-dash check`` for its dispatch decision, so fixing the check
    path fixes the loop's dispatch too (BOU-2567 ticket: "worktree_check.
    _check_worktree is the shared choke point — verify").

These tests were run against pre-fix code to confirm the RED failure (the
"does not block" tests returned exit 10 / rc 2, not 0) before
``_maintenance/pr_state.py`` and ``_maintenance/deferred_review.py`` were wired
together.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentic_pr_dash import config
from agentic_pr_dash import github_api
from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment
from agentic_pr_dash.models import PRData, PRStatus
from agentic_pr_dash._maintenance import deferred_review as dr
from agentic_pr_dash._maintenance import markers as _markers_mod
from agentic_pr_dash._maintenance import pr_state as _pr_state_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod

SID = "sess-defer-gate"
PR_NUMBER = 2863


def _thread(node_id: str = "T1", *, resolved: bool = False) -> ReviewThread:
    c = ReviewThreadComment(
        database_id=42, path="f.py", line=7, body="please fix",
        author="rev", created_at="2026-01-01T00:00:00Z",
    )
    return ReviewThread(node_id=node_id, is_resolved=resolved, is_outdated=False, top=c)


def _clean_pr(**kw) -> PRData:
    base = dict(
        number=PR_NUMBER, title="t", branch="b", url=f"https://x/pull/{PR_NUMBER}",
        failing_checks=[], review_comments=[], merge_state="CLEAN",
        latest_commit_sha="sha", worktree_path="/wt", status=PRStatus.CLEAN,
    )
    base.update(kw)
    return PRData(**base)


def _patch_check_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize ownership/heartbeat side effects so ``_check_worktree`` is pure."""
    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(
        _worktrees_mod, "_live_independent_owner_paths", lambda paths, sid: set()
    )
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda cwd: None)
    monkeypatch.setattr(_markers_mod, "_touch_owner_heartbeat", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# check / loop dispatch (shared engine: _check_worktree)
# ---------------------------------------------------------------------------


def test_control_undeferred_thread_still_blocks(tmp_path: Path, monkeypatch) -> None:
    """Baseline: a plain unresolved thread blocks — the case the deferred
    variant below is contrasted against."""
    _patch_check_env(monkeypatch)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: _clean_pr())
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None, **kw: [_thread()])

    code, text = mc._check_worktree(str(tmp_path), SID)

    assert code == 10, text
    assert f"PR_NUMBER={PR_NUMBER}" in text


def test_deferred_thread_does_not_block_check_or_loop_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    """The fix: once a thread is deferred with a tracked follow-up, the SAME
    engine that dispatches the loop's executor and blocks the stop gate must
    read the PR as clean — not merely stop printing the comment body."""
    _patch_check_env(monkeypatch)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: _clean_pr())
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None, **kw: [_thread()])
    dr.defer_thread(
        str(tmp_path), PR_NUMBER, thread_id="T1", comment_id=42, severity="P1",
        ticket="BOU-2559", reason="out of scope: requires files this PR does not own",
        deferred_by=SID,
    )

    code, text = mc._check_worktree(str(tmp_path), SID)

    assert code == 0, text
    assert f"PR_NUMBER={PR_NUMBER}" not in text  # never built a dispatch prompt


def test_deferred_count_is_reported_distinctly_from_unresolved(
    tmp_path: Path, monkeypatch
) -> None:
    """Acceptance: 'the gate distinguishes deferred from unresolved in its
    output' — a clean check must still SAY a thread is deferred, not go
    silent, so a deferred count is never indistinguishable from zero
    unresolved threads."""
    _patch_check_env(monkeypatch)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: _clean_pr())
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None, **kw: [_thread()])
    dr.defer_thread(
        str(tmp_path), PR_NUMBER, thread_id="T1", comment_id=42, severity="P2",
        ticket="BOU-1000",
    )

    code, text = mc._check_worktree(str(tmp_path), SID)

    assert code == 0
    assert "deferred: 1" in text or "deferred=1" in text.lower(), (
        f"clean-check text must surface a deferred count, got: {text!r}"
    )


def test_mixed_one_deferred_one_live_thread_still_blocks_on_the_live_one(
    tmp_path: Path, monkeypatch
) -> None:
    """A deferred thread must not blanket-suppress OTHER, still-live unresolved
    threads on the same PR."""
    _patch_check_env(monkeypatch)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: _clean_pr())
    monkeypatch.setattr(
        github_api, "get_review_threads",
        lambda pr, cwd=None, **kw: [_thread("T1"), _thread("T2")],
    )
    dr.defer_thread(
        str(tmp_path), PR_NUMBER, thread_id="T1", comment_id=42, severity="P2",
        ticket="BOU-1000",
    )

    code, text = mc._check_worktree(str(tmp_path), SID)

    assert code == 10, text  # T2 still unresolved -> still dispatches
    assert f"PR_NUMBER={PR_NUMBER}" in text


# ---------------------------------------------------------------------------
# stop-gate end-to-end
# ---------------------------------------------------------------------------


def test_stop_gate_does_not_block_on_a_deferred_only_pr(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end reproduction of the BOU-2567 symptom: PR #2863, CI green,
    every unresolved thread deferred with a filed ticket + a reply — the stop
    gate must exit 0, not re-block a converged PR forever."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    config.load.cache_clear()

    _patch_check_env(monkeypatch)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: _clean_pr())
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None, **kw: [_thread()])
    monkeypatch.setattr(
        _worktrees_mod, "_owned_worktrees_across_roots",
        lambda session_id, anchor: [str(tmp_path)],
    )
    monkeypatch.setattr(
        _worktrees_mod, "_reconcile_owned_across_roots",
        lambda session_id, anchor, pid, deadline=None: ([str(tmp_path)], []),
    )
    monkeypatch.setattr(
        _worktrees_mod, "_detached_records_across_roots",
        lambda session_id, anchor: [],
    )
    dr.defer_thread(
        str(tmp_path), PR_NUMBER, thread_id="T1", comment_id=42, severity="P1",
        ticket="BOU-2559", reason="out of scope: requires files this PR does not own",
        deferred_by=SID,
    )

    rc = mc.main(
        ["stop-gate", "--cwd", str(tmp_path), "--session-id", SID, "--no-waiter"]
    )

    assert rc == 0, "a PR whose only unresolved threads are deferred must not block the stop"


def test_stop_gate_surfaces_deferred_count_on_an_otherwise_clean_pass(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """BOU-2567 PR #122 review, P1 #3 (elevated from P2): `check` returns
    'nothing pending (deferred: N)' for a deferred-only PR, but `_stop_gate_impl`
    discarded ordinary code==0 text while walking owned worktrees, so the
    stop-gate surface itself emitted NO deferred count at all -- a stated
    behavior ("the gate distinguishes deferred from unresolved in its output")
    that did not actually hold at the stop-gate layer specifically."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    config.load.cache_clear()

    _patch_check_env(monkeypatch)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: _clean_pr())
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None, **kw: [_thread()])
    monkeypatch.setattr(
        _worktrees_mod, "_owned_worktrees_across_roots",
        lambda session_id, anchor: [str(tmp_path)],
    )
    monkeypatch.setattr(
        _worktrees_mod, "_reconcile_owned_across_roots",
        lambda session_id, anchor, pid, deadline=None: ([str(tmp_path)], []),
    )
    monkeypatch.setattr(
        _worktrees_mod, "_detached_records_across_roots",
        lambda session_id, anchor: [],
    )
    dr.defer_thread(
        str(tmp_path), PR_NUMBER, thread_id="T1", comment_id=42, severity="P2",
        ticket="BOU-1000",
    )

    rc = mc.main(
        ["stop-gate", "--cwd", str(tmp_path), "--session-id", SID, "--no-waiter"]
    )

    assert rc == 0
    err = capsys.readouterr().err
    assert "1" in err and "deferred" in err.lower(), (
        f"stop-gate must surface the deferred count on an otherwise-clean "
        f"pass, not go silent; stderr was: {err!r}"
    )
