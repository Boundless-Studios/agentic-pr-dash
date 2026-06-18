"""BOU-1596: the per-session PR reconcile aggregates across ALL repos a session
touched, not just the cwd repo.

A session can span multiple repos via different ``--cwd`` checkouts. The anchor's
``agentic-pr-dash.toml`` lists sibling repos in ``maintenance_repo_roots``; the
per-session reconcile must run the existing single-repo primitive once per
``[anchor] + roots`` root (each with its own gh/worktree cwd) and MERGE the
records keyed by ``(repo, pr)`` so a same-number PR in two repos stays distinct.

These mirror the cross-root pattern already proven for the stop-gate / list-owned
in ``test_maintenance_check_multi_repo.py`` and stub the gh/worktree boundary the
same way ``test_reconcile_prs.py`` does.
"""
import argparse
import json
import os
import subprocess
from pathlib import Path

from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash import session_ledger as sl
from agentic_pr_dash import github_api
from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash._maintenance import stop_gate as _stop_gate_mod


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _make_repo(root: Path, roots_cfg: list[str] | None = None,
               slug: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    # A distinct origin remote so _repo_slug() resolves a distinct owner/name per
    # repo offline (the git-remote fallback in config._detect_repo).
    if slug is not None:
        _git("remote", "add", "origin",
             f"https://github.com/{slug}.git", cwd=root)
    (root / "f").write_text("x")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    toml = '[project]\nstate_dir = ".gaia"\ntracker = "none"\n'
    if roots_cfg is not None:
        joined = ", ".join(f'"{r}"' for r in roots_cfg)
        toml += f"maintenance_repo_roots = [{joined}]\n"
    (root / "agentic-pr-dash.toml").write_text(toml)
    return root


def _thread(body="please fix", resolved=False, outdated=False):
    c = ReviewThreadComment(database_id=1, path="f.py", line=1, body=body,
                            author="rev", created_at="2026-01-01T00:00:00Z")
    return ReviewThread(node_id="t1", is_resolved=resolved, is_outdated=outdated, top=c)


def _run_reconcile(anchor, capsys, session_id="sess-X"):
    rc = mc.main(["reconcile-prs", "--session-id", session_id, "--cwd", str(anchor)])
    assert rc == 0
    return [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]


# --------------------------------------------------------------------------
# 1) A session with open PRs in TWO repos surfaces BOTH.
# --------------------------------------------------------------------------

def test_reconcile_surfaces_prs_across_two_repos(tmp_path, monkeypatch, capsys):
    sib = _make_repo(tmp_path / "sibling", slug="o/sib")
    anchor = _make_repo(tmp_path / "anchor", roots_cfg=[str(sib)], slug="o/anchor")
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))

    repo_anchor = mc._repo_slug(str(anchor))
    repo_sib = mc._repo_slug(str(sib))
    # Each repo's ledger entry is scoped to that repo (different PR numbers).
    sl.append("sess-X", pr=11, branch="b-anchor", worktree=str(tmp_path / "gone-a"),
              repo=repo_anchor)
    sl.append("sess-X", pr=22, branch="b-sib", worktree=str(tmp_path / "gone-b"),
              repo=repo_sib)

    monkeypatch.setattr(mc, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(mc, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [_thread()])

    def open_state(pr, cwd):
        return ("open", f"https://github.com/o/r/pull/{pr}", False, [])
    monkeypatch.setattr(mc, "_pr_open_state", open_state)

    records = _run_reconcile(anchor, capsys)
    by_pr = {r["pr"]: r for r in records}
    assert set(by_pr) == {11, 22}
    assert by_pr[11]["repo"] == repo_anchor
    assert by_pr[22]["repo"] == repo_sib


# --------------------------------------------------------------------------
# 2) SAME PR number in both repos → two distinct records (no collision).
# --------------------------------------------------------------------------

def test_same_pr_number_in_two_repos_does_not_collide(tmp_path, monkeypatch, capsys):
    sib = _make_repo(tmp_path / "sibling", slug="o/sib")
    anchor = _make_repo(tmp_path / "anchor", roots_cfg=[str(sib)], slug="o/anchor")
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))

    repo_anchor = mc._repo_slug(str(anchor))
    repo_sib = mc._repo_slug(str(sib))
    sl.append("sess-X", pr=42, branch="b-anchor", worktree=str(tmp_path / "gone-a"),
              repo=repo_anchor)
    sl.append("sess-X", pr=42, branch="b-sib", worktree=str(tmp_path / "gone-b"),
              repo=repo_sib)

    monkeypatch.setattr(_reconcile_mod, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(_reconcile_mod, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [_thread()])

    # The URL is derived from the per-repo cwd so the two #42 records differ.
    def open_state(pr, cwd):
        slug = mc._repo_slug(cwd) or "unknown"
        return ("open", f"https://github.com/{slug}/pull/{pr}", False, [])
    monkeypatch.setattr(_reconcile_mod, "_pr_open_state", open_state)

    records = _run_reconcile(anchor, capsys)
    # Two distinct records, both PR #42, but different repo + url.
    assert len(records) == 2
    assert all(r["pr"] == 42 for r in records)
    assert {r["repo"] for r in records} == {repo_anchor, repo_sib}
    assert len({r["url"] for r in records}) == 2


# --------------------------------------------------------------------------
# 3) A legacy repo-less ledger entry resolves once and is never pruned
#    cross-repo by a sibling's same-number closed PR.
# --------------------------------------------------------------------------

def test_legacy_entry_resolves_against_anchor_not_pruned_cross_repo(
    tmp_path, monkeypatch, capsys
):
    sib = _make_repo(tmp_path / "sibling", slug="o/sib")
    anchor = _make_repo(tmp_path / "anchor", roots_cfg=[str(sib)], slug="o/anchor")
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))

    # Legacy: written before repo scoping, so repo="". Its true repo is the anchor;
    # the SAME number is CLOSED on the sibling remote.
    sl.append("sess-X", pr=99, branch="b-legacy", worktree=str(tmp_path / "gone"),
              repo="")

    monkeypatch.setattr(_reconcile_mod, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(_reconcile_mod, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [_thread()])

    repo_anchor = mc._repo_slug(str(anchor))

    def open_state(pr, cwd):
        # Open on the anchor remote; CLOSED on the sibling remote.
        if mc._repo_slug(cwd) == repo_anchor:
            return ("open", f"https://github.com/o/anchor/pull/{pr}", False, [])
        return ("closed", f"https://github.com/o/sib/pull/{pr}", False, [])
    monkeypatch.setattr(_reconcile_mod, "_pr_open_state", open_state)

    records = _run_reconcile(anchor, capsys)
    # Surfaces exactly once (the open anchor record), not duplicated per root.
    assert len(records) == 1
    assert records[0]["pr"] == 99
    assert records[0]["url"].endswith("/anchor/pull/99")

    # The legacy ledger row MUST survive — the sibling's same-number CLOSED PR
    # must not prune it cross-repo (codex PR #30 / PR #32 invariant).
    surviving = sl.read("sess-X")
    assert any((not e.repo) and e.pr == 99 for e in surviving)


def test_single_root_legacy_merged_pr_is_still_pruned(tmp_path, monkeypatch, capsys):
    # With a single root the repo is unambiguous, so a merged legacy entry is still
    # pruned (preserving the pre-BOU-1596 single-root behavior).
    anchor = _make_repo(tmp_path / "anchor", slug="o/anchor")  # no maintenance_repo_roots
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    sl.append("sess-X", pr=500, branch="b-merged", worktree=str(tmp_path / "gone"),
              repo="")  # legacy
    monkeypatch.setattr(_reconcile_mod, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(_reconcile_mod, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(_reconcile_mod, "_pr_open_state",
                        lambda pr, cwd: ("merged", "https://x/pull/500", False, []))

    records = _run_reconcile(anchor, capsys)
    assert records == []
    assert sl.read("sess-X") == []  # pruned


# --------------------------------------------------------------------------
# 4) Stop-gate aggregates across roots, P1-first ordering preserved.
# --------------------------------------------------------------------------

def test_reconcile_p1_first_across_roots(tmp_path, monkeypatch, capsys):
    sib = _make_repo(tmp_path / "sibling", slug="o/sib")
    anchor = _make_repo(tmp_path / "anchor", roots_cfg=[str(sib)], slug="o/anchor")
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))

    repo_anchor = mc._repo_slug(str(anchor))
    repo_sib = mc._repo_slug(str(sib))
    # Anchor PR is a nit; sibling PR is a P1 — P1 must sort first even though it
    # comes from a non-anchor root.
    sl.append("sess-X", pr=1, branch="b-anchor", worktree=str(tmp_path / "g1"),
              repo=repo_anchor)
    sl.append("sess-X", pr=2, branch="b-sib", worktree=str(tmp_path / "g2"),
              repo=repo_sib)

    monkeypatch.setattr(mc, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(mc, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(mc, "_pr_open_state",
                        lambda pr, cwd: ("open", f"https://x/pull/{pr}", False, []))

    def threads(pr, cwd=None):
        return [_thread(body="P1: crash" if pr == 2 else "nit")]
    monkeypatch.setattr(github_api, "get_review_threads", threads)

    records = _run_reconcile(anchor, capsys)
    assert [r["pr"] for r in records] == [2, 1]  # sibling P1 first
    assert records[0]["p1"] is True
    assert records[0]["repo"] == repo_sib


def test_stop_gate_aggregates_detached_across_roots_p1_first(tmp_path, monkeypatch):
    # The stop-gate surfaces detached (worktree-gone) PRs across all roots via
    # _detached_records_across_roots, sorted P1-first. A sibling-repo P1 must block.
    sib = _make_repo(tmp_path / "sibling", slug="o/sib")
    anchor = _make_repo(tmp_path / "anchor", roots_cfg=[str(sib)], slug="o/anchor")

    rec_anchor = {"pr": 1, "p1": False, "unresolved_threads": 1, "state": "open",
                  "url": "u-anchor", "ci_failing": False, "changes_requested": False,
                  "merge_conflict": False, "branch": "b-anchor"}
    rec_sib = {"pr": 2, "p1": True, "unresolved_threads": 1, "state": "open",
               "url": "u-sib", "ci_failing": False, "changes_requested": False,
               "merge_conflict": False, "branch": "b-sib"}

    def fake_detached(sid, cwd, include_legacy=True, prune_legacy=True):
        if os.path.realpath(cwd) == os.path.realpath(str(sib)):
            return [rec_sib]
        if os.path.realpath(cwd) == os.path.realpath(str(anchor)):
            return [rec_anchor]
        return []
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", fake_detached)
    monkeypatch.setattr(mc, "_check_worktree",
                        lambda worktree, sid, *, claim=False: (0, ""))
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees",
                        lambda sid, cwd: [os.path.abspath(cwd)])
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: set())

    captured = {}
    real_block = _stop_gate_mod._build_stop_block

    def spy_block(pending):
        captured["pending"] = pending
        return real_block(pending)
    monkeypatch.setattr(_stop_gate_mod, "_build_stop_block", spy_block)

    args = argparse.Namespace(cwd=str(anchor), session_id="sess-X",
                              pid=os.getpid(), no_waiter=True)
    rc = mc._stop_gate_impl(args)
    assert rc == 2  # a sibling-repo blocker blocks the stop
    # P1 (sibling PR #2) ordered before the anchor nit (PR #1).
    pending = captured["pending"]
    assert "PR #2" in pending[0][0]  # sibling P1 first
    assert "[P1]" in pending[0][1]
