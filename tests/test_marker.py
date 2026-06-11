"""Ownership-marker round-trip + lease wiring (the one-agent-per-PR mechanism)."""

import os
import types

import pytest

from agentic_pr_dash import agents
from agentic_pr_dash import config
from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash import session_registry


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


def _state(pid, worktree_path, *, terminal=False, fp=True, src="launch-worktree-cli"):
    return types.SimpleNamespace(
        pid=pid, worktree_path=worktree_path, is_terminal=terminal,
        is_feature_pipeline=fp, launch_source=src,
    )


def _summary(*states):
    return types.SimpleNamespace(sessions={i: s for i, s in enumerate(states)})


def _no_registry(monkeypatch):
    monkeypatch.setattr(session_registry, "summarize_sessions", lambda: _summary())
    monkeypatch.setattr(session_registry, "pid_is_live", lambda pid: True)


def _no_process(monkeypatch):
    monkeypatch.setattr(agents, "discover_primary_feature_pipeline_agents", lambda paths, **kw: {})


def test_live_independent_owner_paths_registry_idle_session(tmp_path, monkeypatch):
    """A registry session that is alive (even idle, no CPU gate) and not us marks
    its worktree as independently owned; a path with no signal does not."""
    owned = tmp_path / "owned"
    free = tmp_path / "free"
    owned.mkdir()
    free.mkdir()

    monkeypatch.setattr(mc, "_self_pid_chain", lambda: {555})
    _no_process(monkeypatch)
    monkeypatch.setattr(session_registry, "pid_is_live", lambda pid: True)
    monkeypatch.setattr(session_registry, "summarize_sessions", lambda: _summary(_state(999, str(owned))))

    result = mc._live_independent_owner_paths([str(owned), str(free)], "sess-self")
    assert str(owned) in result
    assert str(free) not in result


def test_live_independent_owner_paths_excludes_self(tmp_path, monkeypatch):
    """A live owner whose pid is in our ancestor chain is US, not a foreign owner."""
    owned = tmp_path / "owned"
    owned.mkdir()

    monkeypatch.setattr(mc, "_self_pid_chain", lambda: {555, 42})
    _no_process(monkeypatch)
    monkeypatch.setattr(session_registry, "pid_is_live", lambda pid: True)
    monkeypatch.setattr(session_registry, "summarize_sessions", lambda: _summary(_state(42, str(owned))))
    assert mc._live_independent_owner_paths([str(owned)], "sess-self") == set()


def test_live_independent_owner_paths_process_scan_idle(tmp_path, monkeypatch):
    """The process scan runs with min_cpu=0.0 so an idle-but-alive session counts,
    and a foreign pid marks the path owned."""
    owned = tmp_path / "owned"
    owned.mkdir()
    captured = {}

    def fake_discover(paths, *, min_cpu=1.0):
        captured["min_cpu"] = min_cpu
        return {str(owned): [types.SimpleNamespace(pid=999)]}

    monkeypatch.setattr(mc, "_self_pid_chain", lambda: {555})
    _no_registry(monkeypatch)
    monkeypatch.setattr(agents, "discover_primary_feature_pipeline_agents", fake_discover)

    result = mc._live_independent_owner_paths([str(owned)], "sess-self")
    assert str(owned) in result
    assert captured["min_cpu"] == 0.0  # liveness, not activity


def test_live_independent_owner_paths_marker_pid_gates_not_session_id(tmp_path, monkeypatch):
    """The marker gate keys on the marker PID, not session_id: our own worktree
    (marker stamped with the agent's id while --session-id is the gaia id, pid in
    our ancestor chain) is NOT flagged foreign; a foreign live pid IS (PR #7 P2)."""
    mine = tmp_path / "mine"
    theirs = tmp_path / "theirs"
    mine.mkdir()
    theirs.mkdir()
    # Both markers carry a session id different from the caller's --session-id.
    mc._write_arm_marker(str(mine), "agent-uuid", 4242, 1)     # pid 4242 ∈ self chain
    mc._write_arm_marker(str(theirs), "agent-uuid", 9999, 2)   # pid 9999 ∉ self chain

    monkeypatch.setattr(mc, "_self_pid_chain", lambda: {4242})
    monkeypatch.setattr(mc, "_pid_alive", lambda raw: True)
    _no_registry(monkeypatch)
    _no_process(monkeypatch)

    result = mc._live_independent_owner_paths([str(mine), str(theirs)], "gaia-self-id")
    assert str(mine) not in result      # our own worktree, namespace-mismatched id
    assert str(theirs) in result        # genuinely foreign live pid


def test_collect_owned_skips_and_heals_independent_owner(tmp_path, monkeypatch):
    """Reconciliation does not adopt an independently-owned sibling, AND an
    already-stolen worktree (our marker on it) is not re-emitted — it is healed
    because the live-independent-owner gate runs before the 'already ours' emit
    (PR #7 review, P1 #4)."""
    orphan = tmp_path / "orphan"
    stolen = tmp_path / "stolen"
    orphan.mkdir()
    stolen.mkdir()

    # 'stolen' already carries OUR id from a prior bad adoption.
    mc._write_arm_marker(str(stolen), "claude-uuid-X", 555, 102)
    assert mc._marker_session_id(str(stolen)) == "claude-uuid-X"

    monkeypatch.setattr(
        mc,
        "_iter_worktrees_with_branch",
        lambda cwd: [(str(orphan), "br-orphan"), (str(stolen), "br-stolen")],
    )
    monkeypatch.setattr(
        mc, "_list_my_open_prs", lambda cwd: {"br-orphan": (101, False), "br-stolen": (102, False)}
    )
    # The stolen worktree has a live INDEPENDENT owner now; the orphan has none.
    monkeypatch.setattr(
        mc,
        "_live_independent_owner_paths",
        lambda paths, sid: {os.path.abspath(str(stolen))},
    )

    result = mc._collect_owned_worktrees("claude-uuid-X", str(tmp_path), 555)
    assert str(orphan) in result          # genuinely orphaned → adopted (BOU-1442)
    assert str(stolen) not in result      # contested/stolen → not serviced (healed)


def test_collect_owned_adopts_orphan_when_no_independent_owner(tmp_path, monkeypatch):
    """No independent owner → a genuinely-orphaned PR worktree is still adopted,
    preserving BOU-1442 sub-agent pickup / crash recovery."""
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    monkeypatch.setattr(
        mc, "_iter_worktrees_with_branch", lambda cwd: [(str(orphan), "br-orphan")]
    )
    monkeypatch.setattr(mc, "_list_my_open_prs", lambda cwd: {"br-orphan": (101, False)})
    monkeypatch.setattr(mc, "_live_independent_owner_paths", lambda paths, sid: set())

    result = mc._collect_owned_worktrees("claude-uuid-X", str(tmp_path), 555)
    assert str(orphan) in result
    assert mc._marker_session_id(str(orphan)) == "claude-uuid-X"


def test_check_worktree_defers_to_live_independent_owner(tmp_path, monkeypatch):
    """When work exists but a live independent session owns the worktree, check
    DEFERS (exit 0) instead of declaring work (exit 10) — the heal/safety net for
    stolen markers and unsafe loop fallbacks (PR #7 review, P1 #4/#5)."""
    pr = types.SimpleNamespace(
        number=7, is_draft=False, failing_checks=[], latest_commit_sha="abc",
    )
    heartbeats = []
    monkeypatch.setattr(mc, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(mc, "_resolve_pr_for_branch", lambda cwd: pr)
    monkeypatch.setattr(
        mc, "_touch_owner_heartbeat", lambda cwd, sid, work: heartbeats.append(work)
    )
    from agentic_pr_dash import maintenance as _maint
    monkeypatch.setattr(_maint, "blockers_for_pr", lambda pr: ["review_comments"])

    # Foreign owner present → defer, and DO NOT refresh the heartbeat/lease (else a
    # stolen marker would pin the PR; PR #7 review, P2).
    monkeypatch.setattr(mc, "_live_independent_owner_paths", lambda paths, sid: {os.path.abspath(str(tmp_path))})
    code, text = mc._check_worktree(str(tmp_path), "sess-self")
    assert code == 0
    assert "independent owner" in text
    assert heartbeats == []  # no heartbeat/lease write while deferring

    # No foreign owner → work is surfaced AND the fix lease is stamped.
    monkeypatch.setattr(mc, "_live_independent_owner_paths", lambda paths, sid: set())
    monkeypatch.setattr(_maint, "build_maintenance_prompt", lambda pr, failed_logs=None: "PROMPT")
    code2, text2 = mc._check_worktree(str(tmp_path), "sess-self")
    assert code2 == 10
    assert "PR_NUMBER=7" in text2
    assert heartbeats == [True]  # heartbeat refreshed with work=True before servicing
