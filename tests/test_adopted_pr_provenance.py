"""An auto-adopted PR must not block the adopting session's stop gate (BOU-2221).

``_collect_owned_worktrees`` adopts an unmarkered sibling worktree whose branch has
an open non-draft ``@me`` PR — the "missed arm" recovery path. That is useful, but
the resulting marker is indistinguishable from one the session wrote when it
actually opened the PR, so the stop gate then treats the adopted PR's red CI as a
hard blocker on a session that has no context on it.

Observed (BOU-2193 session): a session working `bou-2193-*` was handed PR #2650
(`bou-2195-g5-supervise-entry-install`) in a different worktree, and could not stop
until it fixed 6 failing agent-coordinator tests belonging to another epic. That
worktree had *uncommitted* edits to the exact failing test file — a live sibling
session was mid-fix — so complying would have clobbered their work. It happened
again minutes later with #2653.

Fix: record provenance on the marker (``armed`` vs ``adopted``) and let the stop
gate treat adopted PRs as informational. The maintenance loop still services them —
they are covered, just not a reason to hold a session hostage.
"""

import os

import pytest

from agentic_pr_dash._maintenance.markers import _read_marker, _write_arm_marker


@pytest.fixture
def worktree(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_PR_DASH_STATE_DIR", str(tmp_path / ".gaia"))
    wt = tmp_path / "wt"
    wt.mkdir()
    return str(wt)


def test_armed_marker_records_armed_provenance(worktree):
    """A marker written by an explicit arm is 'armed' — the session owns this PR."""
    assert _write_arm_marker(worktree, "sess-1", 4242, 111)

    marker = _read_marker(worktree)
    assert marker is not None
    assert marker.get("provenance") == "armed"


def test_adopted_marker_records_adopted_provenance(worktree):
    """A marker written by auto-adoption must be distinguishable from an arm."""
    assert _write_arm_marker(worktree, "sess-1", 4242, 222, provenance="adopted")

    marker = _read_marker(worktree)
    assert marker is not None
    assert marker.get("provenance") == "adopted", (
        "auto-adoption must stamp provenance so the stop gate can tell a PR this "
        "session opened from one it inherited"
    )


def test_marker_without_provenance_reads_as_armed(worktree):
    """Back-compat: markers written before this change have no provenance field.

    Defaulting them to 'armed' keeps existing ownership blocking exactly as-is —
    the new leniency applies only to markers explicitly stamped 'adopted'.
    """
    assert _write_arm_marker(worktree, "sess-1", 4242, 333)

    from agentic_pr_dash._maintenance.markers import _marker_path

    path = _marker_path(worktree)
    kept = [
        line
        for line in open(path, encoding="utf-8").read().splitlines()
        if not line.startswith("provenance=")
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept) + "\n")

    from agentic_pr_dash._maintenance.markers import _marker_provenance

    assert _marker_provenance(worktree) == "armed"


def test_adopted_provenance_is_readable_via_helper(worktree):
    assert _write_arm_marker(worktree, "sess-1", 4242, 444, provenance="adopted")

    from agentic_pr_dash._maintenance.markers import _marker_provenance

    assert _marker_provenance(worktree) == "adopted"


# ── Stop-gate behaviour ──────────────────────────────────────────────────────
# The end that actually matters: a blocked ADOPTED worktree must not hold the
# session, while a blocked ARMED one still must.

SID = "sess-owned"


@pytest.fixture(autouse=True)
def _stop_gate_no_rate_limit(monkeypatch):
    from agentic_pr_dash import config

    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    config.load.cache_clear()


def _stub_gate(monkeypatch, tmp_path, provenance_by_path):
    """Wire a stop-gate run over two owned worktrees, both reporting blockers."""
    from agentic_pr_dash import maintenance_check
    from agentic_pr_dash._maintenance import markers as _markers_mod
    from agentic_pr_dash._maintenance import stop_gate as _stop_gate_mod
    from agentic_pr_dash._maintenance import worktree_check as _worktree_check_mod
    from agentic_pr_dash._maintenance import worktrees as _worktrees_mod

    mine = tmp_path / "mine"
    sibling = tmp_path / "sibling"
    mine.mkdir()
    sibling.mkdir()

    monkeypatch.setattr(
        _worktrees_mod,
        "_iter_worktrees_with_branch",
        lambda cwd: [(str(mine), "mine-branch"), (str(sibling), "sibling-branch")],
    )
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda path: SID)
    monkeypatch.setattr(_worktrees_mod, "_list_my_open_prs", lambda cwd, timeout=15: {})
    monkeypatch.setattr(
        _worktrees_mod, "_live_independent_owner_paths", lambda paths, session_id: set()
    )
    # Both worktrees report a blocker; only provenance differs.
    monkeypatch.setattr(
        _worktree_check_mod,
        "_check_worktree",
        lambda path, session_id, *, claim=True: (
            (10, f"blocker\nPR_NUMBER={'2650' if str(path) == str(sibling) else '111'}")
        ),
    )
    monkeypatch.setattr(
        _stop_gate_mod,
        "_marker_provenance",
        lambda path: provenance_by_path.get(str(path), "armed"),
    )
    return maintenance_check, mine, sibling


def test_blocked_adopted_sibling_does_not_block_the_stop_gate(monkeypatch, tmp_path, capsys):
    """The BOU-2221 regression: an inherited PR's red CI must not wedge the session."""
    maintenance_check, mine, sibling = _stub_gate(monkeypatch, tmp_path, {})

    # Only the sibling is adopted, and only the sibling reports a blocker — so the
    # ONLY thing that could hold this session is work it never armed.
    from agentic_pr_dash._maintenance import stop_gate as _stop_gate_mod
    from agentic_pr_dash._maintenance import worktree_check as _worktree_check_mod

    monkeypatch.setattr(
        _worktree_check_mod,
        "_check_worktree",
        lambda path, session_id, *, claim=True: (
            (10, "blocker\nPR_NUMBER=2650") if str(path) == str(sibling) else (0, "clean")
        ),
    )
    monkeypatch.setattr(
        _stop_gate_mod,
        "_marker_provenance",
        lambda path: "adopted" if str(path) == str(sibling) else "armed",
    )

    rc = maintenance_check.main(
        ["stop-gate", "--cwd", str(tmp_path), "--session-id", SID, "--no-waiter"]
    )

    assert rc == 0, "an auto-adopted sibling PR must not block the stop gate"
    err = capsys.readouterr().err
    assert "adopted PR(s) #2650" in err, "adopted work must still be surfaced, not hidden"
    assert "NOT blocking" in err


def test_blocked_armed_worktree_still_blocks(monkeypatch, tmp_path):
    """Guard the other direction: real ownership must keep failing closed."""
    maintenance_check, mine, sibling = _stub_gate(monkeypatch, tmp_path, {})

    rc = maintenance_check.main(
        ["stop-gate", "--cwd", str(tmp_path), "--session-id", SID, "--no-waiter"]
    )

    assert rc == 2, "a PR this session actually armed must still block the stop gate"
