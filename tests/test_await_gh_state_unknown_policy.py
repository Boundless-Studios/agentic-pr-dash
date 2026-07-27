"""P1 regression from PR #119 review, then a second P1 from PR #121 review:
`gh_state_unknown` needs a per-consumer policy, not one shared boolean — and
the waiter's outcome space for a detached PR is THREE states, not two.

``reconcile._unknown_gh_state_record`` marks a detached-PR record whose `gh`
probe failed entirely with ``gh_state_unknown: True`` (and forces ``p1: True``,
every blocker field falsy) so it can never be mistaken for a verified-clean PR.
That FACT — "we could not determine this PR's state" — is correct and must not
change. Two collapses of it into a falsy default have shipped on this branch
already, each fixed by giving the SAME fact a different, explicit outcome per
consumer instead of reusing one boolean:

1. (First cut.) The shared ``_record_has_blockers`` predicate baked in ONE
   interpretation of ``gh_state_unknown`` for BOTH of its callers — the stop
   gate (which must fail CLOSED: an unresolvable probe blocks like a real one)
   and the `await` waiter (which must NOT treat it as actionable feedback).
   Fixed by giving the predicate a required ``unknown_state_blocks`` keyword.

2. (This fix.) Turning OFF "unknown blocks" for the waiter was necessary but
   not sufficient: the waiter's own ``has_open_detached`` — used only to
   decide "is there ANYTHING at all to watch" — also excluded ``state ==
   "unknown"`` records, a exclusion that belonged to the *different* "is this
   confirmed clean" question. So a session that owns ONLY a detached ledger PR
   whose probe returns "unknown" (no owned worktree at all) hit `not owned and
   not has_open_detached` and exited `_unbound_exit` (rc 4, "watched NOTHING")
   immediately — never reaching the `unknown_detached` guard a few lines below
   that exists specifically to keep the waiter alive through exactly this
   case. The waiter's real outcome space for a detached record is THREE
   states, and each needs its own encoding end-to-end — never sharing one with
   another:

   * actionable feedback → wake (`return 10`)
   * unknown / could-not-determine → keep polling (fall through to the
     existing `unknown_detached` guard)
   * genuinely nothing owned → `_unbound_exit` (rc 4)

   Fixed by renaming the "anything to watch" check to `has_any_detached =
   bool(_detached_this_tick)` — every survivor of `_detached_pr_records`'
   merged/closed/draft pruning is either "open" or "unknown", and BOTH
   represent real, still-tracked work, so BOTH must count as "something to
   watch" for THIS question.

The tests below exercise all three states explicitly, in both the
owned-worktree and detached-only shapes, so a two-state suite (which is what
let both collapses through) cannot pass again.
"""
from __future__ import annotations

import os

import pytest

from agentic_pr_dash import config, github_api
from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import stop_gate as _stop_gate_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod

SID = "sess-gh-unknown-policy"

# `_unbound_exit` returns this loud "watched nothing" sentinel (BOU-2294) — the
# outcome a detached-only unknown record must NOT produce.
_AWAIT_UNBOUND = 4


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger-unused"))
    monkeypatch.setenv("AGENTIC_PR_DASH_WAITER_DIR", str(tmp_path / "waiters"))
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(tmp_path / "empty-daemons"))
    config.load.cache_clear()
    github_api.reset_rate_limit_seen()
    github_api.reset_checks_probe_failure_seen()
    yield
    config.load.cache_clear()
    github_api.reset_rate_limit_seen()
    github_api.reset_checks_probe_failure_seen()


def _unknown_state_record(pr: int = 9, repo: str = "org-a/repo-a") -> dict:
    """The exact shape `reconcile._unknown_gh_state_record` produces."""
    return {
        "pr": pr, "url": f"(pr {pr})", "branch": "b", "repo": repo,
        "worktree_present": False, "unresolved_threads": 0,
        "ci_failing": False, "failing_checks": [], "ci_watch_pending": False,
        "changes_requested": False, "review_decision": "",
        "merge_conflict": False, "merge_state": "", "mergeable": "",
        "p1": True, "state": "unknown", "gh_state_unknown": True,
    }


def _clean_open_record(pr: int = 9, repo: str = "org-a/repo-a") -> dict:
    """The SAME PR as `_unknown_state_record`, once its gh probe resolves
    cleanly (confirmed open, no blockers) — NOT the record disappearing. A
    detached record vanishing from the ledger and a detached record RESOLVING
    are different events; only the latter simulates "the outage ended"."""
    return {
        "pr": pr, "url": f"https://example/{repo}/pull/{pr}", "branch": "b",
        "repo": repo, "worktree_present": False, "unresolved_threads": 0,
        "ci_failing": False, "failing_checks": [], "ci_watch_pending": False,
        "changes_requested": False, "review_decision": "",
        "merge_conflict": False, "merge_state": "CLEAN", "mergeable": "MERGEABLE",
        "p1": False, "state": "open",
    }


def _blocked_record(pr: int = 10, repo: str = "org-a/repo-a") -> dict:
    """A GENUINELY blocked detached PR — real, observed feedback."""
    return {
        "pr": pr, "url": f"(pr {pr})", "branch": "b", "repo": repo,
        "worktree_present": False, "unresolved_threads": 1,
        "ci_failing": False, "failing_checks": [], "ci_watch_pending": False,
        "changes_requested": False, "review_decision": "",
        "merge_conflict": False, "merge_state": "", "mergeable": "",
        "p1": False, "state": "open",
    }


def _await_args(tmp_path):
    return [
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", str(os.getpid()),
        "--max-wait=-1",
        "--interval", "0",
    ]


def _wire_await_with_owned_worktree(tmp_path, monkeypatch, wt):
    """The session owns a real worktree — the shape every existing test in
    this area used, and the one that hid the `has_open_detached` P1 (the
    non-empty `owned` list masks `not owned` before it ever matters)."""
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees",
                        lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(mc, "_owned_open_pr_pairs", lambda owned: [(w, 42) for w in owned])
    monkeypatch.setattr(mc, "_marker_pr_still_current", lambda wt, n: True)
    monkeypatch.setattr("time.sleep", lambda s: None)


def _wire_await_detached_only(tmp_path, monkeypatch):
    """The PRODUCTION-relevant case the first cut's tests missed entirely: the
    session owns NO worktree at all — every PR it tracks is detached (ledger-
    only, worktree torn down). `owned` is empty for the whole run."""
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees",
                        lambda sid, cwd: [])
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(mc, "_owned_open_pr_pairs", lambda owned: [])
    monkeypatch.setattr(mc, "_marker_pr_still_current", lambda wt, n: True)
    monkeypatch.setattr("time.sleep", lambda s: None)


# ── 1. _record_has_blockers: same fact, per-caller policy ───────────────────


def test_record_has_blockers_policy_is_explicit_per_caller():
    unknown = _unknown_state_record()
    blocked = _blocked_record()

    # Stop gate: an unresolvable probe blocks (P1, must not regress).
    assert _stop_gate_mod._record_has_blockers(unknown, unknown_state_blocks=True) is True
    # Waiter: an unresolvable probe alone is NOT actionable feedback.
    assert _stop_gate_mod._record_has_blockers(unknown, unknown_state_blocks=False) is False
    # A GENUINE blocker is a blocker under EITHER policy — never suppressed.
    assert _stop_gate_mod._record_has_blockers(blocked, unknown_state_blocks=True) is True
    assert _stop_gate_mod._record_has_blockers(blocked, unknown_state_blocks=False) is True


# ── 2. Three explicit states, owned-worktree shape ───────────────────────────


def test_await_does_not_treat_unresolvable_gh_state_as_feedback(tmp_path, monkeypatch):
    """State 2 (unknown): the waiter used to `return 10` ("Feedback arrived")
    the very first tick a detached record's gh probe failed entirely, even
    though nothing was actually observed. It must keep polling instead; once
    the record resolves cleanly on a later tick, it clean-exits (0)."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    _wire_await_with_owned_worktree(tmp_path, monkeypatch, wt)

    tick = [0]

    def fake_check(path, sid, *, claim=True):
        tick[0] += 1
        if tick[0] > 3:
            raise AssertionError("await did not exit once the record resolved")
        return 0, "nothing pending"

    def fake_detached(sid, cwd, include_legacy=True, prune_legacy=True):
        return [_unknown_state_record()] if tick[0] <= 1 else []

    monkeypatch.setattr(mc, "_check_worktree", fake_check)
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", fake_detached)

    rc = mc.main(_await_args(tmp_path))

    assert rc == 0, (
        "an unresolvable gh probe must not read as 'Feedback arrived' (rc 10); "
        "it must keep polling and recover once the probe succeeds"
    )
    assert tick[0] == 2, "the waiter must poll again rather than exit on tick 1"


def test_await_still_wakes_on_a_genuine_detached_blocker(tmp_path, monkeypatch):
    """State 1 (actionable feedback), owned-worktree shape: a REAL blocker on
    a detached PR (not a gh outage) must still wake the waiter with 'Feedback
    arrived' immediately — the policy split must never suppress genuine
    feedback."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    _wire_await_with_owned_worktree(tmp_path, monkeypatch, wt)

    monkeypatch.setattr(mc, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [_blocked_record()],
    )

    rc = mc.main(_await_args(tmp_path))

    assert rc == 10, "a genuinely blocked detached PR must still wake the waiter"


# ── 3. Three explicit states, DETACHED-ONLY shape (no owned worktree) ───────
#
# This is the production-relevant case the first cut's tests missed — every
# test above supplies an owned worktree, which keeps `owned` non-empty and
# masks the `not owned and not has_*_detached` exit path entirely.


def test_await_keeps_polling_a_detached_only_unknown_pr(tmp_path, monkeypatch):
    """State 2 (unknown), detached-only shape — the P1 this fix closes. With
    NO owned worktree and the session's only tracked PR reporting
    gh_state_unknown, the waiter must stay alive and keep polling (reaching
    the `unknown_detached` guard), NOT exit `_unbound_exit` claiming it
    watched nothing. Tick 2: the SAME PR's probe resolves to a confirmed-open,
    fully clean record (not the record vanishing from the ledger, which would
    just be a second instance of "genuinely nothing owned") — the waiter then
    clean-exits."""
    _wire_await_detached_only(tmp_path, monkeypatch)

    tick = [0]

    def fake_detached(sid, cwd, include_legacy=True, prune_legacy=True):
        tick[0] += 1
        if tick[0] > 3:
            raise AssertionError("await did not exit once the record resolved")
        return [_unknown_state_record()] if tick[0] <= 1 else [_clean_open_record()]

    monkeypatch.setattr(mc, "_check_worktree",
                        lambda path, sid, *, claim=True: (_ for _ in ()).throw(
                            AssertionError("no owned worktree — _check_worktree must not be called")))
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", fake_detached)

    rc = mc.main(_await_args(tmp_path))

    assert rc != _AWAIT_UNBOUND, (
        "a detached-only unknown record must not read as 'watched nothing' — "
        "it is something to watch, just not resolvable this tick"
    )
    assert rc == 0, "once the record resolves clean, the waiter clean-exits"
    assert tick[0] == 2, "the waiter must poll again rather than exit on tick 1"


def test_await_wakes_on_a_detached_only_genuine_blocker(tmp_path, monkeypatch):
    """State 1 (actionable feedback), detached-only shape (regression guard):
    with no owned worktree, a REAL blocker on the session's only tracked
    detached PR must still wake the waiter immediately."""
    _wire_await_detached_only(tmp_path, monkeypatch)

    monkeypatch.setattr(mc, "_check_worktree",
                        lambda path, sid, *, claim=True: (_ for _ in ()).throw(
                            AssertionError("no owned worktree — _check_worktree must not be called")))
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [_blocked_record()],
    )

    rc = mc.main(_await_args(tmp_path))

    assert rc == 10, "a genuinely blocked detached-only PR must still wake the waiter"


def test_await_reports_unbound_when_genuinely_nothing_is_owned(tmp_path, monkeypatch):
    """State 3 (genuinely nothing), detached-only shape (regression guard):
    no owned worktree AND no detached record of any kind — the waiter must
    still loudly report `_unbound_exit` rather than silently idling. This is
    the exact case `has_any_detached` must keep firing for; only "unknown"
    must stop being conflated with "nothing"."""
    _wire_await_detached_only(tmp_path, monkeypatch)

    monkeypatch.setattr(mc, "_check_worktree",
                        lambda path, sid, *, claim=True: (_ for _ in ()).throw(
                            AssertionError("no owned worktree — _check_worktree must not be called")))
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )

    rc = mc.main(_await_args(tmp_path))

    assert rc == _AWAIT_UNBOUND, (
        "with nothing owned and nothing detached, the waiter must report "
        "unbound, not silently exit clean or hang"
    )
