"""BOU-2475: the loop is a fallback for unowned/idle PRs, not the default fixer.

The detached loop dispatches an executor that has none of the owning session's
context — the design intent, what was already tried and rejected, why a reviewer's
suggestion does not apply. That produced a confidently wrong change that merged:
while shipping BOU-2490, the loop rewrote a verified-true statement in CLAUDE.md
into its negation, because it described a package state that the session had
upgraded minutes earlier. Its own commit was stamped ``committer=apd-loop-executor``,
refuting the claim it had just written.

That is a context-availability failure, not a capability one — a better model does
not fix it. So takeover must be genuinely hard to reach:

* a live WAKE-CAPABLE owner keeps its PR until the no-progress streak AND a real
  elapsed duration are both satisfied;
* a live WAKE-LESS owner (no waiter, cannot be told to act) is still taken over,
  but after a wall-clock horizon rather than the previous ONE tick.

Both floors are wall-clock because ticks are a poor clock: ``--interval`` is
configurable and every dashboard-triggered ``--once`` run burns one too.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from agentic_pr_dash._maintenance import worktree_check as wc


@pytest.fixture
def cwd(tmp_path):
    return str(tmp_path)


class _Cfg:
    """Minimal config stand-in for the two horizons under test."""

    def __init__(self, live=1800, wakeless=600):
        self.live_owner_takeover_seconds = live
        self.wakeless_takeover_seconds = wakeless
        self.reclaim_no_progress_threshold = 3

    def state_dir_for(self, cwd):
        from pathlib import Path

        return Path(cwd)


# ── wake-less owner: a duration, not one tick ──────────────────────────────────

def test_wakeless_owner_is_not_taken_over_on_the_very_next_tick(cwd):
    """The old behaviour: one tick of grace, then seize. That is what we removed."""
    with patch.object(wc, "load_config", return_value=_Cfg(wakeless=600)):
        assert wc._wakeless_grace_exhausted(cwd, "sess-A") is False  # records start
        # Immediately again — previously this returned True and took the PR.
        assert wc._wakeless_grace_exhausted(cwd, "sess-A") is False


def test_wakeless_owner_is_taken_over_once_the_horizon_elapses(cwd):
    """Still bounded: a wake-less owner cannot be told to act, so never taking over
    would strand the PR with nobody servicing it (BOU-1879)."""
    with patch.object(wc, "load_config", return_value=_Cfg(wakeless=600)):
        assert wc._wakeless_grace_exhausted(cwd, "sess-A") is False
        with patch.object(wc.time, "time", return_value=time.time() + 601):
            assert wc._wakeless_grace_exhausted(cwd, "sess-A") is True


def test_wakeless_horizon_restarts_when_the_owner_changes(cwd):
    """A new owner gets its own full horizon, not the previous owner's remainder."""
    with patch.object(wc, "load_config", return_value=_Cfg(wakeless=600)):
        wc._wakeless_grace_exhausted(cwd, "sess-A")
        with patch.object(wc.time, "time", return_value=time.time() + 601):
            assert wc._wakeless_grace_exhausted(cwd, "sess-B") is False


def test_corrupt_wakeless_record_does_not_grant_instant_takeover(cwd):
    """A garbled or future timestamp must fail SAFE (keep deferring), not seize."""
    with patch.object(wc, "load_config", return_value=_Cfg(wakeless=600)):
        wc._write_wakeless_defer(cwd, {cwd: {"owner": "sess-A", "first_seen": "junk"}})
        assert wc._wakeless_grace_exhausted(cwd, "sess-A") is False
        wc._write_wakeless_defer(
            cwd, {cwd: {"owner": "sess-A", "first_seen": time.time() + 10_000}}
        )
        assert wc._wakeless_grace_exhausted(cwd, "sess-A") is False


# ── live wake-capable owner: streak AND elapsed time ───────────────────────────

def test_streak_alone_no_longer_reclaims_a_live_owner(cwd):
    """The core BOU-2475 change.

    Hitting the tick threshold used to be sufficient. With a configurable interval
    and dashboard `--once` runs burning ticks, three ticks can elapse in seconds —
    so a live session doing real work could lose its PR almost immediately.
    """
    now = time.time()
    with patch.object(wc, "load_config", return_value=_Cfg(live=1800)):
        for _ in range(5):  # well past reclaim_no_progress_threshold=3
            count = wc._record_no_progress_tick(
                cwd, owner="sess-A", pr_number=42, fingerprint="fp"
            )
        assert count >= 3, "precondition: the streak threshold is met"
        elapsed = wc._no_progress_elapsed(cwd, owner="sess-A", pr_number=42)
        assert elapsed is not None and elapsed < 1800, (
            "streak met but the time floor is not — must keep deferring"
        )


def test_elapsed_is_measured_from_the_streak_start_not_the_last_tick(cwd):
    """Each tick must not reset the clock, or the floor could never be reached."""
    start = time.time()
    with patch.object(wc, "load_config", return_value=_Cfg()):
        # Pin BOTH clocks so the assertion is exact rather than skew-tolerant.
        with patch.object(wc.time, "time", return_value=start):
            wc._record_no_progress_tick(
                cwd, owner="sess-A", pr_number=42, fingerprint="fp"
            )
        with patch.object(wc.time, "time", return_value=start + 1000):
            wc._record_no_progress_tick(
                cwd, owner="sess-A", pr_number=42, fingerprint="fp"
            )
            elapsed = wc._no_progress_elapsed(cwd, owner="sess-A", pr_number=42)
        assert elapsed == 1000, f"clock restarted on the second tick: {elapsed}"


def test_progress_resets_the_clock_as_well_as_the_count(cwd):
    """A changed blocker fingerprint means the owner IS progressing — full reset."""
    start = time.time()
    with patch.object(wc, "load_config", return_value=_Cfg()):
        wc._record_no_progress_tick(cwd, owner="sess-A", pr_number=42, fingerprint="fp1")
        with patch.object(wc.time, "time", return_value=start + 1000):
            wc._record_no_progress_tick(
                cwd, owner="sess-A", pr_number=42, fingerprint="fp2"
            )
            elapsed = wc._no_progress_elapsed(cwd, owner="sess-A", pr_number=42)
        assert elapsed < 10, f"progress must restart the clock, got {elapsed}"


def test_elapsed_is_none_for_a_record_written_before_this_change(cwd):
    """Upgrading mid-streak must degrade to the old tick-only rule, not defer forever."""
    wc._write_no_progress(
        cwd, {cwd: {"owner": "sess-A", "pr": 42, "fingerprint": "fp", "count": 9}}
    )
    assert wc._no_progress_elapsed(cwd, owner="sess-A", pr_number=42) is None


def test_elapsed_is_none_when_the_record_belongs_to_another_owner_or_pr(cwd):
    with patch.object(wc, "load_config", return_value=_Cfg()):
        wc._record_no_progress_tick(cwd, owner="sess-A", pr_number=42, fingerprint="fp")
    assert wc._no_progress_elapsed(cwd, owner="sess-B", pr_number=42) is None
    assert wc._no_progress_elapsed(cwd, owner="sess-A", pr_number=99) is None


# ── config floors ──────────────────────────────────────────────────────────────

def test_takeover_horizons_have_conservative_defaults():
    from agentic_pr_dash.config import Config

    assert Config.live_owner_takeover_seconds == 1800
    assert Config.wakeless_takeover_seconds == 600
    assert Config.live_owner_takeover_seconds > Config.wakeless_takeover_seconds, (
        "a wake-capable owner must be given MORE time than one that cannot be woken"
    )


def test_non_positive_horizon_falls_back_to_the_default(tmp_path, monkeypatch):
    """Zero or negative would disable the floor and restore the old seize-fast
    behaviour — clamp rather than honour it."""
    from agentic_pr_dash import config as config_mod

    (tmp_path / "agentic-pr-dash.toml").write_text(
        "[project]\nlive_owner_takeover_seconds = 0\nwakeless_takeover_seconds = -5\n"
    )
    monkeypatch.delenv("AGENTIC_PR_DASH_LIVE_OWNER_TAKEOVER_SECONDS", raising=False)
    monkeypatch.delenv("AGENTIC_PR_DASH_WAKELESS_TAKEOVER_SECONDS", raising=False)
    cfg = config_mod.load(str(tmp_path))
    assert cfg.live_owner_takeover_seconds == 1800
    assert cfg.wakeless_takeover_seconds == 600
