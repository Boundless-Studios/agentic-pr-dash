"""BOU-2556: the stop-gate must not pay O(N) serial round trips (nor an
unbounded wall clock) for a session that owns N PRs.

Regression story: the gate ran fine at 1 owned PR and started timing out
(~108s Stop-hook deadline) at 7 — each owned PR cost at least a review-thread
query and a CI-rollup query, made serially with no wall-clock budget. This
file is the RED-then-GREEN evidence for the fix:

  1. The batched multi-PR GraphQL fetch is ONE round trip regardless of N
     (deterministic — asserts a call COUNT, never elapsed wall time).
  2. The stop-gate's prefetch orchestration invokes that batched fetch ONCE
     per repo for the whole owned set, not once per worktree.
  3. A wall-clock budget bounds the per-worktree loop: once exhausted, the
     REMAINING owned worktrees are reported UNKNOWN (never silently "clean"),
     distinctly from a confirmed blocker, and distinctly from a genuine crash.
  4. A genuinely blocking PR still blocks even when the budget is available —
     a gate that gets fast by checking less would be worse than a slow gate.
  5. The across-firings per-PR cache (keyed on local head sha) skips
     re-checking an unchanged, previously-CLEAN worktree, while a worktree
     with a real pending blocker keeps forcing the gate to run every tick
     (proving the interaction with the whole-gate STOP_INTERVAL skip).
"""
from __future__ import annotations

import subprocess
import time
import time as real_time
from pathlib import Path

import pytest

from agentic_pr_dash import config, maintenance_check as mc
from agentic_pr_dash import github_api as _github_api_mod
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import stop_gate as _stop_gate_mod
from agentic_pr_dash._maintenance import worktree_check as _worktree_check_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod


SID = "sess-2556-budget"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Disable the whole-gate STOP_INTERVAL skip and clear the batch cache so
    tests are independent of both real time and any other test's leftovers."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(tmp_path / "empty-daemons"))
    config.load.cache_clear()
    _github_api_mod.clear_pr_batch_cache()
    yield
    _github_api_mod.clear_pr_batch_cache()
    config.load.cache_clear()


def _make_armed_worktree(tmp_path: Path, name: str, session_id: str, pr_number: int) -> Path:
    wt = tmp_path / name
    wt.mkdir(exist_ok=True)
    mc._write_arm_marker(str(wt), session_id, real_pid(), pr_number)
    return wt


def real_pid() -> int:
    import os
    return os.getpid()


def _make_git_worktree(tmp_path: Path, name: str, session_id: str, pr_number: int) -> Path:
    """A REAL tiny git repo (needed for `_local_head_sha`'s `git rev-parse HEAD`),
    armed for ``pr_number``."""
    wt = tmp_path / name
    wt.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=wt, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=wt, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=wt, check=True)
    (wt / "f.txt").write_text("1")
    subprocess.run(["git", "add", "."], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=wt, check=True)
    mc._write_arm_marker(str(wt), session_id, real_pid(), pr_number)
    return wt


# ---------------------------------------------------------------------------
# 1. The batched fetch itself is ONE round trip for N PRs.
# ---------------------------------------------------------------------------


def test_batch_fetch_pr_review_and_ci_is_one_round_trip_for_many_prs(monkeypatch):
    pr_numbers = [101, 102, 103, 104, 105, 106, 107]
    calls: list[list[str]] = []

    def _fake_run(cmd, cwd=None, timeout_s=30):
        calls.append(cmd)
        # One repository object with per-PR aliased fields, mirroring what
        # the real query would return: PR 101 has 1 unresolved thread and no
        # pending required check; the rest are clean.
        repo_fields = {}
        for i, n in enumerate(pr_numbers):
            thread_nodes = []
            if n == 101:
                thread_nodes = [{
                    "id": "THREAD1", "isResolved": False, "isOutdated": False,
                    "comments": {"nodes": [{
                        "databaseId": 1, "path": "a.py", "line": 1, "originalLine": 1,
                        "body": "fix this", "author": {"login": "reviewer"},
                        "createdAt": "2026-01-01T00:00:00Z",
                    }]},
                }]
            repo_fields[f"pr_{n}"] = {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": thread_nodes,
                },
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {
                    "contexts": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [{
                            "__typename": "CheckRun", "status": "COMPLETED",
                            "isRequired": True,
                        }],
                    },
                }}}]},
            }
        import json as _json
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_json.dumps({"data": {"repository": repo_fields}}), stderr="",
        )

    monkeypatch.setattr(_github_api_mod, "_run", _fake_run)

    result = _github_api_mod.batch_fetch_pr_review_and_ci(
        "acme", "widgets", pr_numbers, cwd="/tmp/whatever",
    )

    # The whole point: ONE round trip regardless of how many PRs (deterministic
    # call-count assertion, never a timing one).
    assert len(calls) == 1
    assert set(result.keys()) == set(pr_numbers)
    assert len(result[101]["threads"]) == 1
    assert result[101]["required_pending"] is False
    for n in pr_numbers:
        if n != 101:
            assert result[n]["threads"] == []
            assert result[n]["required_pending"] is False


def test_batch_snapshot_is_typed_complete_and_primes_all_detail_reads(monkeypatch):
    """One aggregate observation carries every stop-gate fact for each PR.

    In particular, resolving a PR after priming must not fall back to the two
    serial calls that survived BOU-2556: latest commit and full CI checks.
    """
    calls: list[list[str]] = []

    def _fake_run(cmd, cwd=None, timeout_s=30):
        calls.append(cmd)
        payload = {
            "data": {"repository": {"pr_401": {
                "headRefOid": "abc123",
                "mergeStateStatus": "DIRTY",
                "mergeable": "CONFLICTING",
                "reviewDecision": "CHANGES_REQUESTED",
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [{
                        "id": "T401", "isResolved": False, "isOutdated": False,
                        "comments": {"nodes": [{
                            "databaseId": 9, "path": "x.py", "line": 2,
                            "originalLine": 2, "body": "fix", "createdAt": "2026-07-01T00:00:00Z",
                            "author": {"login": "reviewer"},
                        }]},
                    }],
                },
                "commits": {"nodes": [{"commit": {
                    "oid": "abc123", "committedDate": "2026-07-01T01:00:00Z",
                    "statusCheckRollup": {"contexts": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [{
                            "__typename": "CheckRun", "name": "unit",
                            "status": "COMPLETED", "conclusion": "FAILURE",
                            "isRequired": True,
                        }],
                    }},
                }}]},
            }}},
        }
        import json as _json
        return subprocess.CompletedProcess(cmd, 0, stdout=_json.dumps(payload), stderr="")

    monkeypatch.setattr(_github_api_mod, "_run", _fake_run)
    monkeypatch.setattr(_github_api_mod, "_repo_for_cwd", lambda cwd: "acme/widgets")
    batch = _github_api_mod.collect_pr_maintenance_snapshots(
        "acme", "widgets", [401], cwd="/tmp/repo",
    )

    assert batch.complete is True
    assert batch.requested == (401,)
    assert batch.missing == ()
    snapshot = batch.observed[401]
    assert snapshot.head_sha == "abc123"
    assert snapshot.merge_conflict is True
    assert snapshot.changes_requested is True
    assert [check.name for check in snapshot.ci_checks] == ["unit"]
    assert snapshot.ci_checks[0].conclusion == "failure"
    assert len(snapshot.unresolved_threads) == 1

    _github_api_mod.clear_pr_batch_cache()
    _github_api_mod.prime_pr_batch_cache("acme/widgets", batch.cache_entries())
    calls.clear()
    assert _github_api_mod.get_latest_commit(401, "/tmp/repo") == (
        "abc123", "2026-07-01T01:00:00Z",
    )
    checks = _github_api_mod.get_ci_checks(401, "/tmp/repo")
    assert [(c.name, c.status, c.conclusion) for c in checks] == [
        ("unit", "completed", "failure"),
    ]
    assert calls == []


def test_batch_snapshot_preserves_observed_results_when_another_pr_is_missing(monkeypatch):
    def _fake_run(cmd, cwd=None, timeout_s=30):
        import json as _json
        payload = {"data": {"repository": {
            "pr_501": {
                "headRefOid": "head501", "mergeStateStatus": "CLEAN",
                "mergeable": "MERGEABLE", "reviewDecision": "APPROVED",
                "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": []},
                "commits": {"nodes": [{"commit": {
                    "oid": "head501", "committedDate": "2026-07-01T00:00:00Z",
                    "statusCheckRollup": None,
                }}]},
            },
            "pr_502": None,
        }}}
        return subprocess.CompletedProcess(cmd, 0, stdout=_json.dumps(payload), stderr="")

    monkeypatch.setattr(_github_api_mod, "_run", _fake_run)
    batch = _github_api_mod.collect_pr_maintenance_snapshots(
        "acme", "widgets", [501, 502], cwd="/tmp/repo",
    )

    assert batch.complete is False
    assert set(batch.observed) == {501}
    assert batch.missing == (502,)


# ---------------------------------------------------------------------------
# 2. The stop-gate's prefetch orchestration batches once per repo, not once
#    per worktree.
# ---------------------------------------------------------------------------


def test_stop_gate_prefetches_once_for_many_owned_worktrees_same_repo(
    monkeypatch, tmp_path, capsys,
):
    pr_numbers = list(range(200, 208))  # 8 owned PRs, same repo
    worktrees = [
        _make_armed_worktree(tmp_path, f"wt{n}", SID, n) for n in pr_numbers
    ]

    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(w) for w in worktrees],
    )
    monkeypatch.setattr(
        _github_api_mod, "repo_slug_for_prefetch", lambda cwd: "acme/widgets",
    )
    # No prior cache entries and no positively-observed `updatedAt` (the
    # listing call is not mocked here), so every one of the 8 is a cache
    # MISS this tick — the whole owned set is the prefetch's target.
    batch_calls: list[list[int]] = []

    def _fake_batch(owner, repo, numbers, cwd=None, *, deadline=None):
        batch_calls.append(list(numbers))
        return {n: {"threads": [], "required_pending": False} for n in numbers}

    monkeypatch.setattr(_github_api_mod, "batch_fetch_pr_review_and_ci", _fake_batch)
    monkeypatch.setattr(
        _worktree_check_mod, "_check_worktree",
        lambda path, sid, *, claim=True: (0, "nothing pending"),
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])

    # 8 owned PRs in one repo, all cache MISSES, used to cost (at least) 8
    # serial "review + CI" pairs; the batch call must fire exactly ONCE,
    # covering all 8 numbers.
    assert len(batch_calls) == 1
    assert sorted(batch_calls[0]) == pr_numbers
    assert rc in (0, 2)  # not asserting the waiter/clean branch here — just the batching


# ---------------------------------------------------------------------------
# 3. Wall-clock budget: exhausted mid-loop -> remaining worktrees UNKNOWN,
#    never silently clean, distinctly worded from a real blocker or a crash.
# ---------------------------------------------------------------------------


def test_stop_gate_budget_exhausted_marks_remaining_worktrees_unknown(
    monkeypatch, tmp_path, capsys,
):
    monkeypatch.setenv("PR_AGENT_OPS_STOP_GATE_BUDGET", "5")
    config.load.cache_clear()

    pr_numbers = list(range(300, 310))  # 10 owned PRs
    worktrees = [
        _make_armed_worktree(tmp_path, f"wt{n}", SID, n) for n in pr_numbers
    ]
    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(w) for w in worktrees],
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    # No batching benefit needed for this test — force the "no batch entries"
    # path so every worktree goes through _check_worktree individually.
    monkeypatch.setattr(_github_api_mod, "repo_slug_for_prefetch", lambda cwd: "")

    # Deterministic fake clock: each `_check_worktree` call is "expensive"
    # (advances the clock by 2s); the real `time.monotonic` is used only to
    # seed the deadline once, then frozen/advanced by the stub. This makes the
    # budget assertion a pure call-count/behavior test, never a real-time one.
    clock = {"t": real_time.monotonic()}

    def _fake_monotonic():
        return clock["t"]

    monkeypatch.setattr(time, "monotonic", _fake_monotonic)

    checked: list[str] = []

    def _fake_check(path, sid, *, claim=True):
        checked.append(path)
        clock["t"] += 2.0  # each check "costs" 2s of the 5s budget
        return 0, "nothing pending"

    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", _fake_check)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err

    assert rc == 2
    # Budget was 5s at 2s/worktree -> checks 3 before the 4th observation
    # exceeds the deadline (t0, t0+2, t0+4 all < t0+5; t0+6 is not).
    assert len(checked) == 3
    assert "BUDGET-UNKNOWN" in err
    assert "checked 3 of 10" in err
    # Never silently "clean" -- the message must say something was left
    # unknown, and must NOT claim a confirmed blocker (that's a different,
    # more actionable message reserved for `pending`).
    assert "not confirmed clean" in err.lower() or "UNKNOWN" in err
    # Distinguishable from a genuine crash (item 4): this path never prints
    # the crash wording.
    assert "CRASHED" not in err

    # Unknown observations get only a bounded release: they may escape after
    # the configured strike count, but must be retried after the cache window.
    assert mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID]) == 2
    assert mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID]) == 0
    capsys.readouterr()
    state = _stop_gate_mod._load_stop_state(str(tmp_path))
    assert state["released_until"] > state["ts"]
    _stop_gate_mod._save_stop_state(
        str(tmp_path), {**state, "released_until": 0}
    )
    assert mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID]) == 2
    assert "BUDGET-UNKNOWN" in capsys.readouterr().err


def test_stop_gate_crash_message_distinct_from_budget_message(monkeypatch, tmp_path, capsys):
    """A genuine internal exception must be labeled a CRASH, never confused
    with the budget-exhausted ('BUDGET-UNKNOWN') partial-completion path."""
    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(mc, "_stop_gate_impl", _boom)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err

    assert rc == 0  # fail-open on a genuine crash, unchanged behavior
    assert "CRASHED" in err
    assert "kaboom" in err
    assert "BUDGET-UNKNOWN" not in err


# ---------------------------------------------------------------------------
# 4. A genuinely blocking PR still blocks when the budget is available —
#    speed must never come at the cost of missing real work.
# ---------------------------------------------------------------------------


def test_stop_gate_still_blocks_on_genuine_pending_with_budget_available(
    monkeypatch, tmp_path, capsys,
):
    wt = _make_armed_worktree(tmp_path, "wt", SID, 555)
    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(wt)],
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    monkeypatch.setattr(
        _worktree_check_mod, "_check_worktree",
        lambda path, sid, *, claim=True: (
            10, "real blocker here\nSUMMARY=PR #555: 1 unresolved review comment(s)\nPR_NUMBER=555",
        ),
    )

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err

    assert rc == 2
    assert "555" in err
    assert "BUDGET-UNKNOWN" not in err  # a real blocker, not a budget artifact


# ---------------------------------------------------------------------------
# 5. Across-firings per-PR cache (head-sha keyed): an unchanged, previously
#    CLEAN worktree is not re-checked on a later stop, while a worktree with
#    a real pending blocker keeps forcing the gate to run every tick.
# ---------------------------------------------------------------------------


def _mock_updated_at_listing(monkeypatch, updated_at_by_pr: dict[int, str]) -> None:
    """Make ``_owned_pr_updated_at_map``'s forced listing return a fixed
    ``updatedAt`` per PR number, for one shared "repo" covering every
    worktree in the test (matching production: one forced ``gh pr list`` per
    distinct repo, never per worktree)."""
    monkeypatch.setattr(
        _github_api_mod, "repo_slug_for_prefetch", lambda cwd: "acme/widgets",
    )
    monkeypatch.setattr(
        _github_api_mod,
        "list_open_prs_cached",
        lambda cwd=None, *, force=False, ttl_s=None: [
            {"number": n, "updatedAt": u} for n, u in updated_at_by_pr.items()
        ],
    )


def test_stop_gate_head_sha_cache_skips_recheck_of_unchanged_clean_worktree(
    monkeypatch, tmp_path, capsys,
):
    # A positive STOP_INTERVAL so the per-PR cache TTL has room to matter;
    # kept alive across ticks by the SECOND worktree's genuine pending work
    # (which bypasses the whole-gate rate-limit skip every time).
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "120")
    config.load.cache_clear()

    clean_wt = _make_git_worktree(tmp_path, "clean", SID, 601)
    pending_wt = _make_armed_worktree(tmp_path, "pending", SID, 602)

    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(clean_wt), str(pending_wt)],
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    # Both PRs' `updatedAt` is stable across both ticks below.
    _mock_updated_at_listing(
        monkeypatch, {601: "2026-01-01T00:00:00Z", 602: "2026-01-01T00:00:00Z"}
    )

    checked: list[str] = []

    def _fake_check(path, sid, *, claim=True):
        checked.append(path)
        if path == str(clean_wt):
            return 0, "nothing pending"
        return 10, "real blocker\nPR_NUMBER=602"

    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", _fake_check)

    rc1 = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert rc1 == 2  # pending_wt blocks
    assert checked == [str(clean_wt), str(pending_wt)]  # both checked tick 1

    checked.clear()
    rc2 = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert rc2 == 2  # pending_wt still blocks -> gate keeps running every tick
    # clean_wt's head sha AND its PR's updatedAt are both unchanged, and its
    # last result was CLEAN -> the cache skips re-checking it; only
    # pending_wt (a real blocker) is actually re-examined.
    assert checked == [str(pending_wt)]


def test_stop_gate_head_sha_cache_rechecks_on_advanced_updated_at(
    monkeypatch, tmp_path, capsys,
):
    """Same local head, but GitHub's `updatedAt` moved (e.g. a comment landed
    with no push) -> the cache must not serve a stale clean verdict."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "120")
    config.load.cache_clear()

    clean_wt = _make_git_worktree(tmp_path, "clean-adv", SID, 611)
    pending_wt = _make_armed_worktree(tmp_path, "pending-adv", SID, 612)

    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(clean_wt), str(pending_wt)],
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    updated_at = {"611": "2026-01-01T00:00:00Z", "612": "2026-01-01T00:00:00Z"}
    monkeypatch.setattr(
        _github_api_mod, "repo_slug_for_prefetch", lambda cwd: "acme/widgets",
    )
    monkeypatch.setattr(
        _github_api_mod,
        "list_open_prs_cached",
        lambda cwd=None, *, force=False, ttl_s=None: [
            {"number": 611, "updatedAt": updated_at["611"]},
            {"number": 612, "updatedAt": updated_at["612"]},
        ],
    )

    checked: list[str] = []

    def _fake_check(path, sid, *, claim=True):
        checked.append(path)
        if path == str(clean_wt):
            return 0, "nothing pending"
        return 10, "real blocker\nPR_NUMBER=612"

    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", _fake_check)

    mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    checked.clear()

    # A comment lands on clean_wt's PR: `updatedAt` advances, head does not.
    updated_at["611"] = "2026-01-01T01:00:00Z"

    mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert str(clean_wt) in checked


def test_stop_gate_head_sha_cache_rechecks_when_listing_unavailable(
    monkeypatch, tmp_path, capsys,
):
    """The forced listing this tick can't observe `updatedAt` at all (rate
    limit / outage) -> no cache hit is possible, never a stale "clean"."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "120")
    config.load.cache_clear()

    clean_wt = _make_git_worktree(tmp_path, "clean-outage", SID, 621)
    pending_wt = _make_armed_worktree(tmp_path, "pending-outage", SID, 622)

    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(clean_wt), str(pending_wt)],
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    _mock_updated_at_listing(
        monkeypatch, {621: "2026-01-01T00:00:00Z", 622: "2026-01-01T00:00:00Z"}
    )

    checked: list[str] = []

    def _fake_check(path, sid, *, claim=True):
        checked.append(path)
        if path == str(clean_wt):
            return 0, "nothing pending"
        return 10, "real blocker\nPR_NUMBER=622"

    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", _fake_check)

    mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    checked.clear()

    # The listing is unavailable this tick — no `updatedAt` evidence for
    # ANY owned PR, so no cache hit is possible for this repo's worktrees.
    monkeypatch.setattr(
        _github_api_mod, "list_open_prs_cached",
        lambda cwd=None, *, force=False, ttl_s=None: None,
    )

    mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert str(clean_wt) in checked


def test_stop_gate_head_sha_cache_rechecks_entry_missing_updated_at(
    monkeypatch, tmp_path, capsys,
):
    """A cache entry written before this field existed (or on a tick where the
    listing was unavailable) has no ``updated_at`` -> fails closed."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "120")
    config.load.cache_clear()

    clean_wt = _make_git_worktree(tmp_path, "clean-noua", SID, 631)
    pending_wt = _make_armed_worktree(tmp_path, "pending-noua", SID, 632)

    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(clean_wt), str(pending_wt)],
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    _mock_updated_at_listing(
        monkeypatch, {631: "2026-01-01T00:00:00Z", 632: "2026-01-01T00:00:00Z"}
    )

    checked: list[str] = []

    def _fake_check(path, sid, *, claim=True):
        checked.append(path)
        if path == str(clean_wt):
            return 0, "nothing pending"
        return 10, "real blocker\nPR_NUMBER=632"

    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", _fake_check)

    mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    checked.clear()

    # Strip `updated_at` from the persisted entry, as if it had been written
    # before this field existed.
    cache = _stop_gate_mod._load_pr_head_cache(str(tmp_path))
    cache[str(clean_wt)].pop("updated_at", None)
    _stop_gate_mod._save_pr_head_cache(str(tmp_path), cache)

    mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert str(clean_wt) in checked


def test_stop_gate_head_sha_cache_rechecks_after_new_commit(monkeypatch, tmp_path):
    """The cache invalidates the instant the worktree's local head moves —
    staleness is bounded by real change, not just the TTL."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "120")
    config.load.cache_clear()

    clean_wt = _make_git_worktree(tmp_path, "clean2", SID, 701)
    pending_wt = _make_armed_worktree(tmp_path, "pending2", SID, 702)

    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(clean_wt), str(pending_wt)],
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )

    checked: list[str] = []

    def _fake_check(path, sid, *, claim=True):
        checked.append(path)
        if path == str(clean_wt):
            return 0, "nothing pending"
        return 10, "real blocker\nPR_NUMBER=702"

    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", _fake_check)

    mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    checked.clear()

    # A new commit lands on clean_wt's branch (e.g. an unrelated push) --
    # its head sha changes, so the cache must NOT skip it next tick.
    (clean_wt / "f.txt").write_text("2")
    subprocess.run(["git", "add", "."], cwd=clean_wt, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "second"], cwd=clean_wt, check=True)

    mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert str(clean_wt) in checked
