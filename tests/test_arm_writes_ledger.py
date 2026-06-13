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


def test_cmd_arm_branch_matching_current_resolves(tmp_path, monkeypatch):
    import argparse

    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))

    resolved_branches: list[str] = []

    def fake_resolve(cwd, branch):
        resolved_branches.append(branch)
        return (321, False)  # (pr_number, is_draft)

    # An explicit --branch is only armed when it is the cwd's checked-out branch
    # (the marker is written for cwd). Owner prefix is stripped before resolving.
    monkeypatch.setattr(mc, "_current_branch", lambda cwd: "feature-head")
    monkeypatch.setattr(mc, "_resolve_open_pr_for_branch", fake_resolve)

    args = argparse.Namespace(
        cwd=str(wt), session_id="sess-Z", pid=99, pr=None, branch="monalisa:feature-head"
    )
    assert mc._cmd_arm(args) == 0
    assert resolved_branches == ["feature-head"]  # owner prefix stripped


def test_cmd_arm_branch_not_checked_out_here_skips(tmp_path, monkeypatch):
    import argparse

    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))

    resolved: list[str] = []
    monkeypatch.setattr(mc, "_current_branch", lambda cwd: "current-branch")
    monkeypatch.setattr(
        mc, "_resolve_open_pr_for_branch", lambda cwd, branch: resolved.append(branch)
    )

    args = argparse.Namespace(
        cwd=str(wt), session_id="sess-Z", pid=99, pr=None, branch="other-branch"
    )
    # other-branch is not checked out in cwd → skip rather than mark the wrong
    # worktree; the PR is never resolved.
    assert mc._cmd_arm(args) == 0
    assert resolved == []


def test_ledger_append_failure_is_nonfatal(tmp_path, monkeypatch):
    # Point the ledger dir at a path that cannot be created (under a regular file).
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(blocker / "sub"))
    wt = tmp_path / "wt"
    wt.mkdir()
    ok = mc._write_arm_marker(str(wt), session_id="sess-Y", pid=1, pr_number=9)
    assert ok is True  # marker still written even though ledger append failed
