"""BOU-1789 — codex PR #50 review round 4 regression guards.

(1) corrupt per-PR health entries coerce to streak 0 (don't raise → release w/o waiter)
(2) escalation_failure_threshold validated/clamped (bad value must not break load())
(3) required_checks_pending paginates the rollup contexts
(4) escalation surfaces even when the escalated PR still has blockers (in `pending`)
"""
from __future__ import annotations

import json
import types

import pytest

from agentic_pr_dash import config, github_api, loop
from agentic_pr_dash._maintenance import stop_gate as _stop_gate_mod


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_PR_DASH_DAEMON_DIR", str(tmp_path / "daemons"))
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(tmp_path / "daemons"))
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _result(rc, stdout):
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr="")


# (1) corrupt health entries -------------------------------------------------

def test_executor_failure_streak_coerces_corrupt_entry(monkeypatch, tmp_path):
    cwd = str(tmp_path)
    monkeypatch.setattr(loop, "_repo_slug", lambda c: "testrepo")
    # Non-dict entry and unparseable streak must both coerce to 0, not raise.
    monkeypatch.setattr(loop, "_load_health", lambda c: {"42": [], "43": {"streak": "x"}})
    assert loop.executor_failure_streak(cwd, 42) == 0
    assert loop.executor_failure_streak(cwd, 43) == 0


def test_record_executor_failure_recovers_from_corrupt_entry(monkeypatch, tmp_path):
    cwd = str(tmp_path)
    monkeypatch.setattr(loop, "_repo_slug", lambda c: "testrepo")
    saved = {}
    monkeypatch.setattr(loop, "_load_health", lambda c: {"42": "garbage"})
    monkeypatch.setattr(loop, "_save_health", lambda c, d: saved.update(d))
    assert loop.record_executor_failure(cwd, 42, "boom") == 1  # treats garbage as 0 → 1


# (2) config threshold validation -------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("5", 5), ("abc", 3), ("0", 3), ("-2", 3), ("1", 1),
])
def test_escalation_threshold_validated(monkeypatch, raw, expected):
    monkeypatch.setenv("AGENTIC_PR_DASH_ESCALATION_THRESHOLD", raw)
    config.load.cache_clear()
    assert config.load(".").escalation_failure_threshold == expected


# (3) rollup pagination ------------------------------------------------------

def _page(nodes, *, has_next, cursor="C1"):
    return json.dumps({"data": {"repository": {"pullRequest": {"commits": {"nodes": [
        {"commit": {"statusCheckRollup": {"contexts": {
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            "nodes": nodes,
        }}}}
    ]}}}}})


def test_required_checks_pending_follows_pagination(monkeypatch):
    monkeypatch.setenv("AGENTIC_PR_DASH_REPO", "owner/name")
    config.load.cache_clear()
    pages = [
        # page 1: no required pending, but more pages
        _page([{"__typename": "CheckRun", "status": "COMPLETED", "isRequired": True}], has_next=True),
        # page 2: a required EXPECTED context
        _page([{"__typename": "StatusContext", "state": "EXPECTED", "isRequired": True}], has_next=False),
    ]
    calls = {"n": 0}
    def fake_run(cmd, **kw):
        i = calls["n"]; calls["n"] += 1
        return _result(0, pages[min(i, len(pages) - 1)])
    monkeypatch.setattr(github_api, "_run", fake_run)
    assert github_api.required_checks_pending(42) is True
    assert calls["n"] == 2  # followed to page 2


# (4) escalation surfaces alongside pending blockers -------------------------

def test_escalation_block_printed_when_escalated_pr_has_blockers(monkeypatch, tmp_path, capsys):
    """An escalated PR almost always still has blockers (it lands in `pending`);
    the escalation explanation must still print, not be skipped (codex review)."""
    import os
    from agentic_pr_dash import maintenance_check as mc
    from agentic_pr_dash._maintenance import worktree_check as _wc_mod
    from agentic_pr_dash._maintenance import reconcile as _rec_mod
    from agentic_pr_dash._maintenance import worktrees as _wt_mod

    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    config.load.cache_clear()
    wt = tmp_path / "wt"; wt.mkdir()
    SID = "sess-r4"
    mc._write_arm_marker(str(wt), SID, os.getpid(), 42)

    monkeypatch.setattr(_wt_mod, "_owned_worktrees_across_roots", lambda sid, cwd: [str(wt)])
    # PR 42 has a blocker → _check_worktree returns 10 → it lands in `pending`.
    monkeypatch.setattr(_wc_mod, "_check_worktree",
                        lambda path, sid, *, claim=True: (10, "PR #42 has failing CI\nPR_NUMBER=42"))
    monkeypatch.setattr(_rec_mod, "_detached_pr_records",
                        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(_stop_gate_mod, "_read_escalation_marker",
                        lambda c: {"42": {"streak": 3, "last_error": "dns outage"}})

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2
    assert "ESCALATION" in err
    assert "dns outage" in err
