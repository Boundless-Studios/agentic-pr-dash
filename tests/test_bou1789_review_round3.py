"""BOU-1789 — codex PR #50 review round 3 regression guards.

(1) get_ci_checks parses stdout on non-zero rc (failing/pending checks were dropped).
(2) detached (ledger-only) PRs carry ci_watch_pending so the await waiter watches them.
(3) stop-gate checks loop-coverage/escalation against each PR's OWN worktree repo.
"""
from __future__ import annotations

import json
import os
import types

import pytest

from agentic_pr_dash import config, github_api
from agentic_pr_dash._maintenance import stop_gate as _stop_gate_mod


def _result(rc, stdout):
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr="")


# --------------------------------------------------------------------------- #
# (1) get_ci_checks must parse stdout even when gh exits non-zero
# --------------------------------------------------------------------------- #

def test_get_ci_checks_parses_failure_on_nonzero_rc(monkeypatch):
    """gh pr checks exits 1 when a check FAILS but still prints the JSON; the
    old `if rc != 0: return []` dropped it, so failing_checks stayed empty and
    the await never woke on failure."""
    checks = [{"name": "test", "bucket": "fail", "state": "completed"}]
    monkeypatch.setattr(github_api, "_run", lambda cmd, **kw: _result(1, json.dumps(checks)))
    result = github_api.get_ci_checks(7)
    assert [(c.name, c.conclusion) for c in result] == [("test", "failure")]


def test_get_ci_checks_parses_pending_on_exit_8(monkeypatch):
    checks = [{"name": "build", "bucket": "pending", "state": "in_progress"}]
    monkeypatch.setattr(github_api, "_run", lambda cmd, **kw: _result(8, json.dumps(checks)))
    result = github_api.get_ci_checks(7)
    assert [(c.name, c.status) for c in result] == [("build", "in_progress")]


def test_get_ci_checks_empty_on_unparseable(monkeypatch):
    monkeypatch.setattr(github_api, "_run", lambda cmd, **kw: _result(1, ""))
    assert github_api.get_ci_checks(7) == []


# --------------------------------------------------------------------------- #
# (3) stop-gate routes per-PR coverage/escalation to the PR's own worktree
# --------------------------------------------------------------------------- #

def test_owned_open_pr_pairs_preserves_worktree(monkeypatch, tmp_path):
    """_owned_open_pr_pairs returns (worktree, pr) so repo-scoped lookups can
    target each PR's own repo instead of the stop-gate anchor."""
    wt_a = tmp_path / "repo-a-wt"; wt_a.mkdir()
    wt_b = tmp_path / "repo-b-wt"; wt_b.mkdir()
    monkeypatch.setattr(_stop_gate_mod, "_read_marker",
                        lambda wt: {"pr": "11"} if "repo-a" in str(wt) else {"pr": "22"})
    pairs = _stop_gate_mod._owned_open_pr_pairs([str(wt_a), str(wt_b)])
    assert (str(wt_a), 11) in pairs
    assert (str(wt_b), 22) in pairs


# --------------------------------------------------------------------------- #
# (2) detached (ledger-only) PRs carry ci_watch_pending for the await waiter
# --------------------------------------------------------------------------- #

def test_detached_record_carries_ci_watch_pending(monkeypatch, tmp_path):
    """A detached PR whose required CI is still running is marked
    ci_watch_pending=True so _cmd_await keeps the waiter alive past --max-wait
    even when no live worktree (`owned`) remains (codex PR #50 review)."""
    from agentic_pr_dash import github_api as _gh
    from agentic_pr_dash._maintenance import reconcile as _rec
    from agentic_pr_dash import session_ledger

    monkeypatch.setenv("AGENTIC_PR_DASH_REPO", "owner/name")
    config.load.cache_clear()

    entry = types.SimpleNamespace(pr=77, branch="feat/x", worktree="",
                                  baseline_sha="", repo="owner/name")
    monkeypatch.setattr(session_ledger, "read", lambda *a, **k: [entry])
    # Open PR, no failing CI, no threads, no conflict — but required CI running.
    monkeypatch.setattr(_rec, "_pr_open_state",
                        lambda pr, cwd: ("open", "http://x/77", False, [], "REVIEW_REQUIRED", "BLOCKED", "MERGEABLE", "head-77"))
    monkeypatch.setattr(_rec, "_unpack_pr_open_state", lambda s: s)
    monkeypatch.setattr(_gh, "get_review_threads", lambda pr, cwd: [])
    monkeypatch.setattr(_gh, "required_checks_pending", lambda pr, cwd: True)
    monkeypatch.setattr(_rec, "_present_worktree_paths", lambda *a, **k: set(), raising=False)

    records = _rec._detached_pr_records("sess", str(tmp_path))
    rec = next(r for r in records if r["pr"] == 77)
    assert rec["ci_watch_pending"] is True
    assert rec["head_sha"] == "head-77"
    config.load.cache_clear()
