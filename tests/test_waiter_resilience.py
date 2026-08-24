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
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_pr_dash import config, github_api, maintenance_check as mc, ownership
from agentic_pr_dash._maintenance import waiter as _waiter_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod


def _force_rate_limit_seen(monkeypatch, value):
    """Pin github_api.rate_limit_seen() (a callable or bool) and neutralize the
    per-tick reset so the waiter loop observes the intended rate-limit state
    (real gh isn't exercised in these hermetic tests)."""
    monkeypatch.setattr(github_api, "reset_rate_limit_seen", lambda: None)
    fn = value if callable(value) else (lambda: value)
    monkeypatch.setattr(github_api, "rate_limit_seen", fn)


def _bind_pr(monkeypatch, pr: int = 42) -> None:
    """Make every owned worktree resolve to open PR ``pr`` (BOU-2294).

    Only a BOUND waiter can reach a clean/idle verdict; one that watched nothing
    reports ``unbound`` instead.
    """
    monkeypatch.setattr(mc, "_owned_open_pr_pairs", lambda owned: [(w, pr) for w in owned])
    monkeypatch.setattr(mc, "_marker_pr_still_current", lambda wt, n: True)


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


def test_resolved_repo_survives_vanished_cwd(monkeypatch):
    """Config.resolved_repo(cwd=None) must not re-raise the deleted-cwd error
    after load() succeeds — the PR #62 review gap: ambient callers
    (coordinator._repo_slug_for_pr, maintenance.pr_url(cwd=None)) hit this."""
    import dataclasses

    config.load.cache_clear()
    # A real Config with repo unset -> resolved_repo() falls through to
    # _detect_repo(_safe_cwd()) instead of returning the pinned repo.
    cfg = dataclasses.replace(config.load(), repo="")

    def _boom(*_a, **_k):
        raise FileNotFoundError("[Errno 2] No such file or directory (cwd deleted)")

    monkeypatch.setattr(config.Path, "cwd", staticmethod(_boom))
    # The contract: must NOT raise on the vanished cwd (returns None or a str).
    result = cfg.resolved_repo()
    assert result is None or isinstance(result, str)


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


def test_await_emits_idle_outcome_on_exit_0(tmp_path, monkeypatch, capsys):
    """Exit 0 (no PRs / max-wait, no feedback) is a DISTINCT `idle` outcome — a
    supervisor must be able to tell it apart from `deferred_to_loop` (exit 3,
    another waiter actually covering the session) (#62)."""
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    _force_rate_limit_seen(monkeypatch, False)
    # A waiter that watched a real PR and found it clean — `idle` is the verdict
    # of a BOUND waiter; an unbound one reports `unbound` instead (BOU-2294).
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees",
                        lambda sid, cwd: [str(tmp_path)])
    monkeypatch.setattr(mc, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)
    _bind_pr(monkeypatch)
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
    assert outcomes[0]["outcome"] == "idle"


def test_await_emits_deferred_outcome_on_single_instance_exit_3(tmp_path, monkeypatch, capsys):
    import os
    live_pid = os.getpid()
    path = Path(mc._await_pidfile(str(tmp_path), SID))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": live_pid,
                "session_id": SID,
                "process_identity": "test-start",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: str(pid) == str(live_pid))
    monkeypatch.setattr(
        _waiter_mod, "_process_identity", lambda pid: "test-start"
    )

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


# --------------------------------------------------------------------------
# BOU-1921 — a gh-unavailable / rate-limited tick must not be misread as
# "no feedback -> exit 0"; the waiter stays alive and re-polls.
# --------------------------------------------------------------------------


def test_await_stays_alive_when_rate_limited_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(mc.time, "sleep", lambda *_: None)  # don't actually wait between ticks
    _force_rate_limit_seen(monkeypatch, True)  # a gh call this tick hit the quota wall
    wt = tmp_path / "worktree"
    wt.mkdir()
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)

    # First tick: gh rate-limited -> _check_worktree returns code 2.
    # Second tick: real feedback -> code 10.
    seq = iter([(2, "gh unavailable (rate limit)"), (10, "pending\nPR_NUMBER=9")])
    monkeypatch.setattr(mc, "_check_worktree", lambda p, sid, *, claim=True: next(seq))

    rc = mc.main(["await", "--cwd", str(tmp_path), "--session-id", SID,
                  "--owner-pid", "12345", "--max-wait", "0", "--interval", "1"])
    # Without the fix: first (code-2) tick + max-wait 0 -> "no feedback" -> return 0.
    # With the fix: the rate-limited tick keeps the waiter alive; it re-polls
    # and the second tick's feedback -> exit 10.
    assert rc == 10


def test_await_honors_max_wait_on_hard_gh_failure(tmp_path, monkeypatch):
    """A persistent NON-rate-limit gh failure (missing gh / auth / bad JSON) also
    returns code 2, but must still honor --max-wait rather than keeping the
    waiter alive forever (#62 review)."""
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(mc.time, "sleep", lambda *_: None)
    _force_rate_limit_seen(monkeypatch, False)  # HARD failure did NOT set the rate-limit flag
    wt = tmp_path / "worktree"
    wt.mkdir()
    # Keep this max-wait assertion focused on waiter control flow.  The real
    # maintenance-root discovery walks every configured repository and can
    # spend up to its per-root git timeout when the temporary test cwd is not a
    # repository; that is unrelated to the hard-failure policy under test.
    monkeypatch.setattr(_worktrees_mod, "_maint_roots_for", lambda cwd: [str(tmp_path)])
    monkeypatch.setattr(mc, "_publishable_anchors", lambda anchors, cwd, snap=None: anchors)
    monkeypatch.setattr(mc, "_update_await_coverage", lambda *args, **kwargs: None)
    empty_snapshot = ownership.OwnershipSnapshot(
        {}, now=datetime.now(timezone.utc)
    )
    monkeypatch.setattr(ownership, "snapshot", lambda **kwargs: empty_snapshot)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)
    monkeypatch.setattr(mc, "_check_worktree", lambda p, sid, *, claim=True: (2, "gh unavailable"))

    rc = mc.main(["await", "--cwd", str(tmp_path), "--session-id", SID,
                  "--owner-pid", "12345", "--max-wait", "0", "--interval", "1"])
    assert rc == 0  # hard failure honors --max-wait; does not stay alive forever


def test_await_detached_only_rate_limited_tick_stays_alive(tmp_path, monkeypatch):
    """A detached/ledger-only session (no owned worktrees) whose detached-path gh
    calls hit the quota wall must NOT exit 0 — the quota-outage case #62 flagged.
    The signal is the per-tick rate_limit_seen() flag set by github_api._run for
    ANY gh call, not the (never-set-for-detached) list-failure global."""
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(mc.time, "sleep", lambda *_: None)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [])  # no owned
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)

    calls = {"n": 0}

    def _detached(sid, cwd, include_legacy=True, prune_legacy=True):
        calls["n"] += 1
        base = {"pr": 7, "url": "http://x/7", "branch": "b", "ci_failing": False, "p1": False}
        if calls["n"] == 1:
            # rate-limited detached tick: an open PR, no actionable blocker yet
            return [{**base, "state": "open", "unresolved_threads": 0}]
        # recovered: a real blocker -> pending -> exit 10
        return [{**base, "state": "open", "unresolved_threads": 1}]

    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", _detached)
    # rate-limited on the first tick, observable on the second.
    _force_rate_limit_seen(monkeypatch, lambda: calls["n"] == 1)

    rc = mc.main(["await", "--cwd", str(tmp_path), "--session-id", SID,
                  "--owner-pid", "12345", "--max-wait", "0", "--interval", "1"])
    assert rc == 10  # stayed alive past the rate-limited detached tick


def test_await_observable_tick_exits_idle_no_stale_ratelimit(tmp_path, monkeypatch):
    """Staleness regression (#62): the rate-limit flag is reset per tick, so an
    observable tick (rate_limit_seen() False) exits idle and a rate-limit from an
    earlier tick cannot wedge the waiter alive."""
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(mc.time, "sleep", lambda *_: None)
    _force_rate_limit_seen(monkeypatch, False)  # this tick observed GitHub fine
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees",
                        lambda sid, cwd: [str(tmp_path)])
    monkeypatch.setattr(mc, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)
    _bind_pr(monkeypatch)
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )

    rc = mc.main(["await", "--cwd", str(tmp_path), "--session-id", SID,
                  "--owner-pid", "12345", "--max-wait", "0", "--interval", "1"])
    assert rc == 0  # observable tick exits idle
