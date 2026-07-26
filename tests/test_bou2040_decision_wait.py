"""BOU-2040: a pending human decision pauses PR maintenance without failing it.

When an executor reaches a boundary it does not own — a service split, a schema
change — it records a coordinator decision request (BOU-2039) and stops. From
the loop's outside view that looks exactly like an executor that exited without
doing the work. Treating it as a failure is wrong twice over: it burns a
failure-streak slot toward escalation, and it re-dispatches an executor that
will immediately hit the same boundary and stop again — a retry loop around a
question only a human can answer.

The properties pinned here:

- A pending decision makes the PR non-dispatchable, in a *named waiting* state
  rather than a failure state.
- Waiting survives a fingerprint change. This is the subtle one: a decision is
  recorded against a task identity that includes the blocker fingerprint, so a
  new review comment arriving while we wait changes the fingerprint. If lookup
  keyed on the full identity, the decision would vanish and dispatch would
  resume — the exact retry loop this exists to prevent. Same reasoning as the
  BOU-1637 claim lookup.
- Waiting survives the death of the process that asked.
- An explicit human act — resolve or cancel — is the only thing that lifts it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_coordinator.models import DecisionOption
from agent_coordinator.service import TaskCoordinator
from agent_coordinator.store import JsonlClaimStore
from agentic_pr_dash import coordinator
from agentic_pr_dash.models import PRData, ReviewComment


BASE_TIME = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def _comment(comment_id: int) -> ReviewComment:
    return ReviewComment(
        id=comment_id,
        author="reviewer",
        body="please fix",
        created_at="2026-07-25T12:00:00Z",
    )


def _pr(**kwargs) -> PRData:
    base = {
        "number": 123,
        "title": "Fix PR",
        "branch": "feature/fix",
        "url": "https://github.com/Boundless-Studios/gaia-free/pull/123",
        "worktree_path": None,
        "failing_checks": ["unit"],
        "review_comments": [_comment(11)],
        "merge_state": "CLEAN",
    }
    base.update(kwargs)
    return PRData(**base)


def _options():
    return [
        DecisionOption(
            option_id="split-service",
            summary="Own service",
            trade_offs="Clean boundary; new deploy unit",
        ),
        DecisionOption(
            option_id="keep-module",
            summary="Keep in module",
            trade_offs="No new infra; implicit boundary",
        ),
    ]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    path = tmp_path / "claims.jsonl"
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(path))
    return path


def _raw(store_path) -> TaskCoordinator:
    return TaskCoordinator(JsonlClaimStore(store_path))


def _ask(store_path, pr, *, runtime="codex", session="s1", logical_key="boundary"):
    """Claim the PR and record a decision against it, as an executor would."""
    coord = _raw(store_path)
    handle = coordinator.claim_pr(
        pr, session_id=session, pid=None, agent=runtime, lease_seconds=900
    )
    return coord.request_decision(
        coordinator.task_identity_for_pr(pr),
        claim_id=handle.claim_id,
        logical_key=logical_key,
        category="architecture",
        question="Should turn orchestration own its own service boundary?",
        options=_options(),
        recommendation="keep-module",
        rationale="Reversible today",
        affected_scope=["backend/src/gaia/orchestrator"],
        fingerprint="evidence:v1",
        requesting_runtime=runtime,
        requesting_session_id=session,
        now=BASE_TIME,
    )


# ---------------------------------------------------------------------------
# Finding the pending decision
# ---------------------------------------------------------------------------


def test_no_decision_means_no_pending_decision(store):
    assert coordinator.pending_decision_for_pr(_pr()) is None


def test_a_requested_decision_is_found_for_the_pr(store):
    pr = _pr()
    record = _ask(store, pr)

    found = coordinator.pending_decision_for_pr(pr)
    assert found is not None
    assert found.request.decision_id == record.request.decision_id
    assert found.request.category == "architecture"


def test_pending_decision_survives_a_fingerprint_change(store):
    """A new comment while waiting must not make the question disappear.

    The decision is recorded against the identity carrying the round-1
    fingerprint. Keying lookup on the full identity would lose it the moment a
    new comment lands — and dispatch would resume into the same boundary.
    """
    pr = _pr()
    record = _ask(store, pr)

    moved = _pr(review_comments=[_comment(11), _comment(99)], failing_checks=[])
    assert coordinator.fingerprint_for_pr(moved) != coordinator.fingerprint_for_pr(pr)

    found = coordinator.pending_decision_for_pr(moved)
    assert found is not None
    assert found.request.decision_id == record.request.decision_id


def test_a_decision_on_another_pr_is_not_confused_for_this_one(store):
    _ask(store, _pr(number=123))

    other = _pr(number=456, url="https://github.com/o/r/pull/456")
    assert coordinator.pending_decision_for_pr(other) is None


def test_pending_decision_survives_the_death_of_the_asking_process(store):
    """The question outlives the executor that asked it."""
    pr = _pr()
    record = _ask(store, pr)

    # A brand-new coordinator over the same ledger — nothing in memory.
    found = coordinator.pending_decision_for_pr(pr)
    assert found is not None
    assert found.request.decision_id == record.request.decision_id


# ---------------------------------------------------------------------------
# Dispatch is paused, not failed
# ---------------------------------------------------------------------------


def test_pending_decision_blocks_dispatch(store):
    pr = _pr()
    _ask(store, pr)

    decision = coordinator.dispatch_decision_for_pr(pr)
    assert decision.should_dispatch is False
    assert decision.state == "waiting_human"


def test_waiting_state_is_not_a_failure_state(store):
    """Named distinctly so the loop can tell 'blocked on a human' from 'broken'."""
    pr = _pr()
    _ask(store, pr)

    decision = coordinator.dispatch_decision_for_pr(pr)
    assert decision.state == "waiting_human"
    assert decision.state != "manual_intervention"
    assert "decision" in decision.reason.lower()


def test_waiting_reason_names_the_decision(store):
    pr = _pr()
    record = _ask(store, pr)

    decision = coordinator.dispatch_decision_for_pr(pr)
    assert record.request.decision_id in decision.reason


def test_repeated_ticks_keep_returning_waiting_never_dispatch(store):
    """The anti-retry-loop property, stated directly."""
    pr = _pr()
    _ask(store, pr)

    for _ in range(5):
        assert coordinator.dispatch_decision_for_pr(pr).should_dispatch is False


# ---------------------------------------------------------------------------
# Only an explicit human act lifts the wait
# ---------------------------------------------------------------------------


def test_resolution_lifts_the_wait(store):
    pr = _pr()
    record = _ask(store, pr)

    _raw(store).resolve_decision(
        record.request.decision_id,
        request_fingerprint="evidence:v1",
        human_actor="ilya",
        selected_option_id="split-service",
        now=BASE_TIME,
    )

    assert coordinator.pending_decision_for_pr(pr) is None
    assert coordinator.dispatch_decision_for_pr(pr).state != "waiting_human"


def test_cancellation_lifts_the_wait(store):
    pr = _pr()
    record = _ask(store, pr)

    _raw(store).cancel_decision(
        record.request.decision_id, actor="ilya", reason="moot", now=BASE_TIME
    )

    assert coordinator.pending_decision_for_pr(pr) is None


def test_time_alone_never_lifts_the_wait(store):
    """No timeout may answer the question — the property BOU-2039 guarantees."""
    from datetime import timedelta

    pr = _pr()
    _ask(store, pr)

    far_future = BASE_TIME + timedelta(days=3650)
    assert coordinator.pending_decision_for_pr(pr) is not None
    assert (
        coordinator.dispatch_decision_for_pr(pr, now=far_future).should_dispatch
        is False
    )


def test_resolved_decision_carries_the_human_direction_for_the_resumed_run(store):
    """Redispatch after resolution must be able to cite what was decided."""
    pr = _pr()
    record = _ask(store, pr)
    _raw(store).resolve_decision(
        record.request.decision_id,
        request_fingerprint="evidence:v1",
        human_actor="ilya",
        selected_option_id="split-service",
        now=BASE_TIME,
    )

    resumable = coordinator.resolved_decision_for_pr(pr)
    assert resumable is not None
    assert resumable.resolution.selected_option_id == "split-service"
    assert resumable.resolution.human_actor == "ilya"


# ---------------------------------------------------------------------------
# Runtime neutrality
# ---------------------------------------------------------------------------


def test_new_feedback_while_waiting_does_not_re_enable_dispatch(store):
    """The unconditional-defer property, at the coordinator layer.

    `worktree_check` normally lets `new_feedback` override a non-dispatchable
    coordinator state (that override exists to stop a live owner's work being
    stolen). A pending decision must not be overridable that way — see the
    worktree_check guard.
    """
    pr = _pr()
    _ask(store, pr)

    noisier = _pr(review_comments=[_comment(11), _comment(77), _comment(88)])
    assert coordinator.dispatch_decision_for_pr(noisier).state == "waiting_human"
    assert coordinator.dispatch_decision_for_pr(noisier).should_dispatch is False


def test_loop_treats_a_recorded_decision_as_not_a_failure(store, monkeypatch):
    """An executor that stopped to ask a human is not a failed executor."""
    from agentic_pr_dash import loop

    pr = _pr()
    _ask(store, pr)

    assert (
        loop._decision_requested_during_dispatch(
            ".", 123, repo_slug="Boundless-Studios/gaia-free"
        )
        is True
    )


def test_loop_sees_no_decision_when_none_was_recorded(store):
    from agentic_pr_dash import loop

    assert (
        loop._decision_requested_during_dispatch(
            ".", 123, repo_slug="Boundless-Studios/gaia-free"
        )
        is False
    )


def test_probe_without_a_slug_returns_false_rather_than_guessing(store):
    """No slug means we cannot key the task identity — do not guess.

    Guessing via auto-detection is what put a subprocess on this path.
    """
    from agentic_pr_dash import loop

    _ask(store, _pr())
    assert loop._decision_requested_during_dispatch(".", 123) is False
    assert loop._decision_requested_during_dispatch(".", 123, repo_slug="") is False


def test_probe_misses_the_decision_if_the_repo_slug_disagrees(store):
    """``task_identity_for_pr`` keys on github:<owner>/<repo>#<n>."""
    from agentic_pr_dash import loop

    _ask(store, _pr())
    assert (
        loop._decision_requested_during_dispatch(".", 123, repo_slug="other/repo")
        is False
    )


def test_decision_probe_makes_no_subprocess_call(store, monkeypatch):
    """The probe runs on every failed dispatch — it must not shell out.

    PR #109 review caught the real hole here: an earlier cut passed a url-less
    probe, which falls through ``_repo_slug_for_pr`` to ``Config.resolved_repo``
    and, when ``[project].repo`` is unset (the documented default), into
    ``_detect_repo`` -> ``gh repo view`` with a 15s timeout. The previous version
    of this test monkeypatched ``_repo_slug_for_pr`` and so never exercised that
    path — it masked exactly the defect it claimed to guard. Now the slug is
    passed in and NOTHING is stubbed except the forbidden call itself.
    """
    import subprocess as _sp

    from agentic_pr_dash import loop

    def _forbidden(*a, **k):
        raise AssertionError("decision probe must not spawn a subprocess")

    monkeypatch.setattr(_sp, "run", _forbidden)
    _ask(store, _pr())

    assert (
        loop._decision_requested_during_dispatch(
            ".", 123, repo_slug="Boundless-Studios/gaia-free"
        )
        is True
    )


def test_fallback_is_not_run_when_the_primary_recorded_a_decision(store, monkeypatch):
    """PR #109 review: the fallback would attack the same unresolved boundary.

    Worse, a fallback that exits 0 makes ``serviced`` true, so the caller never
    probes the ledger and proceeds to completion with the question unanswered.
    """
    from agentic_pr_dash import loop

    _ask(store, _pr())
    ran: list[str] = []

    def _try(executor, prompt, cwd):
        # BOU-2417: _try_run now also returns the executor's output tail.
        ran.append(executor)
        return (1, "exit 1", "") if executor == "primary" else (0, "", "")

    monkeypatch.setattr(loop, "_try_run", _try)

    serviced, errors, _output = loop._dispatch_with_fallback(
        "primary", "fallback", "p", ".", 123,
        repo_slug="Boundless-Studios/gaia-free",
    )

    assert ran == ["primary"], "fallback must not run after a recorded decision"
    assert serviced is False
    assert "primary" in errors


def test_fallback_still_runs_on_an_ordinary_primary_failure(store, monkeypatch):
    """The decision check must not break the normal fallback chain."""
    from agentic_pr_dash import loop

    ran: list[str] = []

    def _try(executor, prompt, cwd):
        # BOU-2417: _try_run now also returns the executor's output tail.
        ran.append(executor)
        return (1, "exit 1", "") if executor == "primary" else (0, "", "")

    monkeypatch.setattr(loop, "_try_run", _try)

    serviced, _, _output = loop._dispatch_with_fallback(
        "primary", "fallback", "p", ".", 123,
        repo_slug="Boundless-Studios/gaia-free",
    )

    assert ran == ["primary", "fallback"]
    assert serviced is True


def test_resolved_direction_is_appended_to_the_redispatched_prompt(store, monkeypatch):
    """PR #109 review: a redispatched executor must be told what was decided.

    Otherwise it runs the original prompt, reaches the same boundary, and asks
    the same question again — the loop the protocol exists to end.
    """
    from agentic_pr_dash import loop

    pr = _pr()
    record = _ask(store, pr)
    _raw(store).resolve_decision(
        record.request.decision_id,
        request_fingerprint="evidence:v1",
        human_actor="ilya",
        selected_option_id="split-service",
        rationale="the boundary is load-bearing now",
        now=BASE_TIME,
    )

    out = loop._append_resolved_direction(
        "ORIGINAL", ".", 123, "Boundless-Studios/gaia-free", "s-loop"
    )

    assert out.startswith("ORIGINAL")
    assert record.request.decision_id in out
    assert "split-service" in out
    assert "ilya" in out
    assert "load-bearing" in out
    assert "do not ask again" in out.lower()


def test_no_direction_is_appended_when_nothing_was_decided(store):
    from agentic_pr_dash import loop

    _ask(store, _pr())  # still waiting, not resolved
    assert (
        loop._append_resolved_direction(
            "ORIGINAL", ".", 123, "Boundless-Studios/gaia-free", "s-loop"
        )
        == "ORIGINAL"
    )


def test_resume_is_recorded_when_the_direction_is_consumed(store, monkeypatch):
    """Closing the coordinator's resume protocol, not just reading it."""
    from agent_coordinator.models import DecisionState
    from agentic_pr_dash import loop

    pr = _pr()
    record = _ask(store, pr)
    _raw(store).resolve_decision(
        record.request.decision_id,
        request_fingerprint="evidence:v1",
        human_actor="ilya",
        selected_option_id="split-service",
        now=BASE_TIME,
    )

    loop._append_resolved_direction(
        "ORIGINAL", ".", 123, "Boundless-Studios/gaia-free", "s-loop"
    )

    assert (
        coordinator.decision_by_id_for_pr(pr, record.request.decision_id).state
        is DecisionState.RESUMED
    )


def test_decision_block_reason_gates_the_shared_dispatch_path(store):
    """PR #109 review: manual dashboard actions must honour the wait too."""
    pr = _pr()
    record = _ask(store, pr)

    reason = coordinator.decision_block_reason(pr)
    assert reason is not None
    assert record.request.decision_id in reason

    _raw(store).cancel_decision(
        record.request.decision_id, actor="ilya", reason="moot", now=BASE_TIME
    )
    assert coordinator.decision_block_reason(pr) is None


def test_loop_decision_probe_is_false_on_lookup_error(store, monkeypatch):
    """A bad read must not suppress a real failure.

    Silently swallowing a genuine executor failure is worse than an extra
    failure-streak entry, so the probe fails closed.
    """
    from agentic_pr_dash import loop

    def _boom(_pr):
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(coordinator, "pending_decision_for_pr", _boom)
    assert loop._decision_requested_during_dispatch(".", 123) is False


def test_loop_decision_probe_handles_a_missing_pr_number(store):
    from agentic_pr_dash import loop

    assert loop._decision_requested_during_dispatch(".", None) is False


# ---------------------------------------------------------------------------
# _check_worktree: the real path that decides whether to dispatch
#
# Drives the REAL engine against the REAL coordinator store; only the
# read-only PR/marker boundaries are stubbed. Harness mirrors
# tests/test_defer_hides_blockers.py.
# ---------------------------------------------------------------------------


def _wt_pr(worktree):
    from agentic_pr_dash.models import PRStatus

    return PRData(
        number=42,
        title="needs review",
        branch="feature/x",
        url="https://github.com/Boundless-Studios/gaia-free/pull/42",
        worktree_path=str(worktree),
        status=PRStatus.HAS_COMMENTS,
        review_comments=[_comment(7)],
    )


def _stub_boundaries(monkeypatch, pr):
    import os as _os

    from agentic_pr_dash._maintenance import markers as _markers_mod
    from agentic_pr_dash._maintenance import pr_state as _pr_state_mod
    from agentic_pr_dash._maintenance import worktrees as _worktrees_mod

    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: pr)
    monkeypatch.setattr(_pr_state_mod, "_unresolved_review_threads", lambda n, cwd: [])
    monkeypatch.setattr(
        _worktrees_mod, "_live_independent_owner_sessions", lambda paths, sid: {}
    )
    monkeypatch.setattr(
        _markers_mod, "_touch_owner_heartbeat", lambda cwd, sid, work: None
    )
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda cwd: None)
    return _os


def _ask_for(store_path, pr, *, session="s-exec"):
    coord = _raw(store_path)
    handle = coordinator.claim_pr(
        pr, session_id=session, pid=None, agent="codex", lease_seconds=900
    )
    return coord.request_decision(
        coordinator.task_identity_for_pr(pr),
        claim_id=handle.claim_id,
        logical_key="boundary",
        category="architecture",
        question="Split the service?",
        options=_options(),
        recommendation="keep-module",
        rationale="Reversible today",
        affected_scope=["backend/src"],
        fingerprint="evidence:v1",
        requesting_runtime="codex",
        requesting_session_id=session,
        now=BASE_TIME,
    )


def test_check_worktree_defers_while_a_decision_is_pending(
    store, monkeypatch, tmp_path
):
    from agentic_pr_dash import maintenance_check

    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _wt_pr(worktree)
    _stub_boundaries(monkeypatch, pr)
    _ask_for(store, pr)

    code, text = maintenance_check._check_worktree(str(worktree), "sess-self", claim=True)

    # Exit 0 = defer, not 10 (service/dispatch).
    assert code == 0, text
    assert "waiting_human" in text


def test_check_worktree_defers_even_when_the_caller_owns_the_claim(
    store, monkeypatch, tmp_path
):
    """`self_owned` normally converts a defer into SERVICE (exit 10).

    A pending decision must outrank that: the owning session re-entering does
    not mean the human answered.
    """
    from agentic_pr_dash import maintenance_check

    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _wt_pr(worktree)
    _stub_boundaries(monkeypatch, pr)
    _ask_for(store, pr, session="sess-self")

    code, text = maintenance_check._check_worktree(str(worktree), "sess-self", claim=True)

    assert code == 0, text
    assert "waiting_human" in text


def test_check_worktree_services_again_once_the_decision_is_resolved(
    store, monkeypatch, tmp_path
):
    """The wait must be liftable — the gate cannot become permanent."""
    from agentic_pr_dash import maintenance_check

    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _wt_pr(worktree)
    _stub_boundaries(monkeypatch, pr)
    record = _ask_for(store, pr, session="sess-self")

    _raw(store).resolve_decision(
        record.request.decision_id,
        request_fingerprint="evidence:v1",
        human_actor="ilya",
        selected_option_id="split-service",
        now=BASE_TIME,
    )

    code, text = maintenance_check._check_worktree(str(worktree), "sess-self", claim=True)
    assert code == 10, text


@pytest.mark.parametrize("runtime", ["codex", "claude"])
def test_either_runtime_can_raise_the_decision(store, runtime):
    pr = _pr()
    record = _ask(store, pr, runtime=runtime, session=f"s-{runtime}")

    found = coordinator.pending_decision_for_pr(pr)
    assert found is not None
    assert found.request.requesting_runtime == runtime
    assert record.request.requesting_runtime == runtime
    assert coordinator.dispatch_decision_for_pr(pr).should_dispatch is False
