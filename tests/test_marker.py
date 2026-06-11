"""Ownership-marker round-trip + lease wiring (the one-agent-per-PR mechanism)."""

import os
import types

import pytest

from agentic_pr_dash import config
from agentic_pr_dash import maintenance as maint
from agentic_pr_dash import maintenance_check as mc


@pytest.fixture(autouse=True)
def _clear_cache():
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def test_arm_marker_roundtrip(tmp_path):
    assert mc._write_arm_marker(str(tmp_path), "sess-1", 4242, 7) is True

    marker = tmp_path / ".agentic-pr-dash" / "pr-watch.armed"
    assert marker.exists()

    fields = mc._read_marker(str(tmp_path))
    assert fields is not None
    assert fields["pr"] == "7"
    assert fields["session_id"] == "sess-1"
    assert fields["pid"] == "4242"

    session = tmp_path / ".agentic-pr-dash" / "pr-watch.session"
    assert session.read_text(encoding="utf-8").strip() == "sess-1"


def test_marker_honors_legacy_state_dir(tmp_path):
    # An existing .gaia dir is adopted, so an existing install's markers keep
    # landing in the same place after the rename.
    (tmp_path / ".gaia").mkdir()
    config.load.cache_clear()
    assert mc._write_arm_marker(str(tmp_path), "s", 1, 2) is True
    assert (tmp_path / ".gaia" / "pr-watch.armed").exists()
    assert not (tmp_path / ".agentic-pr-dash").exists()


def test_marker_path_uses_configured_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_PR_DASH_STATE_DIR", ".markers")
    config.load.cache_clear()
    assert mc._marker_path(str(tmp_path)).endswith(os.path.join(".markers", "pr-watch.armed"))


def test_marker_session_id_read(tmp_path):
    mc._write_arm_marker(str(tmp_path), "owner-xyz", 99, 3)
    assert mc._marker_session_id(str(tmp_path)) == "owner-xyz"


def test_marker_session_id_absent(tmp_path):
    assert mc._marker_session_id(str(tmp_path)) is None


def test_lease_seconds_from_config(tmp_path, monkeypatch):
    monkeypatch.delenv("GAIA_PR_WATCH_LEASE_SECONDS", raising=False)
    monkeypatch.setenv("AGENTIC_PR_DASH_LEASE_SECONDS", "123")
    config.load.cache_clear()
    assert mc._fix_lease_seconds() == 123


def test_lease_legacy_env_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_WATCH_LEASE_SECONDS", "777")
    config.load.cache_clear()
    assert mc._fix_lease_seconds() == 777


def test_collect_owned_skips_worktree_with_live_independent_owner(tmp_path, monkeypatch):
    """Reconciliation must NOT adopt a sibling worktree that an INDEPENDENT live
    session already owns, even when that worktree carries no pr-watch marker
    (arming is opt-in/off, and the marker session_id namespace is disjoint from
    the registry's). Adopting it makes one session service another ticket's PR
    (BOU-1540). Only the genuinely-orphaned worktree is adopted.
    """
    orphan = tmp_path / "wt-orphan"
    owned = tmp_path / "wt-owned"
    orphan.mkdir()
    owned.mkdir()

    self_pid = 555

    # Both branches have an open, non-draft @me PR; neither carries a marker.
    monkeypatch.setattr(
        mc,
        "_iter_worktrees_with_branch",
        lambda cwd: [(str(orphan), "br-orphan"), (str(owned), "br-owned")],
    )
    monkeypatch.setattr(
        mc,
        "_list_my_open_prs",
        lambda cwd: {"br-orphan": (101, False), "br-owned": (102, False)},
    )

    # The owned worktree has a LIVE independent session (different pid); the
    # orphan has none. discover_*_agents is the canonical "defer to live owner"
    # signal, keyed by worktree_path.
    def fake_agents(worktree_path):
        if worktree_path == str(owned):
            return [types.SimpleNamespace(pid=999)]  # a foreign live owner
        return []

    monkeypatch.setattr(maint, "discover_active_primary_feature_pipeline_agents", fake_agents)

    result = mc._collect_owned_worktrees("claude-uuid-X", str(tmp_path), self_pid)

    # Orphan is adopted (stamped with our id); the independently-owned sibling is not.
    assert str(orphan) in result
    assert str(owned) not in result
    assert mc._marker_session_id(str(orphan)) == "claude-uuid-X"
    assert mc._marker_session_id(str(owned)) is None


def test_collect_owned_still_adopts_when_only_self_is_live(tmp_path, monkeypatch):
    """A live session matching OUR own pid is not a foreign owner — adoption of a
    genuinely-orphaned worktree (no other live session) still proceeds, so
    BOU-1442 sub-agent pickup / crash recovery is preserved.
    """
    orphan = tmp_path / "wt-orphan"
    orphan.mkdir()
    self_pid = 555

    monkeypatch.setattr(
        mc, "_iter_worktrees_with_branch", lambda cwd: [(str(orphan), "br-orphan")]
    )
    monkeypatch.setattr(
        mc, "_list_my_open_prs", lambda cwd: {"br-orphan": (101, False)}
    )
    # Only our own pid shows as "live" for the worktree — not a foreign owner.
    monkeypatch.setattr(
        maint,
        "discover_active_primary_feature_pipeline_agents",
        lambda worktree_path: [types.SimpleNamespace(pid=self_pid)],
    )

    result = mc._collect_owned_worktrees("claude-uuid-X", str(tmp_path), self_pid)
    assert str(orphan) in result
