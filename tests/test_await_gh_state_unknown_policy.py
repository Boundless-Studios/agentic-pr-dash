"""P1 regression from PR #119 review: `gh_state_unknown` needs a per-consumer
policy, not one shared boolean.

``reconcile._unknown_gh_state_record`` marks a detached-PR record whose `gh`
probe failed entirely with ``gh_state_unknown: True`` (and forces ``p1: True``,
every blocker field falsy) so it can never be mistaken for a verified-clean PR.
That FACT — "we could not determine this PR's state" — is correct and must not
change. What was wrong is that the shared ``_record_has_blockers`` predicate
baked in ONE interpretation of it for BOTH of its callers:

* The stop gate (``_maintenance/stop_gate.py``) must fail CLOSED: an
  unresolvable probe blocks exactly like a real blocker, so a session never
  idles on a PR it cannot verify. This is the merged PR #119 behavior and must
  stay green (``tests/test_reconcile_prs.py`` pins it).
* The `await` waiter (``maintenance_check._cmd_await``) got the SAME "counts as
  a blocker" answer, which made ``_record_has_blockers`` return True for an
  unknown-state record. That record was added to ``pending``, and the waiter
  immediately ``return 10``'d with "Feedback arrived on PR(s) you own —
  address it now" — instructing the user to push a fix for a condition that
  was never actually observed, instead of recovering on a later tick once `gh`
  responds again (which the waiter's own ``unknown_detached`` guard, several
  lines below, already exists to do — this bug reached ``pending`` and
  returned before that guard was ever consulted).

Fix: ``_record_has_blockers`` takes a required ``unknown_state_blocks``
keyword so neither call site can silently inherit a policy — stop gate passes
``True``, the waiter passes ``False``.
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


def _wire_await(tmp_path, monkeypatch, wt):
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees",
                        lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(mc, "_owned_open_pr_pairs", lambda owned: [(w, 42) for w in owned])
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


# ── 2. The waiter must not exit 10 on an unresolvable gh probe alone ────────


def test_await_does_not_treat_unresolvable_gh_state_as_feedback(tmp_path, monkeypatch):
    """RED (pre-fix): the waiter used to `return 10` ("Feedback arrived") the
    very first tick a detached record's gh probe failed entirely, even though
    nothing was actually observed. GREEN: it keeps polling; once the record
    resolves cleanly on a later tick, it clean-exits (0) exactly like the
    pre-existing plain state=="unknown" case this mirrors."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    _wire_await(tmp_path, monkeypatch, wt)

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
    """Direction 2 (regression guard): a REAL blocker on a detached PR (not a
    gh outage) must still wake the waiter with 'Feedback arrived' immediately —
    this policy split must never suppress genuine feedback."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    _wire_await(tmp_path, monkeypatch, wt)

    monkeypatch.setattr(mc, "_check_worktree", lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [_blocked_record()],
    )

    rc = mc.main(_await_args(tmp_path))

    assert rc == 10, "a genuinely blocked detached PR must still wake the waiter"
