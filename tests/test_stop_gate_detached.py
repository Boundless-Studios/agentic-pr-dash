"""BOU-1587 AC #5 regression: a PR whose worktree was torn down, then receives an
unresolved review comment, must STILL block the stop gate (exit 2, names the PR).

The test does NOT stub the stop-gate decision path (the layer the bug lives in).
It drives the real mc.main(['stop-gate', ...]) and asserts the real exit code +
stderr. Only the GitHub boundary (review threads / PR state) and the worktree-
enumeration boundary (worktree gone) are faked.
"""
from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash import session_ledger as sl
from agentic_pr_dash import github_api
from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment


def _thread():
    c = ReviewThreadComment(database_id=1, path="f.py", line=1, body="please fix",
                            author="rev", created_at="2026-01-01T00:00:00Z")
    return ReviewThread(node_id="t1", is_resolved=False, is_outdated=False, top=c)


def test_detached_pr_with_review_comment_blocks_stop(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    # Session armed PR 777; its worktree was later torn down (gone).
    sl.append("sess-X", pr=777, branch="bou-1-x", worktree=str(tmp_path / "gone"))
    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [])
    monkeypatch.setattr(mc, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(mc, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(mc, "_pr_open_state", lambda pr, cwd: (
        "open", "https://github.com/o/r/pull/777", False, []))
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [_thread()])

    rc = mc.main(["stop-gate", "--session-id", "sess-X", "--cwd", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 2                      # blocks the stop
    assert "777" in err and "pull/777" in err


def test_detached_pr_with_review_level_changes_requested_blocks_stop(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    sl.append("sess-X", pr=779, branch="bou-review", worktree=str(tmp_path / "gone"))
    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [])
    monkeypatch.setattr(mc, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(mc, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(mc, "_pr_open_state", lambda pr, cwd: (
        "open", "https://github.com/o/r/pull/779", False, [],
        "CHANGES_REQUESTED", "CLEAN",
    ))
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [])

    rc = mc.main(["stop-gate", "--session-id", "sess-X", "--cwd", str(tmp_path)])

    err = capsys.readouterr().err
    assert rc == 2
    assert "779" in err and "review-level CHANGES_REQUESTED" in err


def test_detached_pr_with_dirty_merge_state_blocks_stop(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    sl.append("sess-X", pr=780, branch="bou-conflict", worktree=str(tmp_path / "gone"))
    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [])
    monkeypatch.setattr(mc, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(mc, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(mc, "_pr_open_state", lambda pr, cwd: (
        "open", "https://github.com/o/r/pull/780", False, [],
        "", "DIRTY",
    ))
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [])

    rc = mc.main(["stop-gate", "--session-id", "sess-X", "--cwd", str(tmp_path)])

    err = capsys.readouterr().err
    assert rc == 2
    assert "780" in err and "merge conflict" in err


def test_detached_pr_with_conflicting_mergeable_blocks_stop(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    sl.append("sess-X", pr=782, branch="bou-conflicting", worktree=str(tmp_path / "gone"))
    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [])
    monkeypatch.setattr(mc, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(mc, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(mc, "_pr_open_state", lambda pr, cwd: (
        "open", "https://github.com/o/r/pull/782", False, [],
        "", "UNKNOWN", "CONFLICTING",
    ))
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [])

    rc = mc.main(["stop-gate", "--session-id", "sess-X", "--cwd", str(tmp_path)])

    err = capsys.readouterr().err
    assert rc == 2
    assert "782" in err and "merge conflict" in err


def test_detached_pr_clean_does_not_block(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    sl.append("sess-X", pr=778, branch="bou-clean", worktree=str(tmp_path / "gone"))
    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [])
    monkeypatch.setattr(mc, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(mc, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(mc, "_pr_open_state", lambda pr, cwd: (
        "open", "https://x/pull/778", False, []))
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [])

    rc = mc.main(["stop-gate", "--session-id", "sess-X", "--cwd", str(tmp_path)])
    assert rc == 0
