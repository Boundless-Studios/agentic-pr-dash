"""BOU-1924: resolve a PR's live owner from the durable session ledger +
registry, independent of which worktree (if any) has the branch checked out.

The per-worktree marker gate (``_live_foreign_owner``) only sees the marker at
the queried cwd. A session that works several PRs out of ONE repointed worktree
has a marker for only its *current* branch's PR; its other owned PRs have no
marker anywhere, so marker-only resolution can't attribute them to the live
session. ``_live_pr_owner`` closes that gap by resolving ownership from the
worktree-independent session ledger + the session registry's liveness.
"""
from __future__ import annotations

from agentic_pr_dash import session_ledger as sl
from agentic_pr_dash._maintenance import markers

REPO = "owner/name"


def test_resolves_live_session_via_ledger(monkeypatch):
    sl.append("sess-LIVE", pr=2401, branch="b", worktree="", repo=REPO)
    monkeypatch.setattr(markers, "_session_is_live", lambda sid, cwd=None: sid == "sess-LIVE")
    assert markers._live_pr_owner(2401, REPO, "me") == "sess-LIVE"


def test_none_when_owner_dead(monkeypatch):
    sl.append("sess-DEAD", pr=2402, branch="b", worktree="", repo=REPO)
    monkeypatch.setattr(markers, "_session_is_live", lambda sid, cwd=None: False)
    assert markers._live_pr_owner(2402, REPO, "me") is None


def test_ignores_self(monkeypatch):
    sl.append("me", pr=2403, branch="b", worktree="", repo=REPO)
    monkeypatch.setattr(markers, "_session_is_live", lambda sid, cwd=None: True)
    assert markers._live_pr_owner(2403, REPO, "me") is None


def test_none_when_pr_not_owned(monkeypatch):
    sl.append("sess-LIVE", pr=1, branch="b", worktree="", repo=REPO)
    monkeypatch.setattr(markers, "_session_is_live", lambda sid, cwd=None: True)
    assert markers._live_pr_owner(999, REPO, "me") is None


def test_empty_ledger_returns_none(monkeypatch):
    # No sessions in the ledger → no owner (also the common back-compat path:
    # the resolver must be a no-op when nothing is recorded).
    assert markers._live_pr_owner(2401, REPO, "me") is None


def test_check_worktree_defers_to_live_ledger_owner(monkeypatch, tmp_path):
    """A non-draft PR owned (per the ledger) by a LIVE, wake-capable OTHER
    session is deferred to — not serviced — even with NO marker at this cwd."""
    from agentic_pr_dash._maintenance import worktree_check as wc
    from agentic_pr_dash._maintenance import waiter

    class _PR:
        number = 2401
        is_draft = False

    # No per-worktree marker owner here (the repointed-away case).
    monkeypatch.setattr(markers, "_live_foreign_owner", lambda cwd, sid: None)
    # Resolve a real, blocked, non-draft PR for this worktree.
    monkeypatch.setattr(wc, "_resolve_and_blockers", lambda cwd: (_PR(), ["review_comments"]))
    # The ledger says a live OTHER session owns it...
    monkeypatch.setattr(markers, "_live_pr_owner", lambda pr, repo, sid, cwd=None: "sess-LIVE")
    # ...and that owner has a live waiter (wake-capable) → defer, don't take over.
    monkeypatch.setattr(waiter, "_await_alive", lambda cwd, owner: True)

    code, text = wc._check_worktree(str(tmp_path), "me", claim=False)
    assert code == 0
    assert "sess-LIVE" in text
    # The blocked-owned-PR invariant: a defer must still name the PR is blocked.
    assert wc.WARN_ONLY_MARKER in text
