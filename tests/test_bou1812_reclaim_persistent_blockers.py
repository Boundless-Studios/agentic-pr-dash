"""BOU-1812: the "defer to live owner" path reclaims a demonstrably-stuck owner.

Defense-in-depth for the detached PR-maintenance loop. A live owner keeps
ownership while its heartbeat is fresh — but a fresh heartbeat is NOT proof the
PR is being serviced. When the owner's Stop-gate keeps returning a FALSE-NEGATIVE
"nothing pending" (it refreshes the heartbeat with work_found=False, popping the
fix-lease) while the PR still has REAL blockers, deferring every tick strands the
PR: neither the owner nor the loop services it. Policy (mirrors the BOU-1789
per-PR failure streak): count consecutive no-progress ticks (owner ``idle`` —
fresh heartbeat, no fix-lease — with the SAME blocker fingerprint) and, once the
streak reaches ``reclaim_no_progress_threshold``, RECLAIM (exit 10, service the
PR) instead of deferring forever.

A live, actively-progressing owner still wins: an active fix-lease (``fixing``),
or a changing blocker set, resets the streak and keeps ownership — only the
demonstrably-stuck defer is broken.

These drive the REAL engine against a REAL coordinator store and a REAL on-disk
ownership marker; only the PR-resolve and process boundaries are stubbed.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_pr_dash import config, maintenance_check
from agentic_pr_dash.models import PRData, PRStatus, ReviewComment
from agentic_pr_dash._maintenance import _common as _common_mod
from agentic_pr_dash._maintenance import completion as _completion_mod
from agentic_pr_dash._maintenance import markers as _markers_mod
from agentic_pr_dash._maintenance import pr_state as _pr_state_mod
from agentic_pr_dash._maintenance import waiter as _waiter_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash._maintenance import worktree_check as _wc

SID = "sess-loop"           # the detached loop / checker
OWNER = "sess-live-owner"   # the live in-session owner we may reclaim from


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(tmp_path / "claims.jsonl"))
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _now_iso(delta_seconds: int = 0) -> str:
    ts = datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_marker(worktree: Path, *, fields: dict[str, str]) -> None:
    """Write a real pr-watch.armed marker so ``_owner_progress_state`` reads it."""
    marker = Path(str(config.load(str(worktree)).watch_marker_for(str(worktree))))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        "".join(f"{k}={v}\n" for k, v in fields.items()), encoding="utf-8"
    )


def _idle_owner_marker(worktree: Path, *, pr: int = 42) -> None:
    """Owner ticked recently (fresh heartbeat) but has NO fix-lease → ``idle``."""
    _write_marker(
        worktree,
        fields={
            "pr": str(pr),
            "armed_at": _now_iso(-300),
            "session_id": OWNER,
            "pid": str(os.getpid()),  # alive
            "last_heartbeat": _now_iso(),
            "heartbeat": _now_iso(),
        },
    )


def _fixing_owner_marker(worktree: Path, *, pr: int = 42) -> None:
    """Owner holds an ACTIVE fix-lease → ``fixing`` (actively servicing)."""
    _write_marker(
        worktree,
        fields={
            "pr": str(pr),
            "armed_at": _now_iso(-300),
            "session_id": OWNER,
            "pid": str(os.getpid()),
            "last_heartbeat": _now_iso(),
            "heartbeat": _now_iso(),
            "fix_lease_until": _now_iso(1800),  # 30 min out → active
        },
    )


def _review_pr(worktree: Path) -> PRData:
    return PRData(
        number=42, title="needs review", branch="feature/x",
        url="https://github.com/Boundless-Studios/gaia-free/pull/42",
        worktree_path=str(worktree), status=PRStatus.HAS_COMMENTS,
        review_comments=[
            ReviewComment(id=7, author="r", body="fix", created_at="2026-06-11T12:00:00Z")
        ],
    )


def _stub_boundaries(monkeypatch: pytest.MonkeyPatch, pr: PRData) -> None:
    """Stub the read-only boundaries so ``_check_worktree`` reaches the marker-gate
    defer path with a live, WAKE-CAPABLE owner and deterministic blockers. The
    coordinator store and the on-disk marker stay REAL."""
    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda cwd, sid: OWNER)
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, owner: True)  # wake-capable
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: pr)
    monkeypatch.setattr(_pr_state_mod, "_unresolved_review_threads", lambda n, cwd: [])
    monkeypatch.setattr(_worktrees_mod, "_live_independent_owner_paths", lambda paths, sid: set())
    monkeypatch.setattr(_markers_mod, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda cwd: OWNER)
    from agentic_pr_dash import github_api
    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd: False)


def test_stuck_idle_owner_is_reclaimed_after_threshold(monkeypatch, tmp_path):
    """A live owner that repeatedly reports 'nothing pending' (idle, fresh
    heartbeat, no fix-lease) while the SAME blockers persist is deferred to for
    threshold-1 ticks, then RECLAIMED on the threshold tick."""
    monkeypatch.setenv("AGENTIC_PR_DASH_RECLAIM_NO_PROGRESS_THRESHOLD", "3")
    config.load.cache_clear()
    worktree = tmp_path / "wt"; worktree.mkdir()
    pr = _review_pr(worktree)
    _stub_boundaries(monkeypatch, pr)
    _idle_owner_marker(worktree)

    # Ticks 1 & 2 — streak below threshold: surface blockers, keep deferring.
    for i in (1, 2):
        code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
        assert code == 0, f"tick {i}: {text}"
        assert _wc.WARN_ONLY_MARKER in text, text
        assert OWNER in text

    # Tick 3 — streak reaches threshold: RECLAIM and service the PR.
    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
    assert code == 10, text
    assert "PR_NUMBER=42" in text


def test_actively_fixing_owner_is_never_reclaimed(monkeypatch, tmp_path):
    """Regression: a live owner holding an ACTIVE fix-lease is progressing, so it
    keeps ownership indefinitely — never reclaimed no matter how many ticks."""
    monkeypatch.setenv("AGENTIC_PR_DASH_RECLAIM_NO_PROGRESS_THRESHOLD", "2")
    config.load.cache_clear()
    worktree = tmp_path / "wt"; worktree.mkdir()
    pr = _review_pr(worktree)
    _stub_boundaries(monkeypatch, pr)
    _fixing_owner_marker(worktree)

    for i in range(5):  # well past the threshold
        code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
        assert code == 0, f"tick {i}: {text}"
        assert _wc.WARN_ONLY_MARKER in text, text  # defers, never takes over


def test_changing_blockers_reset_the_streak(monkeypatch, tmp_path):
    """Regression: an owner whose blocker set keeps CHANGING is making progress —
    the streak resets each tick, so the threshold is never reached."""
    monkeypatch.setenv("AGENTIC_PR_DASH_RECLAIM_NO_PROGRESS_THRESHOLD", "2")
    config.load.cache_clear()
    worktree = tmp_path / "wt"; worktree.mkdir()
    _idle_owner_marker(worktree)
    from agentic_pr_dash import github_api
    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd: False)
    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda cwd, sid: OWNER)
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, owner: True)
    monkeypatch.setattr(_pr_state_mod, "_unresolved_review_threads", lambda n, cwd: [])
    monkeypatch.setattr(_worktrees_mod, "_live_independent_owner_paths", lambda paths, sid: set())
    monkeypatch.setattr(_markers_mod, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda cwd: OWNER)

    # Each tick the review-comment id differs → different fingerprint → reset.
    for tick in range(5):
        pr = PRData(
            number=42, title="needs review", branch="feature/x",
            url="https://github.com/Boundless-Studios/gaia-free/pull/42",
            worktree_path=str(worktree), status=PRStatus.HAS_COMMENTS,
            review_comments=[
                ReviewComment(id=100 + tick, author="r", body="fix",
                              created_at="2026-06-11T12:00:00Z")
            ],
        )
        monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd, p=pr: p)
        code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
        assert code == 0, f"tick {tick}: {text}"
        assert _wc.WARN_ONLY_MARKER in text, text


def test_single_transient_no_progress_tick_does_not_reclaim(monkeypatch, tmp_path):
    """A single 'nothing pending' tick must never reclaim (threshold defaults to
    3); it only surfaces the blockers and defers."""
    monkeypatch.delenv("AGENTIC_PR_DASH_RECLAIM_NO_PROGRESS_THRESHOLD", raising=False)
    config.load.cache_clear()
    worktree = tmp_path / "wt"; worktree.mkdir()
    pr = _review_pr(worktree)
    _stub_boundaries(monkeypatch, pr)
    _idle_owner_marker(worktree)

    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
    assert code == 0, text
    assert _wc.WARN_ONLY_MARKER in text
    # default threshold is 3 → one tick can't reach it
    assert config.load(str(worktree)).reclaim_no_progress_threshold == 3


def test_passive_stop_gate_probe_never_reclaims(monkeypatch, tmp_path):
    """The passive stop-gate probe (claim=False) must never advance the streak or
    reclaim — the reclaim mechanism belongs to the active detached loop. Even far
    past the threshold it keeps deferring (warn-only, no dispatch)."""
    monkeypatch.setenv("AGENTIC_PR_DASH_RECLAIM_NO_PROGRESS_THRESHOLD", "2")
    config.load.cache_clear()
    worktree = tmp_path / "wt"; worktree.mkdir()
    pr = _review_pr(worktree)
    _stub_boundaries(monkeypatch, pr)
    _idle_owner_marker(worktree)

    for i in range(5):
        code, text = maintenance_check._check_worktree(str(worktree), SID, claim=False)
        assert code == 0, f"tick {i}: {text}"
        assert _wc.WARN_ONLY_MARKER in text, text
        assert "COORDINATOR_CLAIM_ID=" not in text  # never dispatches


def test_streak_resets_when_owner_starts_fixing(monkeypatch, tmp_path):
    """If the owner begins servicing (acquires a fix-lease) mid-streak, the streak
    resets — so a later idle spell must start counting from scratch, not inherit
    the pre-fix count and reclaim early."""
    monkeypatch.setenv("AGENTIC_PR_DASH_RECLAIM_NO_PROGRESS_THRESHOLD", "3")
    config.load.cache_clear()
    worktree = tmp_path / "wt"; worktree.mkdir()
    pr = _review_pr(worktree)
    _stub_boundaries(monkeypatch, pr)

    # Two idle ticks (streak = 2, below threshold 3).
    _idle_owner_marker(worktree)
    for _ in range(2):
        code, _text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
        assert code == 0

    # Owner starts fixing → streak resets.
    _fixing_owner_marker(worktree)
    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
    assert code == 0, text
    assert _wc.WARN_ONLY_MARKER in text

    # Back to idle: needs a FULL fresh streak (3) before reclaiming, proving the
    # reset. Ticks 1 & 2 defer; tick 3 reclaims.
    _idle_owner_marker(worktree)
    for i in (1, 2):
        code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
        assert code == 0, f"post-reset tick {i}: {text}"
    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
    assert code == 10, text
    assert "PR_NUMBER=42" in text


# ---------------------------------------------------------------------------
# Codex PR #83 review — P2: hydrate unresolved threads before fingerprinting
# ---------------------------------------------------------------------------

def _ci_pr(worktree: Path, *, number: int = 43) -> PRData:
    """A CI-failing PR — ``blockers_for_pr`` returns a non-empty set, so
    ``_resolve_and_blockers`` does NOT fetch unresolved review threads."""
    return PRData(
        number=number, title="ci red", branch="feature/ci",
        url=f"https://github.com/Boundless-Studios/gaia-free/pull/{number}",
        worktree_path=str(worktree), status=PRStatus.CI_FAILING,
        failing_checks=["ci/integration"], latest_commit_sha="deadbeef",
    )


def test_new_unresolved_thread_on_ci_pr_resets_streak(monkeypatch, tmp_path):
    """P2 regression: a PR that already has a primary blocker (failed CI) never
    triggers ``_resolve_and_blockers``' thread fallback, so the reclaim path must
    hydrate unresolved threads ITSELF before fingerprinting. A NEW GraphQL-only
    thread appearing on tick 2 must change the fingerprint and RESET the streak —
    without the fix the fingerprint is unchanged, the streak reaches the
    threshold, and the loop wrongly reclaims."""
    monkeypatch.setenv("AGENTIC_PR_DASH_RECLAIM_NO_PROGRESS_THRESHOLD", "2")
    config.load.cache_clear()
    worktree = tmp_path / "wt"; worktree.mkdir()
    pr = _ci_pr(worktree)
    _stub_boundaries(monkeypatch, pr)
    _idle_owner_marker(worktree, pr=43)

    # Per-tick unresolved threads: none on tick 1, a NEW thread on tick 2+.
    new_comment = ReviewComment(
        id=555, author="human", body="please change this",
        created_at="2026-07-01T00:00:00Z",
    )
    threads_by_tick = [[], [new_comment], [new_comment]]
    state = {"i": 0}

    def fake_unresolved(n, cwd):
        return threads_by_tick[min(state["i"], len(threads_by_tick) - 1)]

    monkeypatch.setattr(_pr_state_mod, "_unresolved_review_threads", fake_unresolved)
    # Identity: our fake threads ARE already ReviewComment records.
    monkeypatch.setattr(
        _completion_mod, "_review_comments_from_threads", lambda threads: list(threads)
    )

    # Tick 1 — no thread: streak = 1, defer.
    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
    assert code == 0, text
    assert _wc.WARN_ONLY_MARKER in text
    state["i"] = 1

    # Tick 2 — a NEW thread changed the fingerprint: streak RESETS to 1, defer
    # (would be reclaim at streak==threshold 2 without the hydrate fix).
    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
    assert code == 0, text
    assert _wc.WARN_ONLY_MARKER in text

    # Tick 3 — same thread persists (fingerprint stable): streak = 2 → reclaim,
    # proving the reset only cost one tick and legitimate reclaim still fires.
    state["i"] = 2
    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
    assert code == 10, text
    assert "PR_NUMBER=43" in text


# ---------------------------------------------------------------------------
# Codex PR #83 review — P1: the marker must cover the RECLAIMED PR
# ---------------------------------------------------------------------------

def _stub_ledger_gate(monkeypatch, pr, ledger_cwd):
    """Reach the LEDGER gate: no marker-gate owner, ownership resolved from the
    durable ledger to ``OWNER`` whose marker lives in ``ledger_cwd``."""
    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: pr)
    monkeypatch.setattr(_pr_state_mod, "_unresolved_review_threads", lambda n, cwd: [])
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda cwd: None)
    monkeypatch.setattr(_worktrees_mod, "_marker_pr", lambda cwd: None)
    monkeypatch.setattr(
        _worktrees_mod, "_live_independent_owner_paths", lambda paths, sid: set()
    )
    monkeypatch.setattr(_markers_mod, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr(_common_mod, "_repo_slug", lambda cwd: "acme/repo")
    monkeypatch.setattr(
        _markers_mod, "_live_pr_owner_record",
        lambda pr_number, repo, sid, cwd: (OWNER, str(ledger_cwd)),
    )
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, owner: True)  # wake-capable
    from agentic_pr_dash import github_api
    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd: False)
    monkeypatch.setattr(github_api, "get_failed_logs", lambda sha, checks, cwd: {})


def test_ledger_owner_marker_for_different_pr_is_not_reclaimed(monkeypatch, tmp_path):
    """P1 regression: a live session can hold LEDGER ownership of several PRs
    while its worktree marker names only its CURRENT branch's PR. An idle marker
    for a DIFFERENT PR (#99) must NOT be read as 'this PR (#43) reported idle' —
    so PR #43 is deferred to FOREVER, never reclaimed, no matter how many ticks.
    Without the marker-pr check this reclaims after the threshold."""
    monkeypatch.setenv("AGENTIC_PR_DASH_RECLAIM_NO_PROGRESS_THRESHOLD", "2")
    config.load.cache_clear()
    worktree = tmp_path / "wt"; worktree.mkdir()
    ledger_cwd = tmp_path / "owner-wt"; ledger_cwd.mkdir()
    pr = _ci_pr(worktree, number=43)
    _stub_ledger_gate(monkeypatch, pr, ledger_cwd)
    # Owner's marker is idle but names PR #99, NOT #43.
    _idle_owner_marker(ledger_cwd, pr=99)

    for i in range(5):  # well past the threshold
        code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
        assert code == 0, f"tick {i}: {text}"
        assert _wc.WARN_ONLY_MARKER in text  # defers, never reclaims
        assert "(ledger)" in text
        assert "COORDINATOR_CLAIM_ID=" not in text


def test_ledger_owner_marker_for_same_pr_still_reclaims(monkeypatch, tmp_path):
    """P1 positive control: when the ledger owner's marker names THE SAME PR (#43)
    and is idle, the legitimate reclaim still fires after the threshold — the
    marker-pr guard doesn't over-block genuine no-progress reclaim."""
    monkeypatch.setenv("AGENTIC_PR_DASH_RECLAIM_NO_PROGRESS_THRESHOLD", "2")
    config.load.cache_clear()
    worktree = tmp_path / "wt"; worktree.mkdir()
    ledger_cwd = tmp_path / "owner-wt"; ledger_cwd.mkdir()
    pr = _ci_pr(worktree, number=43)
    _stub_ledger_gate(monkeypatch, pr, ledger_cwd)
    _idle_owner_marker(ledger_cwd, pr=43)  # marker covers THE reclaimed PR

    # Tick 1 defers (streak 1); tick 2 reclaims (streak 2 == threshold).
    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
    assert code == 0, text
    assert _wc.WARN_ONLY_MARKER in text
    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=True)
    assert code == 10, text
    assert "PR_NUMBER=43" in text
