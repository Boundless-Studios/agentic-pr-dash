"""Waiter resilience: machine-readable exit outcome (BOU-1877) + crash-safe
config load when the process cwd is deleted out from under a long-lived waiter
(BOU-1905).

Both belong to the harness-maturity Phase 1 correctness sweep (BOU-1862):

* BOU-1877 — every ``await`` exit path emits exactly ONE machine-readable JSON
  line to stderr: ``{"outcome": "deferred_to_loop" | "woke" | "error", ...}`` so
  a supervising session can tell "deferred to the loop, covered" from "woke with
  feedback" from "died" (an exit code alone can't — 0 means both idle and
  deferred).
* BOU-1905 — ``config.load()`` falls back to ``os.getcwd()`` when given no cwd;
  a detached waiter routinely outlives its cwd (stale-worktree reaping deletes
  directories under running processes), so that call raised FileNotFoundError
  and the waiter died with a raw traceback. It must survive, and any residual
  crash must still emit the structured ``error`` outcome.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_pr_dash import config, maintenance_check as mc
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod


SID = "sess-waiter-resilience"


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", "/tmp/test-ledger-resilience-UNUSED")
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _outcome_lines(err: str) -> list[dict]:
    """Every pure-JSON stderr line carrying an ``outcome`` key."""
    out: list[dict] = []
    for raw in err.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "outcome" in parsed:
            out.append(parsed)
    return out


# --------------------------------------------------------------------------
# BOU-1905 — config.load survives a vanished cwd
# --------------------------------------------------------------------------


def test_config_load_survives_vanished_cwd(monkeypatch):
    """load() with no cwd arg must not crash when os.getcwd()/Path.cwd() raises."""
    config.load.cache_clear()

    def _boom(*_a, **_k):
        raise FileNotFoundError("[Errno 2] No such file or directory (cwd deleted)")

    # Path.cwd() is what load() falls back to when cwd is None.
    monkeypatch.setattr(config.Path, "cwd", staticmethod(_boom))

    cfg = config.load()  # no cwd -> would previously raise FileNotFoundError
    assert cfg is not None
    # It's a real, usable Config (has the resolved state-dir helper).
    assert hasattr(cfg, "state_dir_for")


# --------------------------------------------------------------------------
# BOU-1877 — machine-readable exit outcome on every await exit path
# --------------------------------------------------------------------------


def test_await_emits_woke_outcome_on_exit_10(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(tmp_path / "worktree")],
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    (tmp_path / "worktree").mkdir()
    monkeypatch.setattr(mc, "_check_worktree", lambda p, sid, *, claim=True: (10, "pending\nPR_NUMBER=5"))

    rc = mc.main(["await", "--cwd", str(tmp_path), "--session-id", SID,
                  "--owner-pid", "12345", "--max-wait", "1", "--interval", "1"])
    err = capsys.readouterr().err
    assert rc == 10
    outcomes = _outcome_lines(err)
    assert len(outcomes) == 1, f"expected exactly one outcome line, got {outcomes}"
    assert outcomes[0]["outcome"] == "woke"


def test_await_emits_deferred_outcome_on_exit_0(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [])
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )

    rc = mc.main(["await", "--cwd", str(tmp_path), "--session-id", SID,
                  "--owner-pid", "12345", "--max-wait", "1"])
    err = capsys.readouterr().err
    assert rc == 0
    outcomes = _outcome_lines(err)
    assert len(outcomes) == 1, f"expected exactly one outcome line, got {outcomes}"
    assert outcomes[0]["outcome"] == "deferred_to_loop"


def test_await_emits_deferred_outcome_on_single_instance_exit_3(tmp_path, monkeypatch, capsys):
    import os
    live_pid = os.getpid()
    path = Path(mc._await_pidfile(str(tmp_path), SID))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": live_pid, "session_id": SID}), encoding="utf-8")
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: str(pid) == str(live_pid))

    rc = mc.main(["await", "--cwd", str(tmp_path), "--session-id", SID,
                  "--owner-pid", "12345", "--max-wait", "1"])
    err = capsys.readouterr().err
    assert rc == 3
    outcomes = _outcome_lines(err)
    assert len(outcomes) == 1, f"expected exactly one outcome line, got {outcomes}"
    assert outcomes[0]["outcome"] == "deferred_to_loop"


def test_await_emits_error_outcome_on_exception(tmp_path, monkeypatch, capsys):
    """A deep helper raising (e.g. FileNotFoundError from a deleted cwd) must not
    escape as a raw traceback — the waiter exits 1 with a structured error line."""
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)

    def _boom(*_a, **_k):
        raise FileNotFoundError("cwd vanished mid-poll")

    monkeypatch.setattr(mc, "_update_await_coverage", _boom)

    rc = mc.main(["await", "--cwd", str(tmp_path), "--session-id", SID,
                  "--owner-pid", "12345", "--max-wait", "1"])
    err = capsys.readouterr().err
    assert rc == 1
    outcomes = _outcome_lines(err)
    assert len(outcomes) == 1, f"expected exactly one outcome line, got {outcomes}"
    assert outcomes[0]["outcome"] == "error"
    assert "FileNotFoundError" in outcomes[0].get("reason", "")
