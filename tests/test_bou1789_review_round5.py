"""BOU-1789 — codex PR #50 review round 5 regression guards.

(1) required_checks_pending resolves the repo from the cwd, not a pinned/global config.
(2) stop-gate keeps repo identity: same PR number in two repos isn't collapsed —
    a PR uncovered in ANY repo forces the waiter.
"""
from __future__ import annotations

import os
import types

import pytest

from agentic_pr_dash import config, github_api
from agentic_pr_dash._maintenance import stop_gate as _stop_gate_mod


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(tmp_path / "daemons"))
    monkeypatch.setenv("AGENTIC_PR_DASH_DAEMON_DIR", str(tmp_path / "daemons"))
    config.load.cache_clear()
    yield
    config.load.cache_clear()


# (1) repo resolved from cwd, not the pinned/global config ------------------

def test_repo_for_cwd_prefers_cwd_remote_over_pinned(monkeypatch):
    from agentic_pr_dash import config as _cfg
    # Pinned/global repo (e.g. AGENTIC_PR_DASH_REPO) — must NOT win over cwd.
    monkeypatch.setenv("AGENTIC_PR_DASH_REPO", "anchor/repo")
    _cfg.load.cache_clear()
    monkeypatch.setattr(_cfg, "_detect_repo", lambda p: "sibling/repo")
    assert github_api._repo_for_cwd("/some/sibling/worktree") == "sibling/repo"


def test_repo_for_cwd_falls_back_to_config_when_undetected(monkeypatch):
    from agentic_pr_dash import config as _cfg
    monkeypatch.setenv("AGENTIC_PR_DASH_REPO", "anchor/repo")
    _cfg.load.cache_clear()
    monkeypatch.setattr(_cfg, "_detect_repo", lambda p: None)
    assert github_api._repo_for_cwd("/some/path") == "anchor/repo"


def test_required_checks_pending_queries_cwd_repo(monkeypatch):
    from agentic_pr_dash import config as _cfg
    monkeypatch.setenv("AGENTIC_PR_DASH_REPO", "anchor/repo")
    _cfg.load.cache_clear()
    monkeypatch.setattr(_cfg, "_detect_repo", lambda p: "sibling/repo")
    captured = {}
    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout='{"data":{}}', stderr="")
    monkeypatch.setattr(github_api, "_run", fake_run)
    github_api.required_checks_pending(42, cwd="/some/sibling/worktree")
    assert "owner=sibling" in captured["cmd"]
    assert "name=repo" in captured["cmd"]


# (2) same PR number across repos isn't collapsed ---------------------------

def test_stop_gate_same_pr_number_uncovered_in_one_repo_forces_waiter(monkeypatch, tmp_path):
    """PR #42 exists in repo A (loop-covered) AND repo B (streak at threshold).
    The bare-number key must not collapse them — #42 must be treated as
    uncovered (waiter forced) because repo B's instance is uncovered."""
    from agentic_pr_dash import maintenance_check as mc
    from agentic_pr_dash._maintenance import worktree_check as _wc_mod
    from agentic_pr_dash._maintenance import reconcile as _rec_mod
    from agentic_pr_dash._maintenance import worktrees as _wt_mod
    from agentic_pr_dash._maintenance import waiter as _waiter_mod
    from agentic_pr_dash import loop as _loop_mod

    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    config.load.cache_clear()
    wt_a = tmp_path / "repo-a"; wt_a.mkdir()
    wt_b = tmp_path / "repo-b"; wt_b.mkdir()
    SID = "sess-r5"

    monkeypatch.setattr(_wt_mod, "_owned_worktrees_across_roots",
                        lambda sid, cwd: [str(wt_a), str(wt_b)])
    monkeypatch.setattr(_wc_mod, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_rec_mod, "_detached_pr_records",
                        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_pairs",
                        lambda owned: [(str(wt_a), 42), (str(wt_b), 42)])
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(_stop_gate_mod, "_read_escalation_marker", lambda c: {})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)
    # Covered in repo-a, NOT covered in repo-b.
    monkeypatch.setattr(_loop_mod, "_loop_covers_pr",
                        lambda cwd, pr: "repo-a" in str(cwd))

    rc = mc.main(["stop-gate", "--cwd", str(wt_a), "--session-id", SID])
    assert rc == 2  # repo-b's #42 uncovered → waiter forced (not collapsed away)
