import json

from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash import session_ledger as sl
from agentic_pr_dash import session_registry, github_api
from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment


def _thread():
    c = ReviewThreadComment(database_id=1, path="f.py", line=1, body="fix",
                            author="rev", created_at="2026-01-01T00:00:00Z")
    return ReviewThread(node_id="t1", is_resolved=False, is_outdated=False, top=c)


def _setup_orphan(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("GAIA_PR_CLAIM_DIR", str(tmp_path / "claims"))
    # A DEAD session (sess-DEAD) armed PR 555; worktree later gone.
    sl.append("sess-DEAD", pr=555, branch="bou-orphan", worktree=str(tmp_path / "gone"))
    monkeypatch.setattr(mc, "_iter_worktree_paths", lambda cwd: iter([]))
    monkeypatch.setattr(mc, "_collect_owned_worktrees", lambda sid, cwd, pid: [])
    monkeypatch.setattr(mc, "_pr_open_state",
                        lambda pr, cwd: ("open", "https://x/pull/555", False, []))
    monkeypatch.setattr(github_api, "get_review_threads", lambda pr, cwd=None: [_thread()])
    # sess-DEAD is not live; everything else is.
    monkeypatch.setattr(mc, "_session_is_live", lambda sid, cwd=None: sid != "sess-DEAD")


def test_running_session_claims_orphan(tmp_path, monkeypatch, capsys):
    _setup_orphan(tmp_path, monkeypatch)
    rc = mc.main(["reconcile-prs", "--session-id", "sess-LIVE", "--cwd",
                  str(tmp_path), "--adopt-orphans"])
    assert rc == 0
    records = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert any(r["pr"] == 555 for r in records)          # surfaced to the live session
    assert 555 in {e.pr for e in sl.read("sess-LIVE")}   # adopted into its ledger


def test_orphan_not_claimed_when_owner_still_live(tmp_path, monkeypatch, capsys):
    _setup_orphan(tmp_path, monkeypatch)
    monkeypatch.setattr(mc, "_session_is_live", lambda sid, cwd=None: True)  # owner alive
    mc.main(["reconcile-prs", "--session-id", "sess-LIVE", "--cwd",
             str(tmp_path), "--adopt-orphans"])
    assert 555 not in {e.pr for e in sl.read("sess-LIVE")}  # not stolen from a live owner


def test_second_live_claimant_is_refused(tmp_path, monkeypatch):
    _setup_orphan(tmp_path, monkeypatch)
    # First claimant wins.
    assert mc._claim_pr(555, "sess-LIVE-1", pid=111) is True
    # Second live claimant (different session, live holder) is refused.
    monkeypatch.setattr(session_registry, "pid_is_live", lambda pid: True)
    assert mc._claim_pr(555, "sess-LIVE-2", pid=222) is False


def test_claim_taken_over_when_holder_pid_dead(tmp_path, monkeypatch):
    _setup_orphan(tmp_path, monkeypatch)
    assert mc._claim_pr(555, "sess-LIVE-1", pid=111) is True
    monkeypatch.setattr(session_registry, "pid_is_live", lambda pid: False)  # holder died
    assert mc._claim_pr(555, "sess-LIVE-2", pid=222) is True  # taken over
