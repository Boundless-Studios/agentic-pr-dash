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
        mc, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [os.path.abspath(cwd)])
    monkeypatch.setattr(mc, "_detached_pr_records", lambda sid, cwd: [])

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
        mc, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [os.path.abspath(cwd)])
    monkeypatch.setattr(mc, "_detached_pr_records", lambda sid, cwd: [])
    # No open PRs → no waiter demand.
    monkeypatch.setattr(mc, "_owned_open_pr_numbers", lambda owned: set())

    args = argparse.Namespace(cwd=str(anchor), session_id="sess-1",
                              pid=os.getpid(), no_waiter=True)
    rc = mc._stop_gate_impl(args)
    assert rc == 0
