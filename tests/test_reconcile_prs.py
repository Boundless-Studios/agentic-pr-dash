import json

from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash import session_ledger as sl
from agentic_pr_dash import github_api
from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod


def _thread(resolved=False, outdated=False, body="please fix"):
    c = ReviewThreadComment(database_id=1, path="f.py", line=1, body=body,
                            author="rev", created_at="2026-01-01T00:00:00Z")
    return ReviewThread(node_id="t1", is_resolved=resolved, is_outdated=outdated, top=c)


def test_detached_pr_with_unresolved_thread_is_surfaced(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    # Ledger has PR 777 on a worktree that no longer exists.
    sl.append("sess-X", pr=777, branch="bou-1-x", worktree=str(tmp_path / "gone"))
    monkeypatch.setattr(_reconcile_mod, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(_reconcile_mod, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [_thread()])
    monkeypatch.setattr(_reconcile_mod, "_pr_open_state", lambda pr, cwd: (
        "open", "https://github.com/o/r/pull/777", False, []))
    rc = mc.main(["reconcile-prs", "--session-id", "sess-X", "--cwd", str(tmp_path)])
    assert rc == 0
    records = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(records) == 1
    r = records[0]
    assert r["pr"] == 777 and r["worktree_present"] is False
    assert r["unresolved_threads"] >= 1 and r["url"].endswith("/pull/777")


def test_detached_pr_with_review_level_changes_requested_is_surfaced(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    sl.append("sess-X", pr=779, branch="bou-review", worktree=str(tmp_path / "gone"))
    monkeypatch.setattr(_reconcile_mod, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(_reconcile_mod, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [])
    monkeypatch.setattr(_reconcile_mod, "_pr_open_state", lambda pr, cwd: (
        "open", "https://github.com/o/r/pull/779", False, [],
        "CHANGES_REQUESTED", "CLEAN",
    ))

    rc = mc.main(["reconcile-prs", "--session-id", "sess-X", "--cwd", str(tmp_path)])

    assert rc == 0
    records = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(records) == 1
    assert records[0]["pr"] == 779
    assert records[0]["changes_requested"] is True


def test_detached_pr_with_dirty_merge_state_is_surfaced(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    sl.append("sess-X", pr=780, branch="bou-conflict", worktree=str(tmp_path / "gone"))
    monkeypatch.setattr(_reconcile_mod, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(_reconcile_mod, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [])
    monkeypatch.setattr(_reconcile_mod, "_pr_open_state", lambda pr, cwd: (
        "open", "https://github.com/o/r/pull/780", False, [],
        "", "DIRTY",
    ))

    rc = mc.main(["reconcile-prs", "--session-id", "sess-X", "--cwd", str(tmp_path)])

    assert rc == 0
    records = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(records) == 1
    assert records[0]["pr"] == 780
    assert records[0]["merge_conflict"] is True
    assert records[0]["merge_state"] == "DIRTY"


def test_detached_pr_with_conflicting_mergeable_is_surfaced(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    sl.append("sess-X", pr=782, branch="bou-conflicting", worktree=str(tmp_path / "gone"))
    monkeypatch.setattr(_reconcile_mod, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(_reconcile_mod, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [])
    monkeypatch.setattr(_reconcile_mod, "_pr_open_state", lambda pr, cwd: (
        "open", "https://github.com/o/r/pull/782", False, [],
        "", "UNKNOWN", "CONFLICTING",
    ))

    rc = mc.main(["reconcile-prs", "--session-id", "sess-X", "--cwd", str(tmp_path)])

    assert rc == 0
    records = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(records) == 1
    assert records[0]["pr"] == 782
    assert records[0]["merge_conflict"] is True
    assert records[0]["merge_state"] == "UNKNOWN"
    assert records[0]["mergeable"] == "CONFLICTING"


def test_markerless_recreated_worktree_is_not_skipped(tmp_path, monkeypatch, capsys):
    reused = tmp_path / "reused-wt"
    reused.mkdir()
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    sl.append("sess-X", pr=781, branch="bou-same", worktree=str(reused))
    monkeypatch.setattr(_reconcile_mod, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(_reconcile_mod, "_iter_worktree_paths", lambda cwd: iter([str(reused)]))
    monkeypatch.setattr(_reconcile_mod, "_read_marker", lambda path: None)
    monkeypatch.setattr(_reconcile_mod, "_current_branch", lambda path: "bou-same")
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [_thread()])
    monkeypatch.setattr(_reconcile_mod, "_pr_open_state", lambda pr, cwd: (
        "open", "https://github.com/o/r/pull/781", False, []))

    rc = mc.main(["reconcile-prs", "--session-id", "sess-X", "--cwd", str(tmp_path)])

    assert rc == 0
    records = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert len(records) == 1
    assert records[0]["pr"] == 781
    assert records[0]["worktree_present"] is False


def test_markerless_recreated_worktree_with_live_owner_is_not_surfaced(
    tmp_path, monkeypatch, capsys
):
    reused = tmp_path / "reused-wt"
    reused.mkdir()
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    sl.append("sess-X", pr=783, branch="bou-same", worktree=str(reused))
    monkeypatch.setattr(_reconcile_mod, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(_reconcile_mod, "_iter_worktree_paths", lambda cwd: iter([str(reused)]))
    monkeypatch.setattr(_reconcile_mod, "_read_marker", lambda path: None)
    monkeypatch.setattr(_reconcile_mod, "_current_branch", lambda path: "bou-same")
    monkeypatch.setattr(
        _reconcile_mod,
        "_live_independent_owner_paths",
        lambda paths, sid: {str(reused)},
    )
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [_thread()])
    monkeypatch.setattr(_reconcile_mod, "_pr_open_state", lambda pr, cwd: (
        "open", "https://github.com/o/r/pull/783", False, []))

    rc = mc.main(["reconcile-prs", "--session-id", "sess-X", "--cwd", str(tmp_path)])

    assert rc == 0
    records = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert records == []


def test_merged_detached_pr_is_pruned_and_omitted(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    sl.append("sess-X", pr=900, branch="bou-merged", worktree=str(tmp_path / "gone"))
    monkeypatch.setattr(_reconcile_mod, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(_reconcile_mod, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(_reconcile_mod, "_pr_open_state", lambda pr, cwd: (
        "merged", "https://x/pull/900", False, []))
    rc = mc.main(["reconcile-prs", "--session-id", "sess-X", "--cwd", str(tmp_path)])
    assert rc == 0
    out = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert out == []  # merged PR not surfaced
    assert sl.read("sess-X") == []  # and pruned from the ledger


def test_p1_sorts_first(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    sl.append("sess-X", pr=1, branch="b1", worktree=str(tmp_path / "g1"))
    sl.append("sess-X", pr=2, branch="b2", worktree=str(tmp_path / "g2"))
    monkeypatch.setattr(_reconcile_mod, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(_reconcile_mod, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(_reconcile_mod, "_pr_open_state", lambda pr, cwd: (
        "open", f"https://x/pull/{pr}", False, []))

    def threads(pr, cwd=None):
        return [_thread(body="P1: crash" if pr == 2 else "nit")]
    monkeypatch.setattr(github_api, "get_review_threads", threads)
    mc.main(["reconcile-prs", "--session-id", "sess-X", "--cwd", str(tmp_path)])
    records = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert [r["pr"] for r in records] == [2, 1]  # P1 PR first
