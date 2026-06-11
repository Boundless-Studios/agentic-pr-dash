from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash import session_ledger as sl


def test_write_arm_marker_appends_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    wt = tmp_path / "wt"
    wt.mkdir()
    # _write_arm_marker writes the marker into state_dir_for(cwd); a bare dir is fine.
    ok = mc._write_arm_marker(str(wt), session_id="sess-X", pid=4242, pr_number=777)
    assert ok is True
    entries = sl.read("sess-X")
    assert [e.pr for e in entries] == [777]
    assert entries[0].worktree == str(wt)


def test_ledger_append_failure_is_nonfatal(tmp_path, monkeypatch):
    # Point the ledger dir at a path that cannot be created (under a regular file).
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(blocker / "sub"))
    wt = tmp_path / "wt"
    wt.mkdir()
    ok = mc._write_arm_marker(str(wt), session_id="sess-Y", pid=1, pr_number=9)
    assert ok is True  # marker still written even though ledger append failed
