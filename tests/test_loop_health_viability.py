"""Loop health must reflect executor viability, not just a live PID (BOU-2086).

The machine-wide maintenance daemon could report healthy/running (wrapper pid
alive) and count as stop-gate coverage even when it could dispatch NEITHER
executor — e.g. the python loop exited 2 at startup validation because the
fallback (claude) was missing from PATH, while the wrapper pidfile stayed
"running". A red PR then idled with zero real coverage.

These tests pin the fail-closed contract:
  - the loop persists a heartbeat + executor-viability record every tick;
  - startup validation failure persists a DEGRADED record retaining BOTH
    primary and fallback error strings (and validates both before exiting);
  - ``_detached_loop_alive`` / ``_loop_covers_pr`` require a FRESH, viable
    record beyond pid-alive;
  - the stop-gate forces a per-session waiter when the loop is unhealthy;
  - dispatch failures retain the concrete per-executor error text;
  - a capability marker makes inert config keys on old snapshots detectable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from agentic_pr_dash import config, loop, maintenance_check as mc
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import stop_gate as _stop_gate_mod
from agentic_pr_dash._maintenance import waiter as _waiter_mod
from agentic_pr_dash._maintenance import worktree_check as _worktree_check_mod

SID = "sess-health-viability"


@pytest.fixture(autouse=True)
def _isolation(monkeypatch, tmp_path):
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(tmp_path / "daemons"))
    monkeypatch.setenv("GAIA_MAINTENANCE_LOOP_MACHINE_WIDE", "true")
    monkeypatch.setenv("AGENTIC_PR_DASH_ESCALATION_THRESHOLD", "3")
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _write_live_pidfile(tmp_path: Path) -> None:
    daemon_dir = tmp_path / "daemons"
    daemon_dir.mkdir(parents=True, exist_ok=True)
    (daemon_dir / "pr-maintenance-loop.pid").write_text(str(os.getpid()), encoding="utf-8")


# ---------------------------------------------------------------------------
# Coverage requires a fresh, executors-viable health record
# ---------------------------------------------------------------------------


def test_pid_alive_without_health_record_is_not_coverage(tmp_path):
    """A live loop pid with NO health record must not count as coverage —
    exactly the BOU-2086 incident shape (wrapper pid alive, python loop dead)."""
    cwd = str(tmp_path)
    _write_live_pidfile(tmp_path)
    assert mc._detached_loop_alive(cwd) is False
    assert loop._loop_covers_pr(cwd, 42) is False


def test_fresh_viable_health_record_grants_coverage(tmp_path):
    cwd = str(tmp_path)
    _write_live_pidfile(tmp_path)
    loop.record_loop_health(cwd, executors_viable=True, interval=600)
    assert mc._detached_loop_alive(cwd) is True
    assert loop._loop_covers_pr(cwd, 42) is True


def test_degraded_record_is_not_coverage(tmp_path):
    """executors_viable=False (both executors unreachable) → uncovered."""
    cwd = str(tmp_path)
    _write_live_pidfile(tmp_path)
    loop.record_loop_health(
        cwd, executors_viable=False,
        errors={"primary": "codex: not found", "fallback": "claude: not found"},
        interval=600,
    )
    assert mc._detached_loop_alive(cwd) is False
    assert loop._loop_covers_pr(cwd, 42) is False


def test_stale_heartbeat_is_not_coverage(tmp_path):
    """A viable record whose heartbeat is older than the freshness window
    (max(3×interval, floor)) means the loop stopped ticking → uncovered."""
    cwd = str(tmp_path)
    _write_live_pidfile(tmp_path)
    data = loop._load_health(cwd)
    data[loop.LOOP_HEALTH_KEY] = {
        "heartbeat": time.time() - 7200,  # 2h ago >> max(3*600, 900)
        "pid": os.getpid(),
        "executors_viable": True,
        "interval": 600,
    }
    loop._save_health(cwd, data)
    assert loop.loop_health_ok(cwd) is False
    assert mc._detached_loop_alive(cwd) is False


def test_corrupt_health_record_fails_closed(tmp_path):
    cwd = str(tmp_path)
    _write_live_pidfile(tmp_path)
    data = loop._load_health(cwd)
    data[loop.LOOP_HEALTH_KEY] = {"executors_viable": True, "heartbeat": "not-a-ts"}
    loop._save_health(cwd, data)
    assert loop.loop_health_ok(cwd) is False
    assert mc._detached_loop_alive(cwd) is False


@pytest.mark.parametrize("bad_viable", ["false", "true", 1, "yes", [True]])
def test_non_boolean_viability_value_fails_closed(tmp_path, bad_viable):
    """``executors_viable`` must be a REAL boolean True — a truthy string like
    "false" (shell/tooling writer, hand-edited or corrupt file) must read as
    unhealthy, because this record is the fail-closed coverage signal
    (PR #76 review)."""
    cwd = str(tmp_path)
    _write_live_pidfile(tmp_path)
    data = loop._load_health(cwd)
    data[loop.LOOP_HEALTH_KEY] = {
        "heartbeat": time.time(),
        "pid": os.getpid(),
        "executors_viable": bad_viable,
        "interval": 600,
    }
    loop._save_health(cwd, data)
    assert loop.loop_health_ok(cwd) is False
    assert mc._detached_loop_alive(cwd) is False


def test_dead_recorded_health_pid_is_not_coverage(tmp_path):
    """A fresh viable heartbeat whose RECORDING process has exited must not
    count: the daemon pidfile can hold a supervisor/wrapper pid that outlives
    the python loop, so the heartbeat must be tied to a live servicing process
    (PR #76 review)."""
    cwd = str(tmp_path)
    _write_live_pidfile(tmp_path)  # wrapper pid: alive (this test process)
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proc.wait()  # reaped → pid guaranteed dead
    data = loop._load_health(cwd)
    data[loop.LOOP_HEALTH_KEY] = {
        "heartbeat": time.time(),
        "pid": proc.pid,
        "executors_viable": True,
        "interval": 600,
    }
    loop._save_health(cwd, data)
    assert loop.loop_health_ok(cwd) is False
    assert mc._detached_loop_alive(cwd) is False


def _write_loop_record(cwd: str, **overrides) -> None:
    entry = {
        "heartbeat": time.time(),
        "pid": os.getpid(),
        "executors_viable": True,
        "interval": 600,
    }
    entry.update(overrides)
    data = loop._load_health(cwd)
    data[loop.LOOP_HEALTH_KEY] = entry
    loop._save_health(cwd, data)


@pytest.mark.parametrize(
    "bad_pid",
    [
        "²",  # str.isdigit() True, but int() raises ValueError
        "١٢٣",  # non-ASCII decimal digits: int() accepts, but not a recorded pid
        10**20,  # int() fine, os.kill overflows C pid_t → OverflowError
        "0",  # kill(0, 0) probes the process group, not a recorded process
        "-1",
        "12x",
        "",
    ],
)
def test_malformed_health_pid_fails_closed_without_raising(tmp_path, bad_pid):
    """A corrupt/hand-written pid in an otherwise viable health record must
    read as unhealthy, NOT raise: an escaping OverflowError/ValueError would be
    swallowed by the stop-gate's broad handler and release an uncovered PR
    (PR #76 review)."""
    cwd = str(tmp_path)
    _write_live_pidfile(tmp_path)
    _write_loop_record(cwd, pid=bad_pid)
    assert loop.loop_health_ok(cwd) is False
    assert mc._detached_loop_alive(cwd) is False


@pytest.mark.parametrize("bad_pid", ["²", "١٢٣", str(10**20), "0", "-1", "12x", ""])
def test_pid_alive_malformed_input_is_false_not_raise(bad_pid):
    """The shared ``_pid_alive`` helper (waiter/markers/ownership/stop-gate all
    use it) must fail closed on any unconvertible or unprobe-able pid string."""
    from agentic_pr_dash._maintenance._common import _pid_alive

    assert _pid_alive(bad_pid) is False


def test_zombie_recorded_health_pid_is_not_coverage(tmp_path, monkeypatch):
    """A zombie passes ``os.kill(pid, 0)``, so a crashed-but-unreaped loop
    child would still count as coverage. The health check must positively
    identify the zombie state and treat it as dead (PR #76 review)."""
    cwd = str(tmp_path)
    _write_live_pidfile(tmp_path)
    _write_loop_record(cwd)
    monkeypatch.setattr(loop, "_pid_state", lambda pid: "Z")
    assert loop.loop_health_ok(cwd) is False
    assert mc._detached_loop_alive(cwd) is False


def test_unreadable_pid_state_keeps_kill0_liveness(tmp_path, monkeypatch):
    """State probes can fail (no /proc, ps missing, permissions). Only a
    POSITIVE zombie identification may fail the record; an unknown state keeps
    the kill(0) verdict so permission quirks don't cause false-deads."""
    cwd = str(tmp_path)
    _write_live_pidfile(tmp_path)
    _write_loop_record(cwd)
    monkeypatch.setattr(loop, "_pid_state", lambda pid: None)
    assert loop.loop_health_ok(cwd) is True
    assert mc._detached_loop_alive(cwd) is True


def test_non_zombie_pid_state_keeps_coverage(tmp_path, monkeypatch):
    cwd = str(tmp_path)
    _write_live_pidfile(tmp_path)
    _write_loop_record(cwd)
    monkeypatch.setattr(loop, "_pid_state", lambda pid: "S+")
    assert loop.loop_health_ok(cwd) is True


def test_real_zombie_child_is_not_coverage(tmp_path):
    """End-to-end platform check (/proc on Linux, ps elsewhere): a real
    exited-but-unreaped child is a genuine zombie and must not grant
    coverage."""
    cwd = str(tmp_path)
    _write_live_pidfile(tmp_path)
    proc = subprocess.Popen(  # noqa: S603 — NOT waited: becomes a zombie
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 10.0
        while time.time() < deadline and loop._pid_state(proc.pid) != "Z":
            time.sleep(0.05)
        if loop._pid_state(proc.pid) != "Z":
            pytest.skip("child never observed as zombie on this platform")
        _write_loop_record(cwd, pid=proc.pid)
        assert loop.loop_health_ok(cwd) is False
        assert mc._detached_loop_alive(cwd) is False
    finally:
        proc.wait()


@pytest.mark.parametrize("bad_heartbeat", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_heartbeat_fails_closed(tmp_path, bad_heartbeat):
    """JSON like ``"heartbeat": 1e309`` parses to ``inf`` → ``now - inf`` is
    ``-inf``, which satisfies any max_age and grants coverage forever while the
    recorded pid happens to exist (PR #76 review)."""
    cwd = str(tmp_path)
    _write_live_pidfile(tmp_path)
    _write_loop_record(cwd, heartbeat=bad_heartbeat)
    assert loop.loop_health_ok(cwd) is False
    assert mc._detached_loop_alive(cwd) is False


@pytest.mark.parametrize("bad_interval", [float("inf"), float("nan")])
def test_non_finite_interval_fails_closed(tmp_path, bad_interval):
    """A non-finite ``interval`` makes the freshness window infinite, permitting
    arbitrarily stale records — the record is invalid, fail closed even with a
    fresh heartbeat (PR #76 review)."""
    cwd = str(tmp_path)
    _write_live_pidfile(tmp_path)
    _write_loop_record(cwd, interval=bad_interval)
    assert loop.loop_health_ok(cwd) is False
    assert mc._detached_loop_alive(cwd) is False


def test_missing_recorded_health_pid_fails_closed(tmp_path):
    """A viable record with no ``pid`` field cannot be tied to any live
    servicing process → not coverage."""
    cwd = str(tmp_path)
    _write_live_pidfile(tmp_path)
    data = loop._load_health(cwd)
    data[loop.LOOP_HEALTH_KEY] = {
        "heartbeat": time.time(),
        "executors_viable": True,
        "interval": 600,
    }
    loop._save_health(cwd, data)
    assert loop.loop_health_ok(cwd) is False
    assert mc._detached_loop_alive(cwd) is False


# ---------------------------------------------------------------------------
# Startup validation persists a degraded record with BOTH error strings
# ---------------------------------------------------------------------------


def test_startup_validation_failure_writes_degraded_record_with_both_errors(
    tmp_path, capsys,
):
    cwd = str(tmp_path)
    rc = loop.main([
        "--once", "--cwd", cwd,
        "--executor", "definitely-not-a-real-primary-xyz {prompt}",
        "--fallback-executor", "definitely-not-a-real-fallback-xyz {prompt}",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    # BOTH errors are printed, not just the first one hit.
    assert "definitely-not-a-real-primary-xyz" in err
    assert "definitely-not-a-real-fallback-xyz" in err

    entry = loop.loop_health_entry(cwd)
    assert entry.get("executors_viable") is False
    errors = entry.get("errors", {})
    assert "definitely-not-a-real-primary-xyz" in errors.get("primary", "")
    assert "definitely-not-a-real-fallback-xyz" in errors.get("fallback", "")
    # And the degraded record denies coverage even with a live wrapper pid.
    _write_live_pidfile(tmp_path)
    assert mc._detached_loop_alive(cwd) is False


def test_startup_validation_failure_fallback_only_still_records_both_shape(
    tmp_path, capsys,
):
    """Primary fine, fallback broken → still exit 2 (unchanged) and the record
    retains the concrete fallback error at the startup-validation path."""
    cwd = str(tmp_path)
    rc = loop.main([
        "--once", "--cwd", cwd,
        "--executor", "python3 -c {prompt}",
        "--fallback-executor", "definitely-not-a-real-fallback-xyz {prompt}",
    ])
    assert rc == 2
    assert "definitely-not-a-real-fallback-xyz" in capsys.readouterr().err
    entry = loop.loop_health_entry(cwd)
    assert entry.get("executors_viable") is False
    assert "definitely-not-a-real-fallback-xyz" in entry.get("errors", {}).get("fallback", "")
    assert "primary" not in entry.get("errors", {})


# ---------------------------------------------------------------------------
# Stop-gate fails closed (forces a waiter) when the loop is unhealthy
# ---------------------------------------------------------------------------


def _run_stop_gate(tmp_path, monkeypatch) -> int:
    wt = tmp_path / "worktree"
    wt.mkdir(exist_ok=True)
    mc._write_arm_marker(str(wt), SID, os.getpid(), 42)
    monkeypatch.setattr(_worktree_check_mod, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records",
                        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: {42})
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)
    monkeypatch.setattr(loop, "executor_failure_streak", lambda cwd, pr: 0)
    return mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])


def test_stop_gate_forces_waiter_when_loop_pid_alive_but_no_health(
    tmp_path, monkeypatch, capsys,
):
    """The incident shape: wrapper pid alive, streak 0, but no health record —
    the stop-gate must NOT idle on phantom coverage; it forces a waiter."""
    _write_live_pidfile(tmp_path)
    rc = _run_stop_gate(tmp_path, monkeypatch)
    err = capsys.readouterr().err
    assert rc == 2, f"Expected 2 (unhealthy loop is not coverage), got {rc}; stderr={err!r}"
    assert "waiter" in err.lower()


def test_stop_gate_forces_waiter_when_loop_degraded(tmp_path, monkeypatch, capsys):
    _write_live_pidfile(tmp_path)
    for cwd in (str(tmp_path), str(tmp_path / "worktree")):
        loop.record_loop_health(
            cwd, executors_viable=False,
            errors={"primary": "codex: spawn failed", "fallback": "claude: spawn failed"},
            interval=600,
        )
    rc = _run_stop_gate(tmp_path, monkeypatch)
    err = capsys.readouterr().err
    assert rc == 2, f"Expected 2 (degraded loop is not coverage), got {rc}; stderr={err!r}"


# ---------------------------------------------------------------------------
# Tick heartbeat + concrete dispatch-error retention
# ---------------------------------------------------------------------------


def _tick_args(**kw):
    base = dict(no_discover_worktrees=False, session_id="sess", cwd=["/repo"],
                fallback_executor="claude --dangerously-skip-permissions -p {prompt}",
                interval=600)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _wire_tick(monkeypatch, worktree, *, run_executor, check_rc=loop.CHECK_WORK_FOUND):
    calls: list = []

    def fake_run(cmd, *a, **k):
        if cmd[:3] == [loop.sys.executable, "-m", "agentic_pr_dash"] and "check" in cmd:
            return types.SimpleNamespace(
                returncode=check_rc,
                stdout="fix prompt\nPR_NUMBER=7\nCOORDINATOR_CLAIM_ID=claim-1\n",
                stderr="",
            )
        if cmd[:3] == [loop.sys.executable, "-m", "agentic_pr_dash"] and "complete" in cmd:
            calls.append(("complete", cmd))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[0] == "gh":
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    monkeypatch.setattr(loop, "_discover_cwds", lambda args: [str(worktree)])
    monkeypatch.setattr(loop, "_cleanup_stale_no_pr_worktree", lambda cwd, session_id="": False)
    monkeypatch.setattr(loop, "_baseline_sha", lambda cwd, pr: "base-sha")
    monkeypatch.setattr(loop, "_repo_slug", lambda cwd: "testrepo")
    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    monkeypatch.setattr(loop, "_run_executor",
                        lambda executor, prompt, cwd: run_executor(executor, calls))
    monkeypatch.setattr(loop.coordinator, "heartbeat_claim_id", lambda claim_id, session_id: None)
    monkeypatch.setattr(loop.coordinator, "release_claim_id",
                        lambda claim_id, session_id, reason: calls.append(("release", reason)))
    return calls


def test_tick_writes_heartbeat_record(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"; worktree.mkdir()

    def run_executor(executor, calls):
        calls.append(("executor", executor))
        return 0

    _wire_tick(monkeypatch, worktree, run_executor=run_executor)
    # Deterministic viability regardless of the host PATH.
    monkeypatch.setattr(loop, "_validate_executor", lambda executor: None)
    loop._tick(_tick_args(), "codex exec --full-auto {prompt}")

    entry = loop.loop_health_entry(str(worktree))
    assert entry.get("executors_viable") is True
    assert float(entry.get("heartbeat", 0)) > 0
    assert float(entry.get("interval", 0)) == 600.0


def test_tick_records_nonviable_when_no_executor_resolves(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"; worktree.mkdir()

    def run_executor(executor, calls):
        calls.append(("executor", executor))
        return 1

    _wire_tick(monkeypatch, worktree, run_executor=run_executor)
    monkeypatch.setattr(loop, "_validate_executor", lambda executor: f"{executor.split()[0]!r} was not found on PATH")
    loop._tick(_tick_args(), "codex exec --full-auto {prompt}")

    entry = loop.loop_health_entry(str(worktree))
    assert entry.get("executors_viable") is False
    assert "primary" in entry.get("errors", {})
    assert "fallback" in entry.get("errors", {})


def test_dispatch_spawn_failures_retain_concrete_errors_and_degrade(
    monkeypatch, tmp_path,
):
    """Both executors fail to SPAWN mid-run → the streak record retains the
    concrete per-executor exception text and viability is downgraded."""
    worktree = tmp_path / "wt"; worktree.mkdir()

    def run_executor(executor, calls):
        calls.append(("executor", executor))
        raise FileNotFoundError(f"No such file or directory: {executor.split()[0]!r}")

    _wire_tick(monkeypatch, worktree, run_executor=run_executor)
    monkeypatch.setattr(loop, "_validate_executor", lambda executor: None)
    loop._tick(_tick_args(), "codex exec --full-auto {prompt}")

    cwd = str(worktree)
    assert loop.executor_failure_streak(cwd, 7) == 1
    last_error = loop._load_health(cwd)["7"]["last_error"]
    assert "spawn failed" in last_error
    assert "'codex'" in last_error and "'claude'" in last_error
    # Both-spawn-failure downgrades the viability record immediately.
    entry = loop.loop_health_entry(cwd)
    assert entry.get("executors_viable") is False
    assert "spawn failed" in entry.get("errors", {}).get("primary", "")
    assert "spawn failed" in entry.get("errors", {}).get("fallback", "")


def test_nonzero_exit_keeps_viability_but_retains_exit_codes(monkeypatch, tmp_path):
    """Executors that RUN but fail (rate-limit etc.) are still viable — only the
    streak grows, with concrete exit codes retained."""
    worktree = tmp_path / "wt"; worktree.mkdir()

    def run_executor(executor, calls):
        calls.append(("executor", executor))
        return 3

    _wire_tick(monkeypatch, worktree, run_executor=run_executor)
    monkeypatch.setattr(loop, "_validate_executor", lambda executor: None)
    loop._tick(_tick_args(), "codex exec --full-auto {prompt}")

    cwd = str(worktree)
    assert loop.loop_health_entry(cwd).get("executors_viable") is True
    last_error = loop._load_health(cwd)["7"]["last_error"]
    assert "exit 3" in last_error and "codex" in last_error and "claude" in last_error


# ---------------------------------------------------------------------------
# Health-file hygiene with the reserved loop key
# ---------------------------------------------------------------------------


def test_loop_key_does_not_trip_recovered_streak_guard(monkeypatch, tmp_path):
    """The persistent loop record must not defeat _clear_recovered_streak's
    cheap-guard (which would add a `gh pr view` per worktree per tick)."""
    cwd = str(tmp_path)
    loop.record_loop_health(cwd, executors_viable=True, interval=600)

    def _boom(*a, **k):
        raise AssertionError("gh must not be invoked when only the loop record exists")

    monkeypatch.setattr(loop.subprocess, "run", _boom)
    loop._clear_recovered_streak(cwd)  # no per-PR entries → returns before gh


def test_streak_reset_preserves_loop_record(tmp_path):
    cwd = str(tmp_path)
    loop.record_loop_health(cwd, executors_viable=True, interval=600)
    loop.record_executor_failure(cwd, 42, "boom")
    assert loop.executor_failure_streak(cwd, 42) == 1
    loop.reset_executor_failure(cwd, 42)
    assert loop.executor_failure_streak(cwd, 42) == 0
    assert loop.loop_health_entry(cwd).get("executors_viable") is True


def test_health_file_round_trip_shape(tmp_path):
    cwd = str(tmp_path)
    loop.record_loop_health(
        cwd, executors_viable=False, errors={"primary": "p-err", "fallback": "f-err"},
        interval=60,
    )
    raw = json.loads(loop._health_file(cwd).read_text(encoding="utf-8"))
    entry = raw[loop.LOOP_HEALTH_KEY]
    assert entry["executors_viable"] is False
    assert entry["errors"] == {"primary": "p-err", "fallback": "f-err"}
    assert entry["interval"] == 60.0
    assert isinstance(entry["pid"], int)


# ---------------------------------------------------------------------------
# Capability marker (inert-config detectability)
# ---------------------------------------------------------------------------


def test_capability_marker_exposes_shipped_features():
    assert config.has_capability("escalation_failure_threshold") is True
    assert config.has_capability("loop_health_executor_viability") is True
    assert config.has_capability("definitely-not-a-capability") is False
    # The set itself is importable for external (wrapper-side) probes.
    assert "loop_health_executor_viability" in config.CAPABILITIES
