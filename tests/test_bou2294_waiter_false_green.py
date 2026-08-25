"""Regression tests for BOU-2294: the waiter's false green.

Reproduced on gaia PR #2707: seconds after a push, with four CI jobs
IN_PROGRESS, the `await` waiter printed "all watched PRs are clean (no pending
feedback, required CI terminal)" and exited 0. One of those jobs then went red
with no waiter left alive to wake the session.

Two independent defects produced that:

1. ``required_checks_pending`` counted ONLY branch-protection-required contexts.
   gaia gates merges by convention, so every context reports
   ``isRequired: false`` and running CI read as terminal. Failure detection has
   no such filter, so CI could be found red but never found running.
2. An empty watch set rendered as a clean verdict. A waiter that resolved no PR
   said "everything I watch is clean" instead of "I watched nothing", and its
   marker-only PR resolution went blind when BOU-2223 Stage 4 retired the
   ``pr-watch.armed`` writer.
"""
from __future__ import annotations

import json
import os
import types

import pytest

from agentic_pr_dash import config, github_api, maintenance_check as mc, ownership
from agentic_pr_dash._maintenance import waiter as _waiter_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import ownership_resolution as _ownership_resolution_mod

SID = "sess-bou2294"


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("AGENTIC_PR_DASH_WAITER_DIR", str(tmp_path / "waiters"))
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(tmp_path / "empty-daemons"))
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _await_args(cwd):
    return ["await", "--cwd", str(cwd), "--session-id", SID,
            "--owner-pid", str(os.getpid()), "--max-wait=-1", "--interval", "0"]


# ---------------------------------------------------------------------------
# Defect 1: running CI in a repo with no branch-protection-required checks
# ---------------------------------------------------------------------------


def _rollup(*contexts) -> str:
    return json.dumps({
        "data": {"repository": {"pullRequest": {"commits": {"nodes": [
            {"commit": {"statusCheckRollup": {"contexts": {"nodes": list(contexts)}}}}
        ]}}}}
    })


def _pin_repo(monkeypatch):
    monkeypatch.setenv("AGENTIC_PR_DASH_REPO", "owner/name")
    config.load.cache_clear()


def test_pending_when_no_context_is_branch_protection_required(monkeypatch):
    """The live gaia shape: every context ``isRequired: false``, jobs running."""
    _pin_repo(monkeypatch)
    monkeypatch.setattr(github_api, "_run", lambda cmd, **kw: types.SimpleNamespace(
        returncode=0, stderr="", stdout=_rollup(
            {"__typename": "CheckRun", "status": "IN_PROGRESS", "isRequired": False},
            {"__typename": "CheckRun", "status": "COMPLETED", "isRequired": False},
        )))
    assert github_api.required_checks_pending(2707) is True


def test_terminal_when_no_required_context_and_all_checks_done(monkeypatch):
    _pin_repo(monkeypatch)
    monkeypatch.setattr(github_api, "_run", lambda cmd, **kw: types.SimpleNamespace(
        returncode=0, stderr="", stdout=_rollup(
            {"__typename": "CheckRun", "status": "COMPLETED", "isRequired": False},
            {"__typename": "StatusContext", "state": "SUCCESS", "isRequired": False},
        )))
    assert github_api.required_checks_pending(2707) is False


def test_optional_pending_still_ignored_when_the_repo_declares_required_checks(monkeypatch):
    """The fallback is scoped: a repo that DOES gate on branch protection keeps
    the narrow reading, so a slow optional job cannot hold a waiter open."""
    _pin_repo(monkeypatch)
    monkeypatch.setattr(github_api, "_run", lambda cmd, **kw: types.SimpleNamespace(
        returncode=0, stderr="", stdout=_rollup(
            {"__typename": "CheckRun", "status": "IN_PROGRESS", "isRequired": False},
            {"__typename": "CheckRun", "status": "COMPLETED", "isRequired": True},
        )))
    assert github_api.required_checks_pending(2707) is False


def test_required_context_on_a_later_page_still_scopes_the_fallback(monkeypatch):
    """"No required contexts" is a whole-rollup fact: a required context on page
    2 must suppress page 1's optional-pending fallback."""
    _pin_repo(monkeypatch)
    pages = [
        json.dumps({"data": {"repository": {"pullRequest": {"commits": {"nodes": [
            {"commit": {"statusCheckRollup": {"contexts": {
                "nodes": [{"__typename": "CheckRun", "status": "IN_PROGRESS",
                           "isRequired": False}],
                "pageInfo": {"hasNextPage": True, "endCursor": "cur"}}}}}]}}}}}),
        json.dumps({"data": {"repository": {"pullRequest": {"commits": {"nodes": [
            {"commit": {"statusCheckRollup": {"contexts": {
                "nodes": [{"__typename": "CheckRun", "status": "COMPLETED",
                           "isRequired": True}],
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}]}}}}}),
    ]
    calls = [0]

    def fake_run(cmd, **kw):
        calls[0] += 1
        return types.SimpleNamespace(returncode=0, stderr="", stdout=pages[calls[0] - 1])

    monkeypatch.setattr(github_api, "_run", fake_run)
    assert github_api.required_checks_pending(2707) is False


# ---------------------------------------------------------------------------
# Defect 2a: an empty watch set is not a verdict
# ---------------------------------------------------------------------------


def test_unbound_waiter_does_not_report_clean(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [])
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])

    rc = mc.main(_await_args(tmp_path))

    captured = capsys.readouterr()
    assert rc == mc._AWAIT_UNBOUND
    assert rc != 0, "a wrong answer that looks right must be a loud failure"
    assert rc != 10, "must not masquerade as the harness's wake signal"
    assert "all watched prs are clean" not in captured.out.lower()
    assert json.loads(
        [line for line in captured.err.splitlines() if line.startswith("{")][-1]
    )["outcome"] == "unbound"
    # No verdict recorded means the stop gate keeps demanding coverage.
    assert _waiter_mod._read_clean_exit_keys(SID) == set()


def test_owned_worktree_with_no_open_pr_is_unbound_not_clean(tmp_path, monkeypatch, capsys):
    """Bound to a worktree but to no PR is still "watched nothing"."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees",
                        lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(mc, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)
    monkeypatch.setattr("time.sleep", lambda s: None)

    rc = mc.main(_await_args(tmp_path))

    assert rc == mc._AWAIT_UNBOUND
    assert "all watched prs are clean" not in capsys.readouterr().out.lower()
    assert _waiter_mod._read_clean_exit_keys(SID) == set()


def test_unbound_exit_clears_a_stale_clean_verdict(tmp_path, monkeypatch):
    """A previous tick's verdict must not keep suppressing coverage once the
    waiter can no longer resolve what it owns."""
    _waiter_mod._write_clean_exit_marker(SID, {"owner/name#7"})
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [])
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])

    assert mc.main(_await_args(tmp_path)) == mc._AWAIT_UNBOUND
    assert _waiter_mod._read_clean_exit_keys(SID) == set()


# ---------------------------------------------------------------------------
# Defect 2b: the waiter must bind to a PR this session owns by CLAIM
# ---------------------------------------------------------------------------


def test_waiter_binds_to_a_claim_owned_pr_without_any_marker(tmp_path):
    """BOU-2223 Stage 4 retired the ``pr-watch.armed`` writer, so a session that
    owns an open PR has a live claim and NO marker. The waiter's PR resolution
    must find it — a marker-only read answers "you own nothing" and the whole
    clean-exit verdict is computed over an empty set."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    outcome = ownership.record_ownership(
        repo="owner/name", pr_number=2707, session_id=SID, pid=os.getpid(),
        worktree_path=str(wt),
    )
    assert outcome.ok, outcome.reason

    assert mc._owned_pr_pairs_for_await([str(wt)]) == [(str(wt), 2707)]


def test_claim_owned_waiter_records_its_clean_verdict(tmp_path, monkeypatch, capsys):
    """End to end: a claim-owned, marker-less worktree with a clean PR reaches
    the clean exit and records WHICH PR it verified."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    ownership.record_ownership(
        repo="owner/name", pr_number=2707, session_id=SID, pid=os.getpid(),
        worktree_path=str(wt),
    )
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees",
                        lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(mc, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)
    monkeypatch.setattr(mc, "_marker_pr_still_current", lambda wt_, n: True)
    monkeypatch.setattr(
        _ownership_resolution_mod,
        "resolve_current_prs",
        lambda worktrees, session_id="", **kwargs: {
            worktree: _ownership_resolution_mod.CurrentPRResolution(
                worktree=worktree,
                branch="test-branch",
                pr_number=2707,
                head_sha="test-head",
                resolved=True,
            )
            for worktree in worktrees
        },
    )
    monkeypatch.setattr(
        "agentic_pr_dash._maintenance.stop_gate._current_branch",
        lambda cwd: "test-branch",
    )
    monkeypatch.setattr(
        "agentic_pr_dash._maintenance.stop_gate._local_head_sha",
        lambda cwd: "test-head",
    )
    monkeypatch.setattr("time.sleep", lambda s: None)

    rc = mc.main(_await_args(tmp_path))

    assert rc == 0
    assert "all watched prs are clean" in capsys.readouterr().out.lower()
    assert any(key.endswith("#2707") for key in _waiter_mod._read_clean_exit_keys(SID))
