"""BOU-2041: terminal-clean is a named predicate, and `complete` obeys it.

Opening a PR is not completion. The design contract (BOU-2038) says a PR may be
declared done only when, *for the latest pushed head*: required checks are
green, there is no conflict, and no actionable review thread is unresolved —
and that if the head moves during completion, completion does not get to
declare victory against the head it validated.

Two holes this pins:

1. **Head drift.** ``_cmd_complete`` captures ``head_sha`` once, resolves and
   replies to threads using diff evidence computed against *that* head, then
   re-fetches the PR to recompute blockers. It never compares the two. A push
   landing mid-run means the evidence was gathered against a stale head while
   completion still reports "no blockers remain" and closes the task.

2. **CI still running.** ``blockers_for_pr`` reports only *actionable* work —
   a failing check, a conflict, an open comment. A PR whose required checks are
   still queued has none of those, so completion closes the task before CI
   concludes. ``watch_pending_for_pr`` already models this state but the
   completion path never consults it.

The distinction between the two predicates is deliberate and must survive:
``blockers_for_pr`` answers "what can an executor fix *right now*" and drives
dispatch, so pending CI must NOT enter it — the loop must never dispatch an
executor against a merely-running PR. ``terminal_clean_blockers`` answers "may
we declare this done", which is a strictly stronger question.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentic_pr_dash import maintenance
from agentic_pr_dash.models import PRData, PRStatus, ReviewComment


HEAD = "a" * 40
MOVED_HEAD = "b" * 40


def pr(**overrides) -> PRData:
    payload = dict(
        number=8,
        title="feat: something",
        branch="feature-branch",
        base_branch="main",
        url="https://github.com/Boundless-Studios/agentic-pr-dash/pull/8",
        latest_commit_sha=HEAD,
        merge_state="CLEAN",
        mergeable="MERGEABLE",
        status=PRStatus.CLEAN,
    )
    payload.update(overrides)
    payload.setdefault("maintenance_observed_head_sha", payload["latest_commit_sha"])
    payload.setdefault("maintenance_observed_base_branch", payload["base_branch"])
    payload.setdefault("maintenance_observed_at", datetime.now(UTC))
    return PRData(**payload)


def comment(comment_id: int = 1) -> ReviewComment:
    return ReviewComment(
        id=comment_id,
        author="reviewer",
        body="please fix",
        path="src/a.py",
        line=3,
        created_at="2026-07-25T00:00:00Z",
        is_inline=True,
    )


# ---------------------------------------------------------------------------
# The predicate exists and is named
# ---------------------------------------------------------------------------


def test_a_fully_clean_pr_has_no_terminal_blockers():
    assert maintenance.terminal_clean_blockers(pr(), validated_head=HEAD) == []


def test_terminal_clean_reports_merge_conflict():
    blockers = maintenance.terminal_clean_blockers(
        pr(merge_state="DIRTY"), validated_head=HEAD
    )
    assert "merge_conflict" in blockers


def test_terminal_clean_reports_failing_checks():
    blockers = maintenance.terminal_clean_blockers(
        pr(failing_checks=["unit-tests"]), validated_head=HEAD
    )
    assert "ci_failure" in blockers


def test_terminal_clean_reports_unresolved_review_comments():
    blockers = maintenance.terminal_clean_blockers(
        pr(review_comments=[comment()]), validated_head=HEAD
    )
    assert "review_comments" in blockers


# ---------------------------------------------------------------------------
# Gap 1: pending CI is not terminal-clean
# ---------------------------------------------------------------------------


def test_pending_required_ci_blocks_terminal_clean():
    """Queued is not green. 'No failures yet' is not 'checks succeeded'."""
    subject = pr(ci_watch_pending=True)

    assert maintenance.blockers_for_pr(subject) == []  # nothing to fix *now*
    assert "ci_pending" in maintenance.terminal_clean_blockers(
        subject, validated_head=HEAD
    )


def test_pending_ci_does_not_enter_the_dispatch_predicate():
    """Must not make the loop dispatch an executor against a running PR.

    This is the property that keeps ci-watch a *coverage* signal rather than a
    dispatch trigger; folding pending CI into blockers_for_pr would regress it.
    """
    subject = pr(ci_watch_pending=True)

    assert maintenance.blockers_for_pr(subject) == []
    assert maintenance.watch_pending_for_pr(subject) is True


def test_concluded_ci_with_no_failures_is_terminal_clean():
    subject = pr(ci_watch_pending=False, failing_checks=[])
    assert maintenance.terminal_clean_blockers(subject, validated_head=HEAD) == []


# ---------------------------------------------------------------------------
# Gap 2: head drift during completion
# ---------------------------------------------------------------------------


def test_head_moving_during_completion_blocks_terminal_clean():
    """The head validated by completion must equal the live head."""
    live = pr(latest_commit_sha=MOVED_HEAD)

    blockers = maintenance.terminal_clean_blockers(live, validated_head=HEAD)
    assert "head_drift" in blockers


def test_matching_head_does_not_report_drift():
    assert "head_drift" not in maintenance.terminal_clean_blockers(
        pr(), validated_head=HEAD
    )


def test_head_drift_is_reported_even_when_nothing_else_is_wrong():
    """Drift alone must be enough to deny completion.

    Otherwise a push landing mid-run is invisible: every other signal reads
    clean precisely because it was measured against the superseded head.
    """
    live = pr(latest_commit_sha=MOVED_HEAD)
    assert maintenance.terminal_clean_blockers(live, validated_head=HEAD) == [
        "head_drift"
    ]


def test_unknown_validated_head_does_not_fabricate_drift():
    """No baseline to compare against is not evidence of drift."""
    assert maintenance.terminal_clean_blockers(pr(), validated_head=None) == []
    assert maintenance.terminal_clean_blockers(pr(), validated_head="") == []


def test_drift_check_tolerates_a_missing_live_head():
    """An empty live sha means the API did not tell us — not that it moved."""
    live = pr(latest_commit_sha="")
    assert "head_drift" not in maintenance.terminal_clean_blockers(
        live, validated_head=HEAD
    )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def test_every_blocker_is_reported_not_just_the_first():
    """Completion should show the whole picture, not stop at the first failure."""
    live = pr(
        latest_commit_sha=MOVED_HEAD,
        merge_state="DIRTY",
        failing_checks=["unit-tests"],
        review_comments=[comment()],
        ci_watch_pending=True,
    )

    blockers = set(maintenance.terminal_clean_blockers(live, validated_head=HEAD))
    assert blockers == {
        "merge_conflict",
        "ci_failure",
        "review_comments",
        "ci_pending",
        "head_drift",
    }


def test_terminal_clean_is_a_superset_of_the_dispatch_predicate():
    """Anything an executor must fix also denies completion.

    Stated as a property rather than a fixed list so a future dispatch blocker
    cannot be added without it also gating completion.
    """
    cases = [
        pr(merge_state="DIRTY"),
        pr(failing_checks=["unit-tests"]),
        pr(review_comments=[comment()]),
        pr(status=PRStatus.MERGE_CONFLICT),
    ]
    for subject in cases:
        dispatch = set(maintenance.blockers_for_pr(subject))
        terminal = set(
            maintenance.terminal_clean_blockers(subject, validated_head=HEAD)
        )
        assert dispatch <= terminal, f"{dispatch} not a subset of {terminal}"


@pytest.mark.parametrize(
    "overrides",
    [
        {"merge_state": "DIRTY"},
        {"failing_checks": ["unit-tests"]},
        {"review_comments": [comment()]},
        {"ci_watch_pending": True},
        {"latest_commit_sha": MOVED_HEAD},
    ],
)
def test_no_single_defect_is_silently_tolerated(overrides):
    assert maintenance.terminal_clean_blockers(
        pr(**overrides), validated_head=HEAD
    ), f"{overrides} was treated as terminal-clean"


# ---------------------------------------------------------------------------
# The wiring: `complete` must actually consult the predicate
#
# A correct predicate nobody calls is worth nothing, so these drive the real
# _cmd_complete and assert on its decision — not on the helper in isolation.
# ---------------------------------------------------------------------------


def _wire_complete(monkeypatch, *, live_pr, threads=()):
    """Stub only the gh boundary; the completion decision logic stays real."""
    from agentic_pr_dash import github_api
    from agentic_pr_dash import maintenance_check as mc

    monkeypatch.setattr(mc, "_resolve_pr_by_number", lambda n, cwd, **kw: live_pr)
    monkeypatch.setattr(github_api, "get_local_pr_head", lambda branch, cwd: ("", ""))
    monkeypatch.setattr(github_api, "_is_ancestor", lambda a, d, cwd: False)
    monkeypatch.setattr(github_api, "get_new_pr_commits", lambda *a, **k: [])
    monkeypatch.setattr(
        github_api, "get_commit_changed_files", lambda sha, cwd=None: []
    )
    monkeypatch.setattr(
        github_api, "get_changed_line_spans", lambda b, h, p, cwd=None: None
    )
    monkeypatch.setattr(
        github_api, "get_review_threads", lambda n, cwd=None: list(threads)
    )
    monkeypatch.setattr(mc, "_mark_maintenance_complete", lambda *a, **k: None)


def _complete_args(baseline: str = "basesha"):
    import argparse

    return argparse.Namespace(cwd=".", pr=8, baseline=baseline)


def test_complete_does_not_declare_clean_when_the_head_moved(monkeypatch, capsys):
    """A push landing mid-run must deny "no blockers remain".

    The PR is otherwise pristine — no conflict, no failing check, no open
    thread. Only the head moved. Before BOU-2041 this printed
    "bead closed; no blockers remain".
    """
    from agentic_pr_dash import maintenance_check as mc

    # The run starts against HEAD ...
    start = pr(latest_commit_sha=HEAD)
    # ... and every post-mutation re-fetch sees a newer head.
    seen = {"n": 0}

    def _resolve(n, cwd, **kw):
        seen["n"] += 1
        return start if seen["n"] == 1 else pr(latest_commit_sha=MOVED_HEAD)

    _wire_complete(monkeypatch, live_pr=start)
    monkeypatch.setattr(mc, "_resolve_pr_by_number", _resolve)

    rc = mc._cmd_complete(_complete_args())
    out = capsys.readouterr().out

    assert rc == 0
    assert "head_drift" in out
    assert "no blockers remain" not in out


def test_complete_does_not_declare_clean_while_required_ci_is_running(
    monkeypatch, capsys
):
    """Queued checks must deny completion even with nothing else outstanding."""
    from agentic_pr_dash import maintenance_check as mc

    running = pr(ci_watch_pending=True)
    _wire_complete(monkeypatch, live_pr=running)

    rc = mc._cmd_complete(_complete_args())
    out = capsys.readouterr().out

    assert rc == 0
    assert "ci_pending" in out
    assert "no blockers remain" not in out


def test_complete_still_declares_clean_on_a_genuinely_finished_pr(
    monkeypatch, capsys
):
    """The gate must not become unsatisfiable — the happy path still closes."""
    from agentic_pr_dash import maintenance_check as mc

    done = pr(ci_watch_pending=False)
    _wire_complete(monkeypatch, live_pr=done)
    monkeypatch.setattr(mc, "_resolve_pr_by_number", lambda n, cwd, **kw: done)

    rc = mc._cmd_complete(_complete_args())
    out = capsys.readouterr().out

    assert rc == 0
    assert "no blockers remain" in out
