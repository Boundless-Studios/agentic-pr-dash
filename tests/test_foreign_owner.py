"""BOU-1478: the detached loop must DEFER to a live, actively-looping in-session
owner (fresh heartbeat OR active fix-lease) and TAKE OVER once it goes stale."""

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


def test_stale_heartbeat_no_lease_takes_over(tmp_path):
    _write_marker(tmp_path, session_id="owner-A", pid=str(os.getpid()),
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


def test_dead_pid_takes_over(tmp_path):
    _write_marker(tmp_path, session_id="owner-A", pid="2147480000",
                  heartbeat=_iso(datetime.now(timezone.utc)))
    assert mc._live_foreign_owner(str(tmp_path), "me") is None


def test_heartbeat_ttl_is_configurable(tmp_path, monkeypatch):
    # A heartbeat 5 min old is stale at the 60s TTL...
    monkeypatch.setenv("AGENTIC_PR_DASH_HEARTBEAT_TTL_SECONDS", "60")
    config.load.cache_clear()
    _write_marker(tmp_path, session_id="owner-A", pid=str(os.getpid()),
                  heartbeat=_iso(datetime.now(timezone.utc) - timedelta(minutes=5)))
    assert mc._live_foreign_owner(str(tmp_path), "me") is None
    # ...but fresh at a 3600s TTL.
    monkeypatch.setenv("AGENTIC_PR_DASH_HEARTBEAT_TTL_SECONDS", "3600")
    config.load.cache_clear()
    assert mc._live_foreign_owner(str(tmp_path), "me") == "owner-A"
