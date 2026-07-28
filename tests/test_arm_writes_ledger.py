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


def test_cmd_arm_pr_on_matching_branch_arms(tmp_path, monkeypatch):
    import argparse

    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))

    # BOU-2406: _cmd_arm reads the *_detailed variants so an indeterminate gh
    # result carries its cause. Patch the seam the code actually uses -- patching
    # only the thin wrappers would let the real gh run and fail on auth.
    monkeypatch.setattr(mc, "_pr_draft_status_detailed", lambda cwd, pr, deadline=None: (False, ""))
    monkeypatch.setattr(mc, "_pr_head_branch_detailed", lambda cwd, pr, deadline=None: ("feature", ""))
    monkeypatch.setattr(mc, "_current_branch", lambda cwd: "feature")

    args = argparse.Namespace(
        cwd=str(wt), session_id="sess-Z", pid=99, pr=777, branch=None
    )
    assert mc._cmd_arm(args) == 0
    assert [e.pr for e in sl.read("sess-Z")] == [777]


def test_cmd_arm_pr_wrong_branch_skips(tmp_path, monkeypatch):
    import argparse

    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))

    monkeypatch.setattr(mc, "_pr_draft_status_detailed", lambda cwd, pr, deadline=None: (False, ""))
    monkeypatch.setattr(mc, "_pr_head_branch_detailed", lambda cwd, pr, deadline=None: ("feature", ""))
    monkeypatch.setattr(mc, "_current_branch", lambda cwd: "some-other-branch")

    args = argparse.Namespace(
        cwd=str(wt), session_id="sess-Z", pid=99, pr=777, branch=None
    )
    # PR 777's head branch isn't checked out here → skip, no ledger entry.
    # Still 0: "not applicable" is a genuine quiet path, unlike the
    # "could not determine" path BOU-2406 made non-zero.
    assert mc._cmd_arm(args) == 0
    assert sl.read("sess-Z") == []


def test_cmd_arm_indeterminate_gh_is_loud_and_nonzero(tmp_path, monkeypatch, capsys):
    """BOU-2406: 'we could not find out' must not read as a successful arm.

    Previously this printed "gh unavailable" and returned 0, so the caller
    believed the PR was armed while no ledger entry existed -- which then left
    the feedback waiter with no anchor to discover and no PR to watch.
    """
    import argparse

    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))

    monkeypatch.setattr(
        mc,
        "_pr_draft_status_detailed",
        lambda cwd, pr, deadline=None: (None, "attempt 3: gh exited 1: HTTP 502"),
    )
    monkeypatch.setattr(mc, "_current_branch", lambda cwd: "feature")

    args = argparse.Namespace(
        cwd=str(wt), session_id="sess-Z", pid=99, pr=777, branch=None
    )
    assert mc._cmd_arm(args) == 1, "an indeterminate result must be non-zero"
    assert sl.read("sess-Z") == [], "nothing may be recorded as armed"
    assert "HTTP 502" in capsys.readouterr().err, "the cause must reach the operator"


def test_ledger_append_failure_is_nonfatal(tmp_path, monkeypatch):
    # Point the ledger dir at a path that cannot be created (under a regular file).
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(blocker / "sub"))
    wt = tmp_path / "wt"
    wt.mkdir()
    ok = mc._write_arm_marker(str(wt), session_id="sess-Y", pid=1, pr_number=9)
    assert ok is True  # marker still written even though ledger append failed
