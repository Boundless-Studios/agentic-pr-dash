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

import pytest

from agentic_pr_dash import session_ledger as sl
from agentic_pr_dash._maintenance import markers

REPO = "owner/name"


@pytest.fixture(autouse=True)
def _isolate_ledger(tmp_path, monkeypatch):
    # session_ledger._DEFAULT_DIR is frozen at import (expanduser("~") once), so
    # without an explicit dir every test shares one ledger. Point it at tmp so
    # each test's appends are isolated.
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger"))


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


def test_prefers_most_recent_owner_after_handoff(monkeypatch):
    """PR #61 review (P2): after a re-arm/handoff both sessions keep ledger rows;
    resolve the MOST RECENT owner (latest opened_at), not filesystem order."""
    from agentic_pr_dash.session_ledger import LedgerEntry, _write_all
    _write_all("sess-OLD", [LedgerEntry(pr=2401, branch="b", worktree="",
                                        opened_at="2026-07-01T00:00:00Z", repo=REPO)])
    _write_all("sess-NEW", [LedgerEntry(pr=2401, branch="b", worktree="",
                                        opened_at="2026-07-08T00:00:00Z", repo=REPO)])
    monkeypatch.setattr(markers, "_session_is_live", lambda sid, cwd=None: True)
    assert markers._live_pr_owner(2401, REPO, "me") == "sess-NEW"


def test_liveness_checked_in_entry_worktree_context(monkeypatch, tmp_path):
    """PR #61 review (P2): liveness is resolved in the ledger entry's OWN worktree
    (its repo may configure a per-worktree session_registry_path), not the
    checker's cwd."""
    wt_path = tmp_path / "owner-wt"
    wt_path.mkdir()
    wt = str(wt_path)
    sl.append("sess-LIVE", pr=2401, branch="b", worktree=wt, repo=REPO)
    seen = {}

    def _live(sid, cwd=None):
        seen[sid] = cwd
        return True

    monkeypatch.setattr(markers, "_session_is_live", _live)
    assert markers._live_pr_owner(2401, REPO, "me", cwd="/some/checker/cwd") == "sess-LIVE"
    assert seen["sess-LIVE"] == wt  # entry worktree, NOT the checker cwd


def test_liveness_falls_back_to_checker_cwd_when_entry_worktree_gone(monkeypatch, tmp_path):
    """PR #61 review (P2): detached ledger rows may point at removed worktrees;
    liveness must then use the checker cwd so repo-scoped registries still work."""
    gone = str(tmp_path / "gone-owner-wt")
    checker = str(tmp_path / "checker")
    sl.append("sess-LIVE", pr=2401, branch="b", worktree=gone, repo=REPO)
    seen = {}

    def _live(sid, cwd=None):
        seen[sid] = cwd
        return True

    monkeypatch.setattr(markers, "_session_is_live", _live)
    assert markers._live_pr_owner(2401, REPO, "me", cwd=checker) == "sess-LIVE"
    assert seen["sess-LIVE"] == checker


def test_empty_ledger_returns_none(monkeypatch):
    # No sessions in the ledger → no owner (also the common back-compat path:
    # the resolver must be a no-op when nothing is recorded).
    assert markers._live_pr_owner(2401, REPO, "me") is None


def test_legacy_repoless_row_does_not_match_other_repo(monkeypatch):
    """PR #61 review (P1): a live session's REPO-LESS legacy ledger row for PR #N
    must NOT resolve it as the owner of a DIFFERENT repo's PR #N — strict repo
    matching, else a same-number PR in another repo gets wrongly deferred."""
    sl.append("sess-LIVE", pr=2401, branch="b", worktree="")  # legacy: no repo
    monkeypatch.setattr(markers, "_session_is_live", lambda sid, cwd=None: True)
    # Querying a concrete repo must NOT match the repo-less legacy row.
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


def test_check_worktree_uses_ledger_owner_worktree_for_waiter(monkeypatch, tmp_path):
    """PR #61 review (P2): ledger owners may still have a legacy per-worktree
    waiter. Check waiter liveness in the owner entry's worktree, not the checker
    cwd, so a live owner is not mistaken for wake-less."""
    from agentic_pr_dash._maintenance import worktree_check as wc
    from agentic_pr_dash._maintenance import waiter

    checker = tmp_path / "checker"
    owner_wt = tmp_path / "owner"
    checker.mkdir()
    owner_wt.mkdir()

    class _PR:
        number = 2401
        is_draft = False

    seen: list[str] = []

    monkeypatch.setattr(markers, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(wc, "_resolve_and_blockers", lambda cwd: (_PR(), ["review_comments"]))
    monkeypatch.setattr(markers, "_marker_session_id", lambda cwd: None)
    monkeypatch.setattr(wc.worktrees, "_marker_pr", lambda cwd: "")
    monkeypatch.setattr(
        markers,
        "_live_pr_owner_record",
        lambda pr, repo, sid, cwd=None: ("sess-LIVE", str(owner_wt)),
    )

    def _await_alive(cwd, owner):
        seen.append(cwd)
        return True

    monkeypatch.setattr(waiter, "_await_alive", _await_alive)

    code, text = wc._check_worktree(str(checker), "me", claim=False)

    assert code == 0
    assert "sess-LIVE" in text
    assert seen == [str(owner_wt)]


def test_check_worktree_self_owned_marker_skips_ledger_gate(monkeypatch, tmp_path):
    """PR #61 review (P2): when THIS worktree's marker is self-owned, a stale
    previous-session ledger row must NOT make the current owner defer — the
    ledger gate is limited to the markerless/repointed-away case."""
    from agentic_pr_dash import github_api
    from agentic_pr_dash._maintenance import worktree_check as wc

    class _PR:
        number = 2401
        is_draft = False
        ci_watch_pending = False

    monkeypatch.setattr(markers, "_live_foreign_owner", lambda cwd, sid: None)
    # Clean PR (no blockers) so the service path is short — the point is only that
    # the ledger gate is bypassed, not the full prompt build.
    monkeypatch.setattr(wc, "_resolve_and_blockers", lambda cwd: (_PR(), []))
    # Current worktree marker is OURS...
    monkeypatch.setattr(markers, "_marker_session_id", lambda cwd: "me")
    monkeypatch.setattr(wc.worktrees, "_marker_pr", lambda cwd: str(_PR.number))

    # ...so a stale ledger row's owner must NOT even be consulted.
    def _boom(*a, **k):
        raise AssertionError("ledger gate must be skipped when marker is self-owned")

    monkeypatch.setattr(markers, "_live_pr_owner", _boom)
    monkeypatch.setattr(github_api, "required_checks_pending", lambda *a, **k: False)
    monkeypatch.setattr(wc.worktrees, "_live_independent_owner_paths", lambda paths, sid: set())
    monkeypatch.setattr(wc.markers, "_touch_owner_heartbeat", lambda *a, **k: None)

    code, text = wc._check_worktree(str(tmp_path), "me", claim=False)
    assert code == 0
    assert "nothing pending" in text
    assert "(ledger)" not in text


def test_check_worktree_stale_self_marker_still_checks_ledger(monkeypatch, tmp_path):
    """PR #61 review (P2): a self-owned marker only resolves ownership for the
    current PR when the marker's recorded PR matches the branch PR."""
    from agentic_pr_dash import github_api
    from agentic_pr_dash._maintenance import worktree_check as wc
    from agentic_pr_dash._maintenance import waiter

    class _PR:
        number = 2401
        is_draft = False
        ci_watch_pending = False

    monkeypatch.setattr(markers, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(wc, "_resolve_and_blockers", lambda cwd: (_PR(), []))
    monkeypatch.setattr(markers, "_marker_session_id", lambda cwd: "me")
    monkeypatch.setattr(wc.worktrees, "_marker_pr", lambda cwd: "999")
    monkeypatch.setattr(
        markers,
        "_live_pr_owner_record",
        lambda pr, repo, sid, cwd=None: ("sess-LIVE", str(tmp_path)),
    )
    monkeypatch.setattr(waiter, "_await_alive", lambda cwd, owner: True)
    monkeypatch.setattr(github_api, "required_checks_pending", lambda *a, **k: False)
    monkeypatch.setattr(wc.worktrees, "_live_independent_owner_paths", lambda paths, sid: set())
    monkeypatch.setattr(wc.markers, "_touch_owner_heartbeat", lambda *a, **k: None)

    code, text = wc._check_worktree(str(tmp_path), "me", claim=False)

    assert code == 0
    assert "sess-LIVE" in text
    assert "(ledger)" in text


def test_check_worktree_stale_foreign_marker_still_checks_ledger(monkeypatch, tmp_path):
    """PR #61 review (P2): a stale marker whose owner is no longer live must not
    suppress the durable ledger owner gate."""
    from agentic_pr_dash import github_api
    from agentic_pr_dash._maintenance import worktree_check as wc
    from agentic_pr_dash._maintenance import waiter

    class _PR:
        number = 2401
        is_draft = False
        ci_watch_pending = False

    monkeypatch.setattr(markers, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(wc, "_resolve_and_blockers", lambda cwd: (_PR(), []))
    monkeypatch.setattr(markers, "_marker_session_id", lambda cwd: "stale-other")
    monkeypatch.setattr(markers, "_live_pr_owner", lambda pr, repo, sid, cwd=None: "sess-LIVE")
    monkeypatch.setattr(waiter, "_await_alive", lambda cwd, owner: True)
    monkeypatch.setattr(github_api, "required_checks_pending", lambda *a, **k: False)
    monkeypatch.setattr(wc.worktrees, "_live_independent_owner_paths", lambda paths, sid: set())
    monkeypatch.setattr(wc.markers, "_touch_owner_heartbeat", lambda *a, **k: None)

    code, text = wc._check_worktree(str(tmp_path), "me", claim=False)
    assert code == 0
    assert "sess-LIVE" in text
    assert "(ledger)" in text
