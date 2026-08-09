"""Pin the stop-gate's Phase B payload wording per blocking-reason category (BOU-2845).

The engine's ``_cmd_finalize`` prints ``PR not settled: <work>`` whenever the
review-settlement gate blocks. BOU-2845: when ``work`` is entirely *local review*
(coordinator required actions, missing local/backstop slots, ``head_drift``) the
payload must tell the agent to commit locally and re-run the local review — NOT to
push per review round (which fired one full CI run per round on BOU-2831/PR #3106).
When any *CI/remote* item is present, the push instruction stays.

Both variants must keep the exact ``PR not settled:`` prefix so gaia's Stop-hook
adapter (``stop-pr-maintenance.py::_BLOCKING_SIGNALS``) classifies the block as real
work and never as an observation outage.
"""
from __future__ import annotations

import pytest

from agentic_pr_dash.maintenance_check import _render_unsettled_message

# Local-review items — resolved by a local commit + re-running review_local.py.
LOCAL_REVIEW_ITEMS = [
    "verify_fix",
    "address_p0",
    "address_p1",
    "evaluate_p2",
    "fix_p2",
    "prove_p2",
    "fix_reproduced_p2",
    "local:1",
    "local:2",
    "backstop:1",
    "head_drift",
]

# CI/remote items — the fix must reach the remote PR.
CI_REMOTE_ITEMS = [
    "ci_not_successful",
    "ci_unavailable",
    "ci_pending",
    "ci_failure",
    "changes_requested",
    "not_mergeable",
    "merge_conflict",
    "review_comments",
]

# Observation errors — not actionable by a push; must not silently classify as local.
OBSERVATION_ITEMS = [
    "repository_unknown",
    "ledger_repository_mismatch",
    "head_unknown",
    "stabilization_pending",
    "unstable_observation",
]


def _assert_local_review_guidance(message: str) -> None:
    """The local-review variant tells the agent to hold the push until slots clean."""
    lowered = message.lower()
    assert message.startswith("PR not settled:"), message
    assert "local review" in lowered, message
    assert "commit" in lowered, message
    assert "re-run" in lowered, message
    assert "push" in lowered, message
    # The push guidance is conditional — "push once slots are clean", never "push
    # per review round".
    assert "once the local review slots record clean" in lowered, message
    assert "do not push per review round" in lowered, message


def _assert_push_guidance(message: str) -> None:
    """The CI/remote variant keeps the explicit push-to-EXISTING-branch instruction."""
    assert message.startswith("PR not settled:"), message
    assert "push" in message, message
    assert "EXISTING branch" in message, message


@pytest.mark.parametrize("item", LOCAL_REVIEW_ITEMS)
def test_single_local_review_item_renders_local_guidance(item):
    message = _render_unsettled_message([item])
    _assert_local_review_guidance(message)
    assert item in message, message


def test_missing_local_slots_alone_render_local_guidance():
    message = _render_unsettled_message(["local:1", "backstop:1"])
    _assert_local_review_guidance(message)
    assert "local:1" in message
    assert "backstop:1" in message


def test_head_drift_alone_render_local_guidance():
    """head_drift is a consequence of pushing — the answer is re-review, not push."""
    message = _render_unsettled_message(["head_drift"])
    _assert_local_review_guidance(message)
    assert "head_drift" in message


@pytest.mark.parametrize("item", CI_REMOTE_ITEMS)
def test_single_ci_remote_item_renders_push_guidance(item):
    message = _render_unsettled_message([item])
    _assert_push_guidance(message)
    assert item in message, message


@pytest.mark.parametrize("item", OBSERVATION_ITEMS)
def test_observation_error_renders_push_guidance(item):
    """An observation error is not local-review-unsettled — it must never get the
    'commit locally and hold the push' guidance."""
    message = _render_unsettled_message([item])
    _assert_push_guidance(message)


@pytest.mark.parametrize("ci_item", CI_REMOTE_ITEMS)
@pytest.mark.parametrize("local_item", ["verify_fix", "evaluate_p2"])
def test_mixed_work_renders_push_guidance_ci_wins(local_item, ci_item):
    """Any CI/remote item forces the push message — a genuinely remote blocker never
    gets local-only guidance."""
    message = _render_unsettled_message([local_item, ci_item])
    _assert_push_guidance(message)
    assert local_item in message
    assert ci_item in message


def test_empty_work_never_renders_local_guidance():
    """An empty work list is not a blocker at all — _cmd_finalize only calls the
    renderer on the unsettled branch, but the renderer must not claim local-review
    guidance for nothing."""
    with pytest.raises(ValueError):
        _render_unsettled_message([])
