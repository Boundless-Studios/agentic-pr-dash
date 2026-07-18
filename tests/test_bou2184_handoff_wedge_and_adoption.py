"""BOU-2184: handoff self-wedge + cross-repo adoption gap.

Observed live on agentic-pr-dash#79 (worktree ``bou-2155``):

1. The loop/dashboard wrote its maintenance prompt by MODIFYING a tracked
   ``MAINTENANCE_HANDOFF.md`` in the target worktree; the claim reclaim
   dirty-check counted that as "dirty or unpushed changes" and wedged the claim
   in ``manual_intervention`` — the handoff artifact WAS the dirt. Fix: the
   handoff is written into the worktree's state dir, and the reclaim dirty-check
   excludes the loop's own artifacts (legacy root-level handoff + state dir).

2. ``list-owned``/``reconcile-prs`` from a session with ``maintenance_repo_roots``
   configured returned empty despite an owned open non-draft ``@me`` PR with a
   live sibling worktree — the sibling worktree was markered by a DEAD sub-agent
   session, which the BOU-1953 no-takeover gate made permanently invisible, and
   the ledger orphan path skipped any entry whose worktree still existed. Fix:
   sibling-root scans take over dead-markered worktrees with an open non-draft
   ``@me`` PR, and ledger orphan adoption handles present worktrees whose marker
   owner is dead.
"""
import argparse
import json
import os
import subprocess
from datetime import datetime, timezone

from agent_coordinator.models import OwnerIdentity
from agent_coordinator.service import TaskCoordinator
from agent_coordinator.store import JsonlClaimStore

from agentic_pr_dash import coordinator, github_api, maintenance
from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash import session_ledger as sl
from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment
from agentic_pr_dash.models import PRData, ReviewComment
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod

BASE_TIME = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
DEAD_PID = 999999


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _make_repo(root, roots_cfg=None, slug=None, state_dir=".gaia"):
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    if slug is not None:
        _git("remote", "add", "origin",
             f"https://github.com/{slug}.git", cwd=root)
    (root / "f").write_text("x")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    toml = f'[project]\nstate_dir = "{state_dir}"\ntracker = "none"\n'
    if roots_cfg is not None:
        joined = ", ".join(f'"{r}"' for r in roots_cfg)
        toml += f"maintenance_repo_roots = [{joined}]\n"
    (root / "agentic-pr-dash.toml").write_text(toml)
    return root


def _add_worktree(root, dest, branch):
    _git("worktree", "add", "-q", "-b", branch, str(dest), cwd=root)
    return dest


def _arm(worktree, session_id, pr, pid, state_dir=".agentic-pr-dash"):
    d = worktree / state_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "pr-watch.armed").write_text(
        f"pr={pr}\nsession_id={session_id}\npid={pid}\narmed_at=1\n")


def _marker_fields(worktree, state_dir=".agentic-pr-dash"):
    text = (worktree / state_dir / "pr-watch.armed").read_text()
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def _thread(body="please fix", resolved=False, outdated=False):
    c = ReviewThreadComment(database_id=1, path="f.py", line=1, body=body,
                            author="rev", created_at="2026-01-01T00:00:00Z")
    return ReviewThread(node_id="t1", is_resolved=resolved, is_outdated=outdated, top=c)


def _comment(comment_id):
    return ReviewComment(id=comment_id, author="reviewer", body="please fix",
                         created_at="2026-07-18T12:00:00Z")


def _pr(**kwargs):
    base = {
        "number": 79,
        "title": "Fix PR",
        "branch": "bou-2155",
        "url": "https://github.com/Boundless-Studios/agentic-pr-dash/pull/79",
        "worktree_path": "/tmp/wt",
        "failing_checks": [],
        "review_comments": [_comment(1), _comment(2), _comment(3)],
    }
    base.update(kwargs)
    return PRData(**base)


# ---------------------------------------------------------------------------
# 1) Reclaim dirty-check: loop artifacts are not dirt.
# ---------------------------------------------------------------------------

def test_state_dir_artifacts_do_not_make_worktree_dirty(tmp_path):
    repo = _make_repo(tmp_path / "repo", slug="o/r", state_dir=".gaia")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "toml", cwd=repo)

    # Loop-written state under an UN-gitignored state dir: maintenance state,
    # markers, and the handoff itself.
    state_dir = repo / ".gaia"
    (state_dir / "pr-maintenance").mkdir(parents=True)
    (state_dir / "pr-maintenance" / "pr-79.json").write_text("{}\n")
    (state_dir / "MAINTENANCE_HANDOFF.md").write_text("# handoff\n")
    # Legacy TRACKED root-level handoff, modified by an old writer.
    handoff = repo / maintenance.HANDOFF_FILENAME
    handoff.write_text("old\n")
    _git("add", maintenance.HANDOFF_FILENAME, cwd=repo)
    _git("commit", "-qm", "legacy handoff", cwd=repo)
    handoff.write_text("new prompt content\n")

    assert coordinator.worktree_has_dirty_or_unpushed_changes(str(repo)) is False

    # A REAL source change still counts.
    (repo / "f").write_text("changed")
    assert coordinator.worktree_has_dirty_or_unpushed_changes(str(repo)) is True


def test_rename_of_real_file_into_state_dir_counts_as_dirt(tmp_path):
    """A staged rename ``R real.py -> .gaia/...`` keeps its SOURCE path in the
    dirty decision: only renames whose both sides are loop artifacts are
    ignored, so reclaim can never bulldoze an agent-staged source removal."""
    repo = _make_repo(tmp_path / "repo", slug="o/r", state_dir=".gaia")
    (repo / "real.py").write_text("code\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "base", cwd=repo)

    # Rename a real tracked source file INTO the state dir.
    (repo / ".gaia").mkdir(exist_ok=True)
    _git("mv", "real.py", ".gaia/real.py", cwd=repo)
    assert coordinator.worktree_has_dirty_or_unpushed_changes(str(repo)) is True


def test_rename_between_loop_artifacts_is_not_dirt(tmp_path):
    """Legacy tracked root handoff renamed into the state dir: both sides are
    loop artifacts, so the rename stays reclaimable."""
    repo = _make_repo(tmp_path / "repo", slug="o/r", state_dir=".gaia")
    handoff = repo / maintenance.HANDOFF_FILENAME
    handoff.write_text("# handoff\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "base", cwd=repo)

    (repo / ".gaia").mkdir(exist_ok=True)
    _git("mv", maintenance.HANDOFF_FILENAME, ".gaia/" + maintenance.HANDOFF_FILENAME, cwd=repo)
    assert coordinator.worktree_has_dirty_or_unpushed_changes(str(repo)) is False


def test_claim_with_only_loop_artifact_dirt_is_reclaimable(tmp_path, monkeypatch):
    """The live wedge: a released/reclaimable claim whose owner worktree is dirty
    ONLY by the loop's own handoff/state-dir writes must dispatch, not park in
    manual_intervention."""
    store = tmp_path / "claims.jsonl"
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(store))
    owner_wt = _make_repo(tmp_path / "owner", slug="o/r", state_dir=".gaia")
    _git("add", "-A", cwd=owner_wt)
    _git("commit", "-qm", "toml", cwd=owner_wt)
    (owner_wt / ".gaia").mkdir(exist_ok=True)
    (owner_wt / ".gaia" / "MAINTENANCE_HANDOFF.md").write_text("# handoff\n")
    (owner_wt / maintenance.HANDOFF_FILENAME).write_text("legacy untracked handoff\n")

    pr = _pr(worktree_path=str(tmp_path / "current"))
    coord = TaskCoordinator(JsonlClaimStore(store))
    claim = coord.claim_task(
        coordinator.task_identity_for_pr(pr),
        OwnerIdentity(session_id="agentic-pr-dash-dashboard", pid=DEAD_PID,
                      worktree_path=str(owner_wt)),
        lease_seconds=300,
        now=BASE_TIME,
    )
    coord.release_claim(claim.claim_id, owner_session_id="agentic-pr-dash-dashboard",
                        reason="failed", now=BASE_TIME)

    decision = coordinator.dispatch_decision_for_pr(pr, now=BASE_TIME)

    assert decision.state != "manual_intervention"
    assert decision.should_dispatch is True


# ---------------------------------------------------------------------------
# 2) Handoff is written to the state dir, not the worktree root.
# ---------------------------------------------------------------------------

def test_handoff_written_to_state_dir_not_root(tmp_path):
    repo = _make_repo(tmp_path / "repo", slug="o/r", state_dir=".gaia")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "toml", cwd=repo)

    state = maintenance.build_maintenance_state(
        pr_number=79, branch="bou-2155", worktree_path=str(repo), blockers=[])
    maintenance.write_pipeline_handoff(state, "fix the things")

    inside = repo / ".gaia" / maintenance.HANDOFF_FILENAME
    assert inside.is_file()
    assert "fix the things" in inside.read_text()
    assert not (repo / maintenance.HANDOFF_FILENAME).exists()
    # And the write itself leaves the worktree reclaim-clean.
    assert coordinator.worktree_has_dirty_or_unpushed_changes(str(repo)) is False


# ---------------------------------------------------------------------------
# 3) Cross-repo adoption: a sibling-root worktree armed by a DEAD sub-agent
#    session surfaces in list-owned / reconcile-prs.
# ---------------------------------------------------------------------------

def _sibling_fixture(tmp_path, monkeypatch, *, marker_pid=DEAD_PID):
    sib = _make_repo(tmp_path / "sibling", slug="o/sib")
    anchor = _make_repo(tmp_path / "anchor", roots_cfg=[str(sib)], slug="o/anchor")
    sib_wt = _add_worktree(sib, tmp_path / "sibling-wt-79", "bou-2155")
    # Armed by the (now dead, unless marker_pid is live) sub-agent session.
    _arm(sib_wt, "dead-subagent-sess", 79, marker_pid)

    sib_real = os.path.realpath(str(sib))

    def fake_open_prs(cwd, timeout=15):
        if os.path.realpath(str(cwd)) == sib_real:
            return {"bou-2155": (79, False)}
        return {}

    monkeypatch.setattr(_worktrees_mod, "_list_my_open_prs", fake_open_prs)
    monkeypatch.setattr(_worktrees_mod, "_live_independent_owner_paths",
                        lambda paths, session_id: set())
    monkeypatch.setattr(_reconcile_mod, "_pr_open_state",
                        lambda pr, cwd: ("open", f"https://github.com/o/sib/pull/{pr}",
                                         False, []))
    monkeypatch.setattr(github_api, "get_review_threads",
                        lambda pr, cwd=None: [_thread()])
    return anchor, sib, sib_wt


def test_reconcile_prs_surfaces_sibling_worktree_pr_with_dead_owner(
    tmp_path, monkeypatch, capsys
):
    anchor, sib, sib_wt = _sibling_fixture(tmp_path, monkeypatch)

    rc = mc.main(["reconcile-prs", "--session-id", "sess-X", "--cwd", str(anchor)])
    assert rc == 0
    records = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]

    by_key = {(r["repo"], r["pr"]): r for r in records}
    rec = by_key.get(("o/sib", 79))
    assert rec is not None, f"sibling PR missing from reconcile output: {records}"
    assert rec["worktree_present"] is True
    assert rec["branch"] == "bou-2155"
    assert rec["unresolved_threads"] == 1
    # Adoption is durable: the dead sub-agent's marker was re-stamped to us.
    assert _marker_fields(sib_wt)["session_id"] == "sess-X"


def test_list_owned_surfaces_dead_markered_sibling_worktree(
    tmp_path, monkeypatch, capsys
):
    anchor, sib, sib_wt = _sibling_fixture(tmp_path, monkeypatch)

    args = argparse.Namespace(session_id="sess-X", cwd=str(anchor), pid=os.getpid())
    rc = mc._cmd_list_owned(args)
    assert rc == 0
    out = [os.path.realpath(p) for p in capsys.readouterr().out.splitlines() if p.strip()]
    assert os.path.realpath(str(sib_wt)) in out
    assert _marker_fields(sib_wt)["session_id"] == "sess-X"


def test_live_foreign_marker_in_sibling_root_is_not_taken_over(
    tmp_path, monkeypatch, capsys
):
    # Same shape, but the marker's owner pid is ALIVE — takeover must not happen.
    anchor, sib, sib_wt = _sibling_fixture(tmp_path, monkeypatch,
                                           marker_pid=os.getpid())

    args = argparse.Namespace(session_id="sess-X", cwd=str(anchor), pid=os.getpid())
    rc = mc._cmd_list_owned(args)
    assert rc == 0
    out = [os.path.realpath(p) for p in capsys.readouterr().out.splitlines() if p.strip()]
    assert os.path.realpath(str(sib_wt)) not in out
    assert _marker_fields(sib_wt)["session_id"] == "dead-subagent-sess"


# ---------------------------------------------------------------------------
# 4) Ledger orphan adoption reaches a dead session's PR whose worktree is
#    still PRESENT (it used to be skipped outright).
# ---------------------------------------------------------------------------

def test_adopt_orphan_prs_adopts_present_worktree_with_dead_owner(
    tmp_path, monkeypatch
):
    anchor = _make_repo(tmp_path / "anchor", slug="o/anchor")
    wt = _add_worktree(anchor, tmp_path / "anchor-wt-7", "b-7")
    _arm(wt, "dead-sess", 7, DEAD_PID)
    repo_slug = mc._repo_slug(str(anchor))
    sl.append("dead-sess", pr=7, branch="b-7", worktree=str(wt), repo=repo_slug)

    monkeypatch.setattr(_reconcile_mod, "_pr_open_state",
                        lambda pr, cwd: ("open", f"https://x/pull/{pr}", False, []))
    monkeypatch.setattr(github_api, "get_review_threads",
                        lambda pr, cwd=None: [_thread()])

    adopted = _reconcile_mod._adopt_orphan_prs("sess-X", str(anchor), os.getpid())

    assert [r["pr"] for r in adopted] == [7]
    assert adopted[0]["worktree_present"] is True
    assert adopted[0]["adopted_from"] == "dead-sess"
    # Marker re-stamped; ledger now carries this session's entry.
    assert _marker_fields(wt)["session_id"] == "sess-X"
    assert any(e.pr == 7 for e in sl.read("sess-X", repo=repo_slug))


def test_adopt_orphan_prs_skips_present_worktree_with_live_marker_owner(
    tmp_path, monkeypatch
):
    anchor = _make_repo(tmp_path / "anchor", slug="o/anchor")
    wt = _add_worktree(anchor, tmp_path / "anchor-wt-8", "b-8")
    _arm(wt, "other-live-sess", 8, os.getpid())  # marker pid ALIVE
    repo_slug = mc._repo_slug(str(anchor))
    sl.append("dead-sess", pr=8, branch="b-8", worktree=str(wt), repo=repo_slug)

    monkeypatch.setattr(_reconcile_mod, "_pr_open_state",
                        lambda pr, cwd: ("open", f"https://x/pull/{pr}", False, []))
    monkeypatch.setattr(github_api, "get_review_threads",
                        lambda pr, cwd=None: [_thread()])

    adopted = _reconcile_mod._adopt_orphan_prs("sess-X", str(anchor), os.getpid())

    assert adopted == []
    assert _marker_fields(wt)["session_id"] == "other-live-sess"


# ---------------------------------------------------------------------------
# 5) Durable-ledger ownership gates takeover (PR #82 review, P1): a dead
#    marker does not mean the PR is ownerless. A LIVE session that repointed
#    its worktree elsewhere leaves a dead marker behind while still owning the
#    same (repo, pr) in the durable ledger; overwriting that marker would make
#    _check_worktree treat the takeover as resolved self-ownership and skip its
#    _live_pr_owner_record fallback — concurrent maintenance against a live
#    owner. Both takeover paths must consult the durable resolver first.
# ---------------------------------------------------------------------------

def _liveness_only_for(monkeypatch, live_sid):
    from agentic_pr_dash._maintenance import markers as _markers_mod

    def fake(sid, cwd=None):
        return sid == live_sid

    monkeypatch.setattr(_markers_mod, "_session_is_live", fake)
    monkeypatch.setattr(_reconcile_mod, "_session_is_live", fake)


def test_dead_marker_takeover_defers_to_live_durable_ledger_owner(
    tmp_path, monkeypatch, capsys
):
    anchor, sib, sib_wt = _sibling_fixture(tmp_path, monkeypatch)
    # A LIVE session repointed its worktree away (dead marker here) but still
    # owns (o/sib, 79) in the durable ledger — takeover must defer to it.
    sl.append("live-owner-sess", pr=79, branch="bou-2155", worktree="", repo="o/sib")
    _liveness_only_for(monkeypatch, "live-owner-sess")

    args = argparse.Namespace(session_id="sess-X", cwd=str(anchor), pid=os.getpid())
    rc = mc._cmd_list_owned(args)
    assert rc == 0
    out = [os.path.realpath(p) for p in capsys.readouterr().out.splitlines() if p.strip()]
    assert os.path.realpath(str(sib_wt)) not in out
    # Marker NOT overwritten — the live durable owner keeps sole ownership.
    assert _marker_fields(sib_wt)["session_id"] == "dead-subagent-sess"


def test_dead_marker_takeover_proceeds_when_durable_owner_dead(
    tmp_path, monkeypatch, capsys
):
    anchor, sib, sib_wt = _sibling_fixture(tmp_path, monkeypatch)
    # A stale ledger row from a DEAD session must not block takeover.
    sl.append("stale-dead-owner", pr=79, branch="bou-2155", worktree="", repo="o/sib")

    args = argparse.Namespace(session_id="sess-X", cwd=str(anchor), pid=os.getpid())
    rc = mc._cmd_list_owned(args)
    assert rc == 0
    out = [os.path.realpath(p) for p in capsys.readouterr().out.splitlines() if p.strip()]
    assert os.path.realpath(str(sib_wt)) in out
    assert _marker_fields(sib_wt)["session_id"] == "sess-X"


def test_adopt_orphan_prs_present_worktree_defers_to_live_durable_ledger_owner(
    tmp_path, monkeypatch
):
    anchor = _make_repo(tmp_path / "anchor", slug="o/anchor")
    wt = _add_worktree(anchor, tmp_path / "anchor-wt-9", "b-9")
    _arm(wt, "dead-sess", 9, DEAD_PID)
    repo_slug = mc._repo_slug(str(anchor))
    sl.append("dead-sess", pr=9, branch="b-9", worktree=str(wt), repo=repo_slug)
    # The live owner repointed its worktree elsewhere but still owns (repo, 9).
    sl.append("live-owner-sess", pr=9, branch="b-9", worktree="", repo=repo_slug)
    _liveness_only_for(monkeypatch, "live-owner-sess")

    monkeypatch.setattr(_reconcile_mod, "_pr_open_state",
                        lambda pr, cwd: ("open", f"https://x/pull/{pr}", False, []))
    monkeypatch.setattr(github_api, "get_review_threads",
                        lambda pr, cwd=None: [_thread()])

    adopted = _reconcile_mod._adopt_orphan_prs("sess-X", str(anchor), os.getpid())

    assert adopted == []
    # Marker NOT re-stamped and no ledger entry claimed for this session.
    assert _marker_fields(wt)["session_id"] == "dead-sess"
    assert not any(e.pr == 9 for e in sl.read("sess-X", repo=repo_slug))
