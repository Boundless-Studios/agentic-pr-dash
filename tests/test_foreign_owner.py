"""BOU-1478: the detached loop must DEFER to a live in-session owner and TAKE
OVER only once the owner is truly gone.

Ownership is recognised by a fresh heartbeat, an active fix-lease, or — as the
robust fallback — a still-alive owner ``pid``. The pid fallback (session
precedence over the machine-wide loop) means a live session keeps ownership even
when its heartbeat has gone stale (e.g. no in-session waiter is running to
refresh it); the loop only takes over when the owner ``pid`` is dead."""

import os
from datetime import datetime, timedelta, timezone

from agentic_pr_dash import config
from agentic_pr_dash import maintenance_check as mc


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_marker(cwd, **fields):
    p = config.load(str(cwd)).watch_marker_for(str(cwd))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(f"{k}={v}\n" for k, v in fields.items()), encoding="utf-8")


def test_fresh_heartbeat_defers(tmp_path):
    _write_marker(tmp_path, session_id="owner-A", pid=str(os.getpid()),
                  heartbeat=_iso(datetime.now(timezone.utc)))
    assert mc._live_foreign_owner(str(tmp_path), "me") == "owner-A"


def test_stale_heartbeat_live_pid_still_defers(tmp_path):
    # Session precedence: a stale heartbeat with NO active lease used to hand the
    # PR to the loop, but a still-alive owner pid now keeps ownership — the loop
    # must not claim a live session's PR just because its heartbeat lapsed.
    _write_marker(tmp_path, session_id="owner-A", pid=str(os.getpid()),
                  heartbeat=_iso(datetime.now(timezone.utc) - timedelta(hours=1)))
    assert mc._live_foreign_owner(str(tmp_path), "me") == "owner-A"


def test_stale_heartbeat_dead_pid_takes_over(tmp_path):
    # Only when the owner is TRULY gone (dead pid, stale heartbeat, no lease) does
    # the loop take over — the anti-wedge backstop for an abandoned worktree.
    _write_marker(tmp_path, session_id="owner-A", pid="2147480000",
                  heartbeat=_iso(datetime.now(timezone.utc) - timedelta(hours=1)))
    assert mc._live_foreign_owner(str(tmp_path), "me") is None


def test_active_fix_lease_defers_despite_stale_heartbeat(tmp_path):
    _write_marker(tmp_path, session_id="owner-A", pid=str(os.getpid()),
                  heartbeat=_iso(datetime.now(timezone.utc) - timedelta(hours=1)),
                  fix_lease_until=_iso(datetime.now(timezone.utc) + timedelta(minutes=20)))
    assert mc._live_foreign_owner(str(tmp_path), "me") == "owner-A"


def test_self_session_is_never_foreign(tmp_path):
    _write_marker(tmp_path, session_id="me", pid=str(os.getpid()),
                  heartbeat=_iso(datetime.now(timezone.utc)))
    assert mc._live_foreign_owner(str(tmp_path), "me") is None


def test_dead_pid_with_fresh_heartbeat_still_defers(tmp_path):
    _write_marker(tmp_path, session_id="owner-A", pid="2147480000",
                  heartbeat=_iso(datetime.now(timezone.utc)))
    assert mc._live_foreign_owner(str(tmp_path), "me") == "owner-A"


def test_last_heartbeat_field_counts_as_fresh(tmp_path):
    _write_marker(tmp_path, session_id="owner-A", pid="2147480000",
                  last_heartbeat=_iso(datetime.now(timezone.utc)))
    assert mc._live_foreign_owner(str(tmp_path), "me") == "owner-A"


def test_fresh_legacy_heartbeat_wins_when_last_heartbeat_is_stale(tmp_path):
    _write_marker(
        tmp_path,
        session_id="owner-A",
        pid="2147480000",
        last_heartbeat=_iso(datetime.now(timezone.utc) - timedelta(hours=1)),
        heartbeat=_iso(datetime.now(timezone.utc)),
    )
    assert mc._live_foreign_owner(str(tmp_path), "me") == "owner-A"


def test_corrupt_fix_lease_without_fresh_heartbeat_does_not_defer_forever(tmp_path):
    _write_marker(
        tmp_path,
        session_id="owner-A",
        pid="2147480000",
        last_heartbeat=_iso(datetime.now(timezone.utc) - timedelta(hours=1)),
        fix_lease_until="not-a-timestamp",
    )
    assert mc._live_foreign_owner(str(tmp_path), "me") is None


def test_modern_ttl_env_beats_legacy(monkeypatch):
    # New AGENTIC_PR_DASH_* must win even when the legacy var is still exported.
    monkeypatch.setenv("GAIA_PR_WATCH_HEARTBEAT_TTL", "60")
    monkeypatch.setenv("AGENTIC_PR_DASH_HEARTBEAT_TTL_SECONDS", "3600")
    config.load.cache_clear()
    assert mc._heartbeat_ttl_seconds() == 3600
    # Legacy still applies as a fallback when no modern value is set.
    monkeypatch.delenv("AGENTIC_PR_DASH_HEARTBEAT_TTL_SECONDS")
    config.load.cache_clear()
    assert mc._heartbeat_ttl_seconds() == 60


def test_ttl_honors_per_worktree_config(tmp_path):
    # heartbeat_ttl_seconds in the CHECKED worktree's toml is honored when cwd is
    # threaded through (the loop runs `check --cwd <worktree>`).
    (tmp_path / "agentic-pr-dash.toml").write_text(
        "[project]\nheartbeat_ttl_seconds = 42\n", encoding="utf-8")
    config.load.cache_clear()
    assert mc._heartbeat_ttl_seconds(str(tmp_path)) == 42
    assert mc._heartbeat_ttl_seconds() == 600  # process cwd -> default


def test_worktree_toml_ttl_beats_legacy_env(tmp_path, monkeypatch):
    # A per-repo toml setting must win over stale legacy shell state.
    monkeypatch.setenv("GAIA_PR_WATCH_HEARTBEAT_TTL", "60")
    (tmp_path / "agentic-pr-dash.toml").write_text(
        "[project]\nheartbeat_ttl_seconds = 42\n", encoding="utf-8")
    config.load.cache_clear()
    assert mc._heartbeat_ttl_seconds(str(tmp_path)) == 42


def test_heartbeat_ttl_is_configurable(tmp_path, monkeypatch):
    # Dead pid so heartbeat freshness (not the pid fallback) is the deciding
    # factor — this test isolates the configurable TTL behavior.
    # A heartbeat 5 min old is stale at the 60s TTL...
    monkeypatch.setenv("AGENTIC_PR_DASH_HEARTBEAT_TTL_SECONDS", "60")
    config.load.cache_clear()
    _write_marker(tmp_path, session_id="owner-A", pid="2147480000",
                  heartbeat=_iso(datetime.now(timezone.utc) - timedelta(minutes=5)))
    assert mc._live_foreign_owner(str(tmp_path), "me") is None
    # ...but fresh at a 3600s TTL.
    monkeypatch.setenv("AGENTIC_PR_DASH_HEARTBEAT_TTL_SECONDS", "3600")
    config.load.cache_clear()
    assert mc._live_foreign_owner(str(tmp_path), "me") == "owner-A"
