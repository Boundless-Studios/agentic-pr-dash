"""BOU-1924: ``list-owned --prs`` enumerates ALL PRs the session owns — including
detached (no-worktree) ones — so one ``/pr-maintenance-check`` surfaces every
owned PR's pending feedback, not just the current worktree's.

The default (path) mode is unchanged: the maintenance loop's ``_discover_cwds``
consumes owned worktree PATHS from ``list-owned`` and must keep getting paths.
"""
from __future__ import annotations

from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash._maintenance import reconcile as rec


def test_list_owned_prs_enumerates_records(monkeypatch, tmp_path, capsys):
    recs = [
        {"pr": 2400, "repo": "o/n", "worktree_present": True, "branch": "b1",
         "url": "https://x/2400", "p1": False, "unresolved_threads": 1},
        {"pr": 2401, "repo": "o/n", "worktree_present": False, "branch": "b2",
         "url": "https://x/2401", "p1": False, "unresolved_threads": 0},
    ]
    seen = {}

    def _fake(session_id, cwd, pid, adopt_orphans):
        seen["args"] = (session_id, adopt_orphans)
        return recs

    monkeypatch.setattr(rec, "_owned_pr_records_all_roots", _fake)

    rc = mc.main([
        "list-owned", "--prs",
        "--cwd", str(tmp_path), "--session-id", "me", "--pid", "123",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    # Both PRs surface — including the detached (no-worktree) one.
    assert "2400" in out
    assert "2401" in out
    # The detached PR is flagged as having no worktree.
    assert "none" in out.lower()
    # Read-only enumeration must NOT adopt orphaned (dead-session) PRs.
    assert seen["args"] == ("me", False)


def test_list_owned_default_mode_still_prints_paths(monkeypatch, tmp_path, capsys):
    """Without --prs, list-owned prints owned worktree PATHS (loop contract)."""
    monkeypatch.setattr(mc, "_resolve_maintenance_roots", lambda cwd: [str(tmp_path)])
    monkeypatch.setattr(
        mc, "_collect_owned_worktrees",
        lambda sid, root, pid, **kw: [str(tmp_path / "wtX")],
    )
    # Make the git worktree probe succeed.
    import subprocess

    class _OK:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _OK())

    rc = mc.main([
        "list-owned",
        "--cwd", str(tmp_path), "--session-id", "me", "--pid", "123",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert str(tmp_path / "wtX") in out
