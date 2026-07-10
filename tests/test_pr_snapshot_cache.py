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
    github_api._write_pr_snapshot(path, cached)

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
    github_api._write_pr_snapshot(path, cached)

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
    github_api._write_pr_snapshot(path, prior)

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
    github_api._write_pr_snapshot(path, old)

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
