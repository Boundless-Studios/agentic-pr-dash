"""Shared short-TTL PR-list snapshot cache (BOU-1923 Bucket 2 / BOU-1953).

The stop-gate, the detached loop, and every in-session ``await`` waiter each
resolve "my open PRs" on their own cadence, all via the same underlying
``gh pr list --author @me --state open`` call
(:func:`github_api.list_open_prs`). :func:`github_api.list_open_prs_cached`
shares ONE snapshot (a JSON file under the worktree's ``state_dir``) across
those callers within a short TTL so a burst of calls collapses to a single
real fetch instead of one per caller.
"""
from __future__ import annotations

import json

from agentic_pr_dash import github_api
from agentic_pr_dash._maintenance import pr_state


def _fake_prs(n: int = 1) -> list[dict]:
    return [{"number": i, "headRefName": f"branch-{i}"} for i in range(n)]


# --------------------------------------------------------------------------- #
# cache HIT: an unexpired snapshot is returned WITHOUT calling gh
# --------------------------------------------------------------------------- #

def test_cache_hit_avoids_gh_call(tmp_path, monkeypatch):
    calls = {"n": 0}

    def _boom(cwd=None):
        calls["n"] += 1
        raise AssertionError("list_open_prs must not be called on a warm cache")

    monkeypatch.setattr(github_api, "list_open_prs", _boom)

    cached = _fake_prs(2)
    path = github_api._pr_snapshot_path(str(tmp_path))
    github_api._write_pr_snapshot(path, cached, "@me")

    result = github_api.list_open_prs_cached(str(tmp_path))

    assert result == cached
    assert calls["n"] == 0


def test_cache_hit_within_custom_ttl(tmp_path, monkeypatch):
    """A snapshot fetched moments ago is still warm under a longer explicit ttl_s."""
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: (_ for _ in ()).throw(
        AssertionError("must not refetch")
    ))
    cached = _fake_prs(1)
    path = github_api._pr_snapshot_path(str(tmp_path))
    github_api._write_pr_snapshot(path, cached, "@me")

    result = github_api.list_open_prs_cached(str(tmp_path), ttl_s=3600)

    assert result == cached


# --------------------------------------------------------------------------- #
# cache MISS / expiry: refetches for real and (re)writes the snapshot
# --------------------------------------------------------------------------- #

def test_cache_miss_no_snapshot_refetches_and_writes(tmp_path, monkeypatch):
    fresh = _fake_prs(3)
    calls = {"n": 0}

    def _fake_list(cwd=None):
        calls["n"] += 1
        return fresh

    monkeypatch.setattr(github_api, "list_open_prs", _fake_list)

    result = github_api.list_open_prs_cached(str(tmp_path))

    assert result == fresh
    assert calls["n"] == 1

    path = github_api._pr_snapshot_path(str(tmp_path))
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["prs"] == fresh
    assert isinstance(on_disk["fetched_at"], (int, float))

    # A second call within the TTL is now a HIT — no further gh call.
    result2 = github_api.list_open_prs_cached(str(tmp_path))
    assert result2 == fresh
    assert calls["n"] == 1


def test_expired_snapshot_refetches(tmp_path, monkeypatch):
    stale = _fake_prs(1)
    path = github_api._pr_snapshot_path(str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"fetched_at": 0.0, "prs": stale}), encoding="utf-8"
    )

    fresh = _fake_prs(5)
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: fresh)

    result = github_api.list_open_prs_cached(str(tmp_path), ttl_s=45)

    assert result == fresh
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["prs"] == fresh


def test_corrupt_snapshot_treated_as_miss(tmp_path, monkeypatch):
    """A torn/partial concurrent write must not crash the reader — fall back
    to a fresh fetch instead of raising."""
    path = github_api._pr_snapshot_path(str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")

    fresh = _fake_prs(2)
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: fresh)

    result = github_api.list_open_prs_cached(str(tmp_path))

    assert result == fresh


# --------------------------------------------------------------------------- #
# failure must never poison the cache (None-vs-[] invariant preserved)
# --------------------------------------------------------------------------- #

def test_failed_fetch_returns_none_and_does_not_write_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: None)

    result = github_api.list_open_prs_cached(str(tmp_path))

    assert result is None
    path = github_api._pr_snapshot_path(str(tmp_path))
    assert not path.exists()


def test_failed_forced_refetch_leaves_prior_snapshot_untouched(tmp_path, monkeypatch):
    """force=True bypasses the cache to refetch, but a failed refetch must not
    clobber (or otherwise corrupt) whatever snapshot was already on disk."""
    prior = _fake_prs(1)
    path = github_api._pr_snapshot_path(str(tmp_path))
    github_api._write_pr_snapshot(path, prior, "@me")

    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: None)

    result = github_api.list_open_prs_cached(str(tmp_path), force=True)

    assert result is None
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["prs"] == prior  # untouched by the failed forced refetch


# --------------------------------------------------------------------------- #
# force=True bypasses a warm cache
# --------------------------------------------------------------------------- #

def test_force_bypasses_warm_cache(tmp_path, monkeypatch):
    old = _fake_prs(1)
    path = github_api._pr_snapshot_path(str(tmp_path))
    github_api._write_pr_snapshot(path, old, "@me")

    new = _fake_prs(9)
    calls = {"n": 0}

    def _fake_list(cwd=None):
        calls["n"] += 1
        return new

    monkeypatch.setattr(github_api, "list_open_prs", _fake_list)

    result = github_api.list_open_prs_cached(str(tmp_path), force=True)

    assert result == new
    assert calls["n"] == 1
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["prs"] == new


# --------------------------------------------------------------------------- #
# thundering-herd: concurrent misses re-check under the lock before fetching
# (BOU-1923 review #2)
# --------------------------------------------------------------------------- #

def test_concurrent_miss_rechecks_under_lock_and_skips_fetch(tmp_path, monkeypatch):
    """A cold/expired-cache burst must not all fire `list_open_prs`. The lock
    serializes the refresh; a waiter that gets in AFTER the winner wrote finds
    the fresh snapshot on the re-check and skips its own fetch."""
    fresh = _fake_prs(2)
    calls = {"n": 0}

    def _list(cwd=None):
        calls["n"] += 1
        return fresh

    monkeypatch.setattr(github_api, "list_open_prs", _list)

    # Simulate: the pre-lock read misses (cold), but by the time we hold the
    # lock a sibling (the herd winner) has written a fresh snapshot, so the
    # under-lock re-check HITS.
    reads = {"n": 0}

    def _read(p, ttl, author):
        reads["n"] += 1
        return None if reads["n"] == 1 else fresh

    monkeypatch.setattr(github_api, "_read_pr_snapshot", _read)

    result = github_api.list_open_prs_cached(str(tmp_path))

    assert result == fresh
    assert calls["n"] == 0  # re-check under the lock avoided the redundant fetch
    assert reads["n"] == 2  # pre-lock miss + under-lock re-check


def test_cold_miss_both_reads_miss_fetches_exactly_once(tmp_path, monkeypatch):
    """When the re-check under the lock ALSO misses (a genuine cold cache), the
    fetch fires exactly once and the snapshot is written."""
    fresh = _fake_prs(3)
    calls = {"n": 0}

    def _list(cwd=None):
        calls["n"] += 1
        return fresh

    monkeypatch.setattr(github_api, "list_open_prs", _list)

    result = github_api.list_open_prs_cached(str(tmp_path))

    assert result == fresh
    assert calls["n"] == 1
    path = github_api._pr_snapshot_path(str(tmp_path))
    assert json.loads(path.read_text(encoding="utf-8"))["prs"] == fresh


def test_snapshot_lock_acquire_and_release_roundtrip(tmp_path):
    """The lock helper acquires an fd and a second acquire while held times out
    to None (bounded), then succeeds once released."""
    monkey_path = github_api._pr_snapshot_path(str(tmp_path))
    import agentic_pr_dash.github_api as ga
    if ga.fcntl is None:  # pragma: no cover - non-POSIX
        return
    orig = ga._PR_SNAPSHOT_LOCK_WAIT_S
    ga._PR_SNAPSHOT_LOCK_WAIT_S = 0.2
    try:
        fd = github_api._acquire_snapshot_lock(monkey_path)
        assert fd is not None
        # A second acquire while the first is held cannot get it → bounded None.
        assert github_api._acquire_snapshot_lock(monkey_path) is None
        github_api._release_snapshot_lock(fd)
        # After release it is available again.
        fd2 = github_api._acquire_snapshot_lock(monkey_path)
        assert fd2 is not None
        github_api._release_snapshot_lock(fd2)
    finally:
        ga._PR_SNAPSHOT_LOCK_WAIT_S = orig


# --------------------------------------------------------------------------- #
# cache-hit must NOT mask gh-unavailability during the detail fetch
# (BOU-1923 review #1)
# --------------------------------------------------------------------------- #

def _warm_snapshot(tmp_path, branch: str, number: int) -> None:
    snap = [{
        "number": number, "headRefName": branch, "title": "t", "url": "http://x",
        "baseRefName": "main", "isDraft": False,
        "mergeStateStatus": "CLEAN", "mergeable": "MERGEABLE",
    }]
    github_api._write_pr_snapshot(github_api._pr_snapshot_path(str(tmp_path)), snap, "@me")


def test_warm_cache_plus_gh_unavailable_detail_is_unavailable(tmp_path, monkeypatch):
    """A warm snapshot skips `list_open_prs`, so an outage that hits the DETAIL
    calls (which fail open to empty) must still surface as _GH_UNAVAILABLE — not
    a false 'clean' PR (the None-vs-[] invariant, BOU-1923 review)."""
    branch = "feature-x"
    _warm_snapshot(tmp_path, branch, 7)

    monkeypatch.setattr(pr_state, "_current_branch", lambda cwd: branch)
    # A warm cache means list_open_prs must NOT be consulted at all.
    monkeypatch.setattr(
        github_api, "list_open_prs",
        lambda cwd=None: (_ for _ in ()).throw(AssertionError("warm cache: no list call")),
    )

    github_api.reset_rate_limit_seen()
    monkeypatch.setattr(github_api, "get_latest_commit", lambda n, cwd=None: ("", ""))

    def _ci(n, cwd=None):
        # Simulate a rate-limit hit DURING the detail fetch: the getter fails
        # open to [] but `_run` records the event — it sets the per-tick flag
        # AND bumps the monotonic event counter (the guard reads the counter
        # since BOU-1966, so it also fires when the flag was already set).
        github_api._RATE_LIMIT_SEEN = True
        github_api._RATE_LIMIT_EVENTS = github_api._RATE_LIMIT_EVENTS + 1
        return []

    monkeypatch.setattr(github_api, "get_ci_checks", _ci)
    monkeypatch.setattr(github_api, "get_unaddressed_comments", lambda n, d, cwd=None: [])

    result = pr_state._resolve_pr_for_branch(str(tmp_path))

    assert result is pr_state._GH_UNAVAILABLE


def test_warm_cache_healthy_detail_still_resolves_pr(tmp_path, monkeypatch):
    """Contrast: a warm cache with HEALTHY detail calls (no rate-limit) resolves
    a normal PRData — the availability guard must not misfire on the happy path."""
    branch = "feature-y"
    _warm_snapshot(tmp_path, branch, 11)

    monkeypatch.setattr(pr_state, "_current_branch", lambda cwd: branch)
    monkeypatch.setattr(
        github_api, "list_open_prs",
        lambda cwd=None: (_ for _ in ()).throw(AssertionError("warm cache: no list call")),
    )
    github_api.reset_rate_limit_seen()
    monkeypatch.setattr(github_api, "get_latest_commit", lambda n, cwd=None: ("abc", "2026-01-01T00:00:00Z"))
    monkeypatch.setattr(github_api, "get_ci_checks", lambda n, cwd=None: [])
    monkeypatch.setattr(github_api, "get_unaddressed_comments", lambda n, d, cwd=None: [])

    result = pr_state._resolve_pr_for_branch(str(tmp_path))

    assert result is not pr_state._GH_UNAVAILABLE
    assert result is not None
    assert result.number == 11


def test_resolve_by_number_warm_cache_plus_outage_is_unavailable(tmp_path, monkeypatch):
    """Same availability guard for the explicit-number resolver."""
    branch = "feature-z"
    _warm_snapshot(tmp_path, branch, 42)

    monkeypatch.setattr(
        github_api, "list_open_prs",
        lambda cwd=None: (_ for _ in ()).throw(AssertionError("warm cache: no list call")),
    )
    github_api.reset_rate_limit_seen()
    monkeypatch.setattr(github_api, "get_latest_commit", lambda n, cwd=None: ("", ""))

    def _ci(n, cwd=None):
        # See test_warm_cache_plus_gh_unavailable_detail_is_unavailable: mirror
        # `_run`'s real recording (flag + monotonic event counter, BOU-1966).
        github_api._RATE_LIMIT_SEEN = True
        github_api._RATE_LIMIT_EVENTS = github_api._RATE_LIMIT_EVENTS + 1
        return []

    monkeypatch.setattr(github_api, "get_ci_checks", _ci)
    monkeypatch.setattr(github_api, "get_unaddressed_comments", lambda n, d, cwd=None: [])

    result = pr_state._resolve_pr_by_number(42, str(tmp_path))

    assert result is pr_state._GH_UNAVAILABLE


# --------------------------------------------------------------------------- #
# snapshot is partitioned by pr_author (PR #69 review)
# --------------------------------------------------------------------------- #

def test_author_mismatch_is_a_cache_miss(tmp_path, monkeypatch):
    """A fresh snapshot fetched for a DIFFERENT author must not be served.

    Regression for the PR #69 review: after pr_author is pinned (e.g.
    "ilganeli" under the App automation identity), a fresh "@me"-fetched []
    snapshot from an older process must not make stop/await resolution miss
    the operator's PR until the TTL expires.
    """
    from agentic_pr_dash import config

    stale_author_prs = []  # what @me-as-App-bot saw: nothing
    path = github_api._pr_snapshot_path(str(tmp_path))
    github_api._write_pr_snapshot(path, stale_author_prs, "@me")

    (tmp_path / "agentic-pr-dash.toml").write_text(
        'pr_author = "ilganeli"\n', encoding="utf-8"
    )
    config.load.cache_clear()
    try:
        fresh = _fake_prs(3)
        calls = {"n": 0}

        def _fake_list(cwd=None):
            calls["n"] += 1
            return fresh

        monkeypatch.setattr(github_api, "list_open_prs", _fake_list)

        result = github_api.list_open_prs_cached(str(tmp_path))

        assert result == fresh  # the @me snapshot was NOT served
        assert calls["n"] == 1  # a real fetch happened despite the fresh file
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["author"] == "ilganeli"  # rewritten under the new key
    finally:
        config.load.cache_clear()


def test_legacy_snapshot_without_author_field_is_a_miss(tmp_path, monkeypatch):
    """Snapshots written before the author field existed are treated as a miss
    (one extra fetch), never as a hit for any author."""
    import time as _time

    path = github_api._pr_snapshot_path(str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"fetched_at": _time.time(), "prs": _fake_prs(2)}),
        encoding="utf-8",
    )

    fresh = _fake_prs(5)
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: fresh)

    assert github_api.list_open_prs_cached(str(tmp_path)) == fresh


def test_same_author_hit_still_skips_gh(tmp_path, monkeypatch):
    """Partitioning must not break the herd-suppression hit path."""
    cached = _fake_prs(2)
    path = github_api._pr_snapshot_path(str(tmp_path))
    github_api._write_pr_snapshot(path, cached, "@me")

    monkeypatch.setattr(
        github_api, "list_open_prs",
        lambda cwd=None: (_ for _ in ()).throw(AssertionError("warm same-author cache: no list call")),
    )

    assert github_api.list_open_prs_cached(str(tmp_path)) == cached
