"""BOU-1546: maintenance_check aggregates across configured repo roots.

gaia-free is the super-repo: its agentic-pr-dash.toml lists sibling repo
main-checkout paths in `maintenance_repo_roots`, and stop-gate / list-owned must
expand [anchor] + roots, running the existing single-root discovery per root with
per-root config isolation. These are the RED-first regression tests.
"""
import argparse
import os
import subprocess
from pathlib import Path

from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import stop_gate as _stop_gate_mod


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _make_repo(root: Path, roots_cfg: list[str] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (root / "f").write_text("x")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    toml = '[project]\nstate_dir = ".gaia"\ntracker = "none"\n'
    if roots_cfg is not None:
        joined = ", ".join(f'"{r}"' for r in roots_cfg)
        toml += f"maintenance_repo_roots = [{joined}]\n"
    (root / "agentic-pr-dash.toml").write_text(toml)
    return root


def _arm(worktree: Path, session_id: str, pr: int, pid: int):
    d = worktree / ".gaia"
    d.mkdir(parents=True, exist_ok=True)
    (d / "pr-watch.armed").write_text(
        f"pr={pr}\nsession_id={session_id}\npid={pid}\narmed_at=1\n")


def _rp(p) -> str:
    return os.path.realpath(str(p))


# --- _resolve_maintenance_roots --------------------------------------------

def test_resolve_maintenance_roots_includes_configured(tmp_path):
    sib = _make_repo(tmp_path / "sibling")
    anchor = _make_repo(tmp_path / "anchor", roots_cfg=[str(sib)])
    roots = [_rp(r) for r in mc._resolve_maintenance_roots(str(anchor))]
    assert _rp(anchor) in roots
    assert _rp(sib) in roots


def test_resolve_skips_missing_root(tmp_path):
    anchor = _make_repo(tmp_path / "anchor",
                        roots_cfg=[str(tmp_path / "does-not-exist")])
    roots = [_rp(r) for r in mc._resolve_maintenance_roots(str(anchor))]
    assert _rp(tmp_path / "does-not-exist") not in roots
    assert _rp(anchor) in roots


def test_resolve_no_config_returns_only_anchor(tmp_path):
    anchor = _make_repo(tmp_path / "anchor")  # no maintenance_repo_roots
    roots = [_rp(r) for r in mc._resolve_maintenance_roots(str(anchor))]
    assert roots == [_rp(anchor)]


# --- list-owned aggregation -------------------------------------------------

def test_list_owned_aggregates_across_roots(tmp_path, monkeypatch, capsys):
    sib = _make_repo(tmp_path / "sibling")
    anchor = _make_repo(tmp_path / "anchor", roots_cfg=[str(sib)])
    _arm(anchor, "sess-1", 100, os.getpid())
    _arm(sib, "sess-1", 200, os.getpid())
    # No gh in tests: reconciliation is skipped, the markered pass still works.
    monkeypatch.setattr(mc, "_list_my_open_prs", lambda cwd: {})

    args = argparse.Namespace(session_id="sess-1", cwd=str(anchor),
                              pid=os.getpid())
    rc = mc._cmd_list_owned(args)
    out = [_rp(p) for p in capsys.readouterr().out.splitlines() if p.strip()]
    assert rc == 0
    assert _rp(anchor) in out
    assert _rp(sib) in out


def test_list_owned_excludes_live_foreign_owner(tmp_path, monkeypatch, capsys):
    # A sibling worktree owned by a LIVE foreign session must not be adopted —
    # cross-repo, ownership/self-exclusion still yields one executor per PR.
    sib = _make_repo(tmp_path / "sibling")
    anchor = _make_repo(tmp_path / "anchor", roots_cfg=[str(sib)])
    _arm(anchor, "sess-1", 100, os.getpid())
    # Foreign marker on the sibling, pid alive (this process) but session != ours.
    _arm(sib, "other-sess", 200, os.getpid())
    # Even if a PR exists for the sibling branch, the live foreign owner blocks adoption.
    monkeypatch.setattr(mc, "_list_my_open_prs", lambda cwd: {"main": (200, False)})

    args = argparse.Namespace(session_id="sess-1", cwd=str(anchor),
                              pid=os.getpid())
    rc = mc._cmd_list_owned(args)
    out = [_rp(p) for p in capsys.readouterr().out.splitlines() if p.strip()]
    assert rc == 0
    assert _rp(anchor) in out
    assert _rp(sib) not in out


# --- stop-gate aggregation --------------------------------------------------

def test_stop_gate_blocks_when_sibling_has_pending(tmp_path, monkeypatch):
    sib = _make_repo(tmp_path / "sibling")
    anchor = _make_repo(tmp_path / "anchor", roots_cfg=[str(sib)])

    def fake_check(worktree, session_id, *, claim=False):
        if _rp(worktree) == _rp(sib):
            return 10, "PR #200 has 3 unresolved review threads"
        return 0, ""

    monkeypatch.setattr(mc, "_check_worktree", fake_check)
    # Each root reports its own worktree as owned.
    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [os.path.abspath(cwd)])
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])

    args = argparse.Namespace(cwd=str(anchor), session_id="sess-1",
                              pid=os.getpid(), no_waiter=True)
    rc = mc._stop_gate_impl(args)
    assert rc == 2  # sibling pending must block the stop


def test_stop_gate_clean_across_roots_exits_zero(tmp_path, monkeypatch):
    sib = _make_repo(tmp_path / "sibling")
    anchor = _make_repo(tmp_path / "anchor", roots_cfg=[str(sib)])

    monkeypatch.setattr(mc, "_check_worktree",
                        lambda worktree, sid, *, claim=False: (0, ""))
    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [os.path.abspath(cwd)])
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    # No open PRs → no waiter demand.
    monkeypatch.setattr(_stop_gate_mod, "_owned_open_pr_numbers", lambda owned: set())

    args = argparse.Namespace(cwd=str(anchor), session_id="sess-1",
                              pid=os.getpid(), no_waiter=True)
    rc = mc._stop_gate_impl(args)
    assert rc == 0


# --- codex PR #30 review fixes ----------------------------------------------

def test_config_resolves_relative_root_against_config_dir(tmp_path):
    # codex P2: a RELATIVE maintenance_repo_root must resolve against the config
    # file's dir, not the process cwd.
    from agentic_pr_dash import config as cfg_mod
    sib = _make_repo(tmp_path / "sibling")
    anchor = _make_repo(tmp_path / "anchor", roots_cfg=["../sibling"])
    cfg = cfg_mod.load(str(anchor))
    assert _rp(sib) in [_rp(r) for r in cfg.maintenance_repo_roots]


def test_detached_records_dedup_by_root_and_number(tmp_path, monkeypatch):
    # codex P2: same PR number in two repos must BOTH survive (numbers are only
    # unique within a repo).
    sib = _make_repo(tmp_path / "sibling")
    anchor = _make_repo(tmp_path / "anchor", roots_cfg=[str(sib)])
    rec_a = {"pr": 42, "p1": True, "unresolved_threads": 1, "state": "open", "url": "u-a"}
    rec_b = {"pr": 42, "p1": False, "unresolved_threads": 2, "state": "open", "url": "u-b"}

    def fake_detached(sid, cwd, include_legacy=True, prune_legacy=True):
        if _rp(cwd) == _rp(sib):
            return [rec_b]
        if _rp(cwd) == _rp(anchor):
            return [rec_a]
        return []

    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", fake_detached)
    recs = mc._detached_records_across_roots("sess-1", str(anchor))
    assert len(recs) == 2
    assert {r["url"] for r in recs} == {"u-a", "u-b"}


def test_owned_worktrees_across_roots_aggregates(tmp_path, monkeypatch):
    # codex P1: the shared helper (used by both stop-gate and the waiter) spans
    # all roots, so a spawned waiter actually covers sibling-repo PRs.
    sib = _make_repo(tmp_path / "sibling")
    anchor = _make_repo(tmp_path / "anchor", roots_cfg=[str(sib)])
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees",
                        lambda sid, cwd: [os.path.abspath(cwd)])
    owned = [_rp(p) for p in mc._owned_worktrees_across_roots("s", str(anchor))]
    assert _rp(anchor) in owned
    assert _rp(sib) in owned


def test_ledger_strict_read_and_prune_exclude_legacy(tmp_path, monkeypatch):
    # codex P2: a legacy repo-less ledger entry must not be read/pruned when a
    # sibling root is processed strictly.
    from agentic_pr_dash import session_ledger as sl
    monkeypatch.setattr(sl, "ledger_path", lambda sid: str(tmp_path / f"{sid}.jsonl"))
    sl.append("sess", 42, "branchA", "/wt/a", repo="ownerA/repoA")
    sl.append("sess", 42, "legacy", "/wt/legacy", repo="")  # legacy: no repo

    strict = sl.read("sess", repo="ownerA/repoA", include_legacy=False)
    assert all(e.repo == "ownerA/repoA" for e in strict)
    loose = sl.read("sess", repo="ownerA/repoA", include_legacy=True)
    assert any(not e.repo for e in loose)

    # Strict prune of #42 against repoA must NOT drop the legacy #42.
    sl.prune("sess", {42}, repo="ownerA/repoA", include_legacy=False)
    after = sl.read("sess")
    assert any((not e.repo) and e.pr == 42 for e in after)        # legacy survived
    assert not any(e.repo == "ownerA/repoA" and e.pr == 42 for e in after)  # repoA dropped


def test_detached_across_roots_reads_legacy_everywhere_never_prunes(tmp_path, monkeypatch):
    # codex PR #32 P2b: a legacy row's true repo is unknown, so EVERY root reads
    # legacy (to surface a sibling-owned PR) but NONE prune it in cross-root mode.
    sib = _make_repo(tmp_path / "sibling")
    anchor = _make_repo(tmp_path / "anchor", roots_cfg=[str(sib)])
    calls = {}

    def fake_detached(sid, cwd, include_legacy=True, prune_legacy=True):
        calls[_rp(cwd)] = (include_legacy, prune_legacy)
        return []

    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", fake_detached)
    mc._detached_records_across_roots("sess", str(anchor))
    assert calls[_rp(anchor)] == (True, False)
    assert calls[_rp(sib)] == (True, False)


def test_detached_single_root_still_prunes_legacy(tmp_path, monkeypatch):
    # codex PR #32 P3: a single-root checkout (no maintenance_repo_roots) must
    # still prune merged/closed legacy rows — pruning is only unsafe with >1 root.
    anchor = _make_repo(tmp_path / "anchor")  # no configured roots
    calls = {}

    def fake_detached(sid, cwd, include_legacy=True, prune_legacy=True):
        calls[_rp(cwd)] = (include_legacy, prune_legacy)
        return []

    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", fake_detached)
    mc._detached_records_across_roots("sess", str(anchor))
    assert calls[_rp(anchor)] == (True, True)  # legacy pruning preserved


def test_ledger_strict_read_empty_repo_excludes_legacy(tmp_path, monkeypatch):
    # codex PR #32 P2: a strict read/prune with an EMPTY repo (undetectable remote)
    # must still exclude legacy rows (their repo is also "").
    from agentic_pr_dash import session_ledger as sl
    monkeypatch.setattr(sl, "ledger_path", lambda sid: str(tmp_path / f"{sid}.jsonl"))
    sl.append("sess", 7, "legacy", "/wt/legacy", repo="")  # legacy, no repo

    assert sl.read("sess", repo="", include_legacy=False) == []      # strict: no legacy
    assert any(not e.repo for e in sl.read("sess", repo="", include_legacy=True))  # loose: legacy

    # Strict prune with empty repo must NOT drop the legacy row.
    sl.prune("sess", {7}, repo="", include_legacy=False)
    assert any((not e.repo) and e.pr == 7 for e in sl.read("sess"))
