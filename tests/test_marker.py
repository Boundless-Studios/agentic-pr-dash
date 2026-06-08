"""Ownership-marker round-trip + lease wiring (the one-agent-per-PR mechanism)."""

import os

import pytest

from pr_agent_ops import config
from pr_agent_ops import maintenance_check as mc


@pytest.fixture(autouse=True)
def _clear_cache():
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def test_arm_marker_roundtrip(tmp_path):
    assert mc._write_arm_marker(str(tmp_path), "sess-1", 4242, 7) is True

    marker = tmp_path / ".pr-agent-ops" / "pr-watch.armed"
    assert marker.exists()

    fields = mc._read_marker(str(tmp_path))
    assert fields is not None
    assert fields["pr"] == "7"
    assert fields["session_id"] == "sess-1"
    assert fields["pid"] == "4242"

    session = tmp_path / ".pr-agent-ops" / "pr-watch.session"
    assert session.read_text(encoding="utf-8").strip() == "sess-1"


def test_marker_honors_legacy_state_dir(tmp_path):
    # An existing .gaia dir is adopted, so an existing install's markers keep
    # landing in the same place after the rename.
    (tmp_path / ".gaia").mkdir()
    config.load.cache_clear()
    assert mc._write_arm_marker(str(tmp_path), "s", 1, 2) is True
    assert (tmp_path / ".gaia" / "pr-watch.armed").exists()
    assert not (tmp_path / ".pr-agent-ops").exists()


def test_marker_path_uses_configured_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_AGENT_OPS_STATE_DIR", ".markers")
    config.load.cache_clear()
    assert mc._marker_path(str(tmp_path)).endswith(os.path.join(".markers", "pr-watch.armed"))


def test_marker_session_id_read(tmp_path):
    mc._write_arm_marker(str(tmp_path), "owner-xyz", 99, 3)
    assert mc._marker_session_id(str(tmp_path)) == "owner-xyz"


def test_marker_session_id_absent(tmp_path):
    assert mc._marker_session_id(str(tmp_path)) is None


def test_lease_seconds_from_config(tmp_path, monkeypatch):
    monkeypatch.delenv("GAIA_PR_WATCH_LEASE_SECONDS", raising=False)
    monkeypatch.setenv("PR_AGENT_OPS_LEASE_SECONDS", "123")
    config.load.cache_clear()
    assert mc._fix_lease_seconds() == 123


def test_lease_legacy_env_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_WATCH_LEASE_SECONDS", "777")
    config.load.cache_clear()
    assert mc._fix_lease_seconds() == 777
