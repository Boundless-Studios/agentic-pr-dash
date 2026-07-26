"""BOU-2490: who acted on a PR, and could they write code?

Five surfaces in this package are all called "PR maintenance". Two of them run an
executor and push (the in-session agent, and the detached loop); three cannot (the
Stop gate, the await waiter, and the dashboard, which only queues a handoff).
Nothing recorded that distinction, so:

* the dashboard's "queued a work order" and the loop's "ran ``codex --full-auto``
  and pushed" both wrote ``kind="dispatch"`` with ``session_id=None``;
* both logged under the same ``[agentic-pr-dash]`` prefix;
* the ownership ledger stored *who* but not *whether they could execute*, so a
  dashboard claim that can never be fulfilled outranked the loop that could
  (BOU-2491);
* loop-authored commits were indistinguishable from the human's, because the
  executor commits under the user's git identity.

A session that found commits it did not write therefore had to guess, and guessed
that a daemon had taken over its work. These tests pin the attribution that makes
that question answerable instead of inferable.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from unittest.mock import patch

from agentic_pr_dash import loop
from agentic_pr_dash.models import (
    EXECUTING_ACTORS,
    MaintenanceActor,
    can_execute,
)
from agentic_pr_dash.observability.event_store import ObservabilityEvent


# ── Layer 0: the vocabulary ────────────────────────────────────────────────────

def test_only_the_session_and_the_loop_can_execute() -> None:
    """Capability is the axis that matters; pin exactly which actors have it."""
    assert EXECUTING_ACTORS == {
        MaintenanceActor.SESSION,
        MaintenanceActor.LOOP_EXECUTOR,
    }
    assert can_execute(MaintenanceActor.LOOP_EXECUTOR) is True
    assert can_execute(MaintenanceActor.SESSION) is True
    assert can_execute(MaintenanceActor.DASHBOARD_QUEUE) is False
    assert can_execute(MaintenanceActor.STOP_GATE) is False
    assert can_execute(MaintenanceActor.WAITER) is False


def test_can_execute_accepts_the_string_form() -> None:
    """Claim metadata is dict[str,str] and event rows are JSON — both round-trip."""
    assert can_execute("loop-executor") is True
    assert can_execute("dashboard-queue") is False


def test_unknown_or_missing_actor_is_not_treated_as_coverage() -> None:
    """Fail closed: the caller is asking "does someone else have this covered?"

    Wrongly answering yes strands a red PR with nobody working it (BOU-1789), so an
    actor we cannot classify must never count as an executing owner.
    """
    assert can_execute(None) is False
    assert can_execute("") is False
    assert can_execute("some-future-actor") is False


# ── Layer 1: the event log ─────────────────────────────────────────────────────

def test_event_carries_actor() -> None:
    ev = ObservabilityEvent(
        ts=datetime.now(timezone.utc),
        kind="dispatch",
        actor=MaintenanceActor.LOOP_EXECUTOR.value,
    )
    assert ev.actor == "loop-executor"


def test_legacy_event_row_without_actor_still_parses() -> None:
    """Rows written before BOU-2490 must not break the reader.

    `events.jsonl` is append-only and already megabytes long on live machines; a
    required field would make every historical row unreadable.
    """
    legacy = (
        '{"ts":"2026-07-26T15:10:42.236693Z","repo":"/x","pr_number":2831,'
        '"kind":"dispatch","session_id":null,"details":{}}'
    )
    ev = ObservabilityEvent.model_validate_json(legacy)
    assert ev.actor is None
    assert ev.kind == "dispatch"


def test_loop_emitter_stamps_loop_executor_and_a_session_id(tmp_path) -> None:
    """The loop's dispatch event must be distinguishable from the dashboard's.

    Both are ``kind="dispatch"``; only ``actor`` tells them apart. ``session_id``
    was previously computed on this path and then dropped.
    """
    captured: list[ObservabilityEvent] = []

    class _Store:
        def append(self, event):
            captured.append(event)

    with patch("agentic_pr_dash.observability.event_store.get_event_store",
               return_value=_Store()):
        loop._emit_loop_event(str(tmp_path), "dispatch", 2831, {"status": "x"})

    assert len(captured) == 1, "loop emit must not be swallowed"
    assert captured[0].actor == MaintenanceActor.LOOP_EXECUTOR.value
    assert captured[0].session_id, "loop dispatch must record which loop ran it"


# ── Layer 2: ownership claims carry capability ─────────────────────────────────

def test_dashboard_claim_is_marked_non_executing() -> None:
    """The dashboard claims PRs it can never fix — the claim must say so.

    It writes a bead, a state file and a MAINTENANCE_HANDOFF.md, then claims. But
    nothing in the package ever reads that handoff back to execute it, so treating
    the claim as coverage leaves the PR worked by nobody (BOU-2491).
    """
    from agentic_pr_dash import coordinator

    captured: dict = {}

    class _Rec:
        claim_id = "c1"
        lease_epoch = 1

    class _Coord:
        def claim_task(self, _identity, owner, lease_seconds):
            captured["owner"] = owner
            return _Rec()

    class _PR:
        number = 1
        worktree_path = "/wt"
        branch = "b"

    with patch.object(coordinator, "_coordinator", return_value=_Coord()), \
         patch.object(coordinator, "task_identity_for_pr", return_value="t"):
        coordinator.claim_pr(
            _PR(), session_id="s", pid=None, agent="a", lease_seconds=60,
            actor=MaintenanceActor.DASHBOARD_QUEUE,
        )

    assert captured["owner"].metadata["actor"] == "dashboard-queue"
    assert captured["owner"].metadata["can_execute"] == "false"


def test_executing_claim_is_marked_executable() -> None:
    from agentic_pr_dash import coordinator

    captured: dict = {}

    class _Rec:
        claim_id = "c1"
        lease_epoch = 1

    class _Coord:
        def claim_task(self, _identity, owner, lease_seconds):
            captured["owner"] = owner
            return _Rec()

    class _PR:
        number = 1
        worktree_path = "/wt"
        branch = "b"

    with patch.object(coordinator, "_coordinator", return_value=_Coord()), \
         patch.object(coordinator, "task_identity_for_pr", return_value="t"):
        coordinator.claim_pr(
            _PR(), session_id="s", pid=123, agent="a", lease_seconds=60,
            actor=MaintenanceActor.LOOP_EXECUTOR,
        )

    assert captured["owner"].metadata["can_execute"] == "true"


# ── Layer 3: git history distinguishes loop commits ────────────────────────────

def test_executor_env_stamps_the_loop_committer_identity() -> None:
    """`git log --format='%cn'` is the durable, out-of-band attribution record.

    Author is deliberately untouched so blame and the contribution graph still
    point at the human; only the committer records the machine.
    """
    env = loop._executor_env()
    assert env["GIT_COMMITTER_NAME"] == loop.LOOP_COMMITTER_NAME == "apd-loop-executor"
    assert env["GIT_COMMITTER_EMAIL"] == loop.LOOP_COMMITTER_EMAIL
    assert env["AGENTIC_PR_DASH_ACTOR"] == MaintenanceActor.LOOP_EXECUTOR.value
    assert "GIT_AUTHOR_NAME" not in env, "the human must remain the commit AUTHOR"


def test_executor_env_inherits_the_ambient_environment() -> None:
    """The executor needs PATH, HOME, auth tokens — a bare env would break it."""
    env = loop._executor_env()
    for key in ("PATH", "HOME"):
        if key in os.environ:
            assert env[key] == os.environ[key]


def test_run_executor_passes_the_committer_env_to_the_subprocess() -> None:
    """The env must actually reach the dispatched process, not just be computed."""
    seen: dict = {}

    class _Completed:
        returncode = 0

    def _fake_run(parts, cwd=None, env=None):
        seen["env"] = env
        seen["parts"] = parts
        return _Completed()

    with patch.object(subprocess, "run", _fake_run):
        rc = loop._run_executor("codex exec --full-auto {prompt}", "do it", "/wt")

    assert rc == 0
    assert seen["env"]["GIT_COMMITTER_NAME"] == "apd-loop-executor"
    assert seen["parts"][-1] == "do it", "prompt must stay a single argv element"


# ── Layer 4: the surfaces say which one they are ───────────────────────────────

def test_loop_log_prefix_is_specific_to_the_loop() -> None:
    """A shared `[agentic-pr-dash]` prefix cannot answer "which daemon logged this"."""
    src = (loop.__file__)
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    assert "[apd:loop]" in text
    assert "[agentic-pr-dash]" not in text, (
        "loop.py must not log under the ambiguous shared prefix"
    )


def test_stop_gate_block_discloses_the_concurrent_executor() -> None:
    """The gate writes no code but shares a worktree with a daemon that does.

    Saying so — with the git command to check — is what stops a session from
    inferring a culprit for commits it did not write.
    """
    from agentic_pr_dash._maintenance import stop_gate

    block = stop_gate._build_stop_block([("(no worktree) x", "PR #1 needs work")])
    assert "never edits, commits or pushes" in block
    assert "pr-maintenance-loop" in block
    assert "apd-loop-executor" in block
    assert "%cn" in block, "must give the concrete attribution command"
