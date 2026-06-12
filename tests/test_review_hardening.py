"""Regression tests for the PR #16 review hardening (BOU-1587)."""
import json
import threading

from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash import session_ledger as sl
from agentic_pr_dash import github_api
from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment


def _thread():
    c = ReviewThreadComment(database_id=1, path="f.py", line=1, body="fix",
                            author="rev", created_at="2026-01-01T00:00:00Z")
    return ReviewThread(node_id="t1", is_resolved=False, is_outdated=False, top=c)


# --- #1 concurrent append serialization ------------------------------------

def test_concurrent_appends_keep_all_prs(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path))

    def arm(pr):
        sl.append("sess-X", pr=pr, branch=f"b{pr}", worktree=f"/wt/{pr}")

    threads = [threading.Thread(target=arm, args=(n,)) for n in range(1, 21)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert {e.pr for e in sl.read("sess-X")} == set(range(1, 21))  # none dropped


# --- #3 draft detached PR is skipped ---------------------------------------

def test_detached_draft_pr_is_not_surfaced(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    sl.append("sess-X", pr=800, branch="bou-draft", worktree=str(tmp_path / "gone"))
    monkeypatch.setattr(mc, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(mc, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [_thread()])
    monkeypatch.setattr(mc, "_pr_open_state",
                        lambda pr, cwd: ("draft", "https://x/pull/800", False, []))
    mc.main(["reconcile-prs", "--session-id", "sess-X", "--cwd", str(tmp_path)])
    out = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert out == []                       # draft not surfaced
    assert 800 in {e.pr for e in sl.read("sess-X")}  # but NOT pruned (may reopen)


# --- #6 path reuse: same dir, different branch -> still detached ------------

def test_path_reuse_does_not_mask_detached_pr(tmp_path, monkeypatch, capsys):
    reused = tmp_path / "reused-wt"
    reused.mkdir()
    sl_dir = tmp_path / "ledger"
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(sl_dir))
    # Ledger entry for PR 810 on branch bou-old, worktree path = reused dir.
    sl.append("sess-X", pr=810, branch="bou-old", worktree=str(reused))
    monkeypatch.setattr(mc, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    # The dir is present again, but now hosts a DIFFERENT branch/PR.
    monkeypatch.setattr(mc, "_iter_worktree_paths", lambda cwd: iter([str(reused)]))
    monkeypatch.setattr(mc, "_read_marker", lambda p: {"pr": "999"})  # different PR
    monkeypatch.setattr(mc, "_current_branch", lambda p: "bou-new")    # different branch
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [_thread()])
    monkeypatch.setattr(mc, "_pr_open_state",
                        lambda pr, cwd: ("open", "https://x/pull/810", False, []))
    mc.main(["reconcile-prs", "--session-id", "sess-X", "--cwd", str(tmp_path)])
    records = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert any(r["pr"] == 810 and r["worktree_present"] is False for r in records)


# --- #7 detached stop-block omits the fake --cwd completion command --------

def test_detached_stop_block_has_no_fake_cwd_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    sl.append("sess-X", pr=820, branch="bou-d", worktree=str(tmp_path / "gone"))
    monkeypatch.setattr(mc, "_collect_stop_gate_worktrees", lambda sid, cwd: [])
    monkeypatch.setattr(mc, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(mc, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(mc, "_pr_open_state",
                        lambda pr, cwd: ("open", "https://x/pull/820", False, []))
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [_thread()])
    rc = mc.main(["stop-gate", "--session-id", "sess-X", "--cwd", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "complete --cwd (no worktree)" not in err   # no non-executable command
    assert "--cwd (no worktree)" not in err
    assert "820" in err                                 # but the PR is still named


# --- round-2 #2: reconcile scopes ledger PRs to the target repo -------------

def test_reconcile_only_surfaces_target_repo_pr(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    # Same session opened PR #12 in two different repos.
    sl.append("sess-X", pr=12, branch="a", worktree=str(tmp_path / "wtA"),
              repo="org/repoA")
    sl.append("sess-X", pr=12, branch="b", worktree=str(tmp_path / "wtB"),
              repo="org/repoB")
    monkeypatch.setattr(mc, "_repo_slug", lambda cwd: "org/repoA")
    monkeypatch.setattr(mc, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(mc, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [_thread()])

    queried_cwds = []

    def fake_state(pr, cwd):
        queried_cwds.append((pr, cwd))
        return ("open", "https://x/pull/12", False, [])

    monkeypatch.setattr(mc, "_pr_open_state", fake_state)
    mc.main(["reconcile-prs", "--session-id", "sess-X", "--cwd", str(tmp_path)])
    records = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    # Exactly one record (repoA's PR 12); repoB's same-number PR is NOT surfaced.
    assert len(records) == 1
    assert records[0]["pr"] == 12 and records[0]["branch"] == "a"
    # And the foreign-repo PR was never queried against repoA's cwd.
    assert len(queried_cwds) == 1


def test_reconcile_prune_does_not_drop_foreign_repo_pr(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    sl.append("sess-X", pr=12, branch="a", worktree=str(tmp_path / "wtA"),
              repo="org/repoA")
    sl.append("sess-X", pr=12, branch="b", worktree=str(tmp_path / "wtB"),
              repo="org/repoB")
    monkeypatch.setattr(mc, "_repo_slug", lambda cwd: "org/repoA")
    monkeypatch.setattr(mc, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(mc, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [])
    # repoA's PR 12 is closed -> would be pruned.
    monkeypatch.setattr(mc, "_pr_open_state",
                        lambda pr, cwd: ("closed", "", False, []))
    mc.main(["reconcile-prs", "--session-id", "sess-X", "--cwd", str(tmp_path)])
    remaining = sl.read("sess-X")
    # repoB's PR 12 must survive the repoA-scoped prune.
    assert {(e.repo, e.pr) for e in remaining} == {("org/repoB", 12)}
