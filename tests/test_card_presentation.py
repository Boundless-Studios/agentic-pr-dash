"""Pin the NEW card-presentation contract.

These tests describe the desired API/rendering behaviour that does NOT yet
exist.  They are expected to FAIL on first run (RED phase).  Do NOT
implement any production code alongside this file.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from datetime import timedelta

from agentic_pr_dash.models import (
    MaintenanceState,
    MaintenanceStatus,
    PRStatus,
    ReviewComment,
    ThreadDecision,
    WorktreeCard,
    humanize_relative,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_card(**kwargs) -> WorktreeCard:
    defaults = dict(id="wt-foo", worktree_name="foo", branch="bou-1551-test")
    defaults.update(kwargs)
    return WorktreeCard(**defaults)


def _maintenance(state: MaintenanceStatus, **kwargs) -> MaintenanceState:
    return MaintenanceState(
        pr_number=99,
        branch="bou-1551-test",
        worktree_path="/tmp/wt/foo",
        state=state,
        **kwargs,
    )


def _review_comments(n: int) -> list[ReviewComment]:
    return [
        ReviewComment(
            id=i,
            author="somereviewer",
            body="UNIQUE_COMMENT_BODY_XYZ",
            path="some/file.py",
            created_at="2026-06-11T10:00:00Z",
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# 1. WorktreeCard.agent_state property
# ---------------------------------------------------------------------------

def test_agent_state_minimal_card_returns_clean():
    """A default-constructed minimal card must return 'clean' and never raise."""
    card = _minimal_card()
    assert card.agent_state == "clean"  # type: ignore[attr-defined]


def test_agent_state_bare_statuses():
    """Each PRStatus maps to its baseline agent_state when no maintenance is set."""
    cases = [
        (PRStatus.CLEAN, "clean"),
        (PRStatus.OBSERVATION_UNAVAILABLE, "observation_unavailable"),
        (PRStatus.NO_PR, "no_pr"),
        (PRStatus.CI_FAILING, "ci_failing"),
        (PRStatus.CI_PENDING, "ci_pending"),
        (PRStatus.HAS_COMMENTS, "awaiting_fixes"),
        (PRStatus.CI_AND_COMMENTS, "awaiting_fixes"),
        (PRStatus.MERGE_CONFLICT, "merge_conflict"),
        (PRStatus.AGENT_WORKING, "working"),
        (PRStatus.AGENT_FAILED, "failed"),
    ]
    for status, expected in cases:
        card = _minimal_card(status=status)
        result = card.agent_state  # type: ignore[attr-defined]
        assert result == expected, f"status={status.value}: expected {expected!r}, got {result!r}"


def test_agent_state_maintenance_queued_wins_over_working():
    """status=AGENT_WORKING + maintenance QUEUED → 'queued' (queue signal takes precedence)."""
    card = _minimal_card(
        status=PRStatus.AGENT_WORKING,
        maintenance=_maintenance(MaintenanceStatus.QUEUED),
    )
    assert card.agent_state == "queued"  # type: ignore[attr-defined]


def test_agent_state_maintenance_signaled_wins_over_working():
    """status=AGENT_WORKING + maintenance SIGNALED → 'queued'."""
    card = _minimal_card(
        status=PRStatus.AGENT_WORKING,
        maintenance=_maintenance(MaintenanceStatus.SIGNALED),
    )
    assert card.agent_state == "queued"  # type: ignore[attr-defined]


def test_agent_state_maintenance_running_wins_over_has_comments():
    """status=HAS_COMMENTS + maintenance RUNNING → 'working'."""
    card = _minimal_card(
        status=PRStatus.HAS_COMMENTS,
        maintenance=_maintenance(MaintenanceStatus.RUNNING),
    )
    assert card.agent_state == "working"  # type: ignore[attr-defined]


def test_agent_state_maintenance_waiting_for_push():
    """maintenance WAITING_FOR_PUSH → 'working'."""
    card = _minimal_card(
        status=PRStatus.HAS_COMMENTS,
        maintenance=_maintenance(MaintenanceStatus.WAITING_FOR_PUSH),
    )
    assert card.agent_state == "working"  # type: ignore[attr-defined]


def test_agent_state_maintenance_failed_wins():
    """maintenance FAILED → 'failed' (overrides status-level state)."""
    card = _minimal_card(
        status=PRStatus.CLEAN,
        maintenance=_maintenance(MaintenanceStatus.FAILED),
    )
    assert card.agent_state == "failed"  # type: ignore[attr-defined]


def test_agent_state_agent_failure_reason_wins():
    """agent_failure_reason set on a CLEAN card → 'failed'."""
    card = _minimal_card(status=PRStatus.CLEAN, agent_failure_reason="boom")
    assert card.agent_state == "failed"  # type: ignore[attr-defined]


def test_agent_state_agent_failed_status():
    """status=AGENT_FAILED alone → 'failed'."""
    card = _minimal_card(status=PRStatus.AGENT_FAILED)
    assert card.agent_state == "failed"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 1b. WorktreeCard.agent_state_label property
# ---------------------------------------------------------------------------

_ALL_STATES = [
    "failed", "working", "waiting", "ready_cleanup", "queued", "awaiting_fixes",
    "ci_failing", "ci_pending", "merge_conflict", "observation_unavailable",
    "no_pr", "clean",
]


def test_agent_state_label_non_empty_and_distinct():
    """Each state must map to a non-empty, distinct human label."""
    labels: dict[str, str] = {}
    for state_val in _ALL_STATES:
        # Build a card that produces this agent_state
        if state_val == "failed":
            card = _minimal_card(status=PRStatus.AGENT_FAILED)
        elif state_val == "working":
            card = _minimal_card(status=PRStatus.AGENT_WORKING)
        elif state_val == "waiting":
            card = _minimal_card(status=PRStatus.AGENT_WAITING)
        elif state_val == "ready_cleanup":
            card = _minimal_card(status=PRStatus.READY_CLEANUP)
        elif state_val == "queued":
            card = _minimal_card(
                status=PRStatus.AGENT_WORKING,
                maintenance=_maintenance(MaintenanceStatus.QUEUED),
            )
        elif state_val == "awaiting_fixes":
            card = _minimal_card(status=PRStatus.HAS_COMMENTS)
        elif state_val == "ci_failing":
            card = _minimal_card(status=PRStatus.CI_FAILING)
        elif state_val == "ci_pending":
            card = _minimal_card(status=PRStatus.CI_PENDING)
        elif state_val == "merge_conflict":
            card = _minimal_card(status=PRStatus.MERGE_CONFLICT)
        elif state_val == "observation_unavailable":
            card = _minimal_card(status=PRStatus.OBSERVATION_UNAVAILABLE)
        elif state_val == "no_pr":
            card = _minimal_card(status=PRStatus.NO_PR)
        else:  # clean
            card = _minimal_card(status=PRStatus.CLEAN)

        label = card.agent_state_label  # type: ignore[attr-defined]
        assert isinstance(label, str) and label, (
            f"agent_state_label for '{state_val}' must be a non-empty str"
        )
        labels[state_val] = label

    # All labels must be distinct
    assert len(set(labels.values())) == len(_ALL_STATES), (
        f"Labels not all distinct: {labels}"
    )


# ---------------------------------------------------------------------------
# 2. WorktreeCard.started_at / PRData.created_at / worktree_started_at
# ---------------------------------------------------------------------------

def test_pr_data_has_created_at_field():
    """PRData must expose a created_at: str field."""
    from agentic_pr_dash.models import PRData  # noqa: PLC0415
    pr = PRData(
        number=123,
        title="Test PR",
        branch="bou-1551-test",
        url="https://github.com/Boundless-Studios/gaia-free/pull/123",
        created_at="2026-06-10T12:00:00Z",
    )
    assert pr.created_at == "2026-06-10T12:00:00Z"  # type: ignore[attr-defined]


def test_worktree_card_started_at_from_pr_created_at():
    """A card built with pr_created_at exposes started_at as a matching datetime."""
    card = _minimal_card(pr_created_at="2026-06-10T12:00:00Z")  # type: ignore[call-arg]
    started = card.started_at  # type: ignore[attr-defined]
    assert isinstance(started, datetime)
    assert started.year == 2026
    assert started.month == 6
    assert started.day == 10
    assert started.hour == 12


def test_worktree_started_at_existing_dir(tmp_path):
    """worktree_started_at(path) returns a datetime for an existing directory."""
    try:
        from agentic_pr_dash.models import worktree_started_at  # type: ignore[attr-defined]
    except ImportError:
        pytest.fail("agentic_pr_dash.models must expose worktree_started_at")

    result = worktree_started_at(str(tmp_path))
    assert isinstance(result, datetime), (
        f"worktree_started_at on existing dir must return datetime, got {result!r}"
    )


def test_worktree_started_at_missing_dir():
    """worktree_started_at(path) returns None for a non-existent path."""
    try:
        from agentic_pr_dash.models import worktree_started_at  # type: ignore[attr-defined]
    except ImportError:
        pytest.fail("agentic_pr_dash.models must expose worktree_started_at")

    result = worktree_started_at("/tmp/__nonexistent_path_bou1551__")
    assert result is None, f"Expected None for missing path, got {result!r}"


# ---------------------------------------------------------------------------
# 2b. Relative-age labels ("1d 5h ago", "1m 30s ago")
# ---------------------------------------------------------------------------

def test_humanize_relative_days_and_hours():
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    dt = now - timedelta(days=1, hours=5, minutes=40)
    assert humanize_relative(dt, now=now) == "1d 5h ago"


def test_humanize_relative_minutes_and_seconds():
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    dt = now - timedelta(minutes=1, seconds=30)
    assert humanize_relative(dt, now=now) == "1m 30s ago"


def test_humanize_relative_hours_and_minutes():
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    dt = now - timedelta(hours=3, minutes=8, seconds=12)
    assert humanize_relative(dt, now=now) == "3h 8m ago"


def test_humanize_relative_seconds_only():
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    assert humanize_relative(now - timedelta(seconds=12), now=now) == "12s ago"


def test_humanize_relative_future_is_just_now():
    now = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)
    assert humanize_relative(now + timedelta(minutes=5), now=now) == "just now"


def test_started_at_label_is_relative():
    """started_at_label renders the relative form, not an absolute timestamp."""
    card = _minimal_card(pr_created_at="2026-06-10T12:00:00Z")  # type: ignore[call-arg]
    label = card.started_at_label  # type: ignore[attr-defined]
    assert label.endswith("ago") or label == "just now"
    assert "2026-" not in label  # no absolute date leaking through


# ---------------------------------------------------------------------------
# 3. Template rendering helpers
# ---------------------------------------------------------------------------

def _make_jinja_env():
    """Build a Jinja2 Environment pointing at the real templates directory."""
    from jinja2 import Environment, FileSystemLoader  # noqa: PLC0415
    import agentic_pr_dash  # noqa: PLC0415

    pkg_dir = Path(agentic_pr_dash.__file__).parent
    templates_dir = pkg_dir / "templates"
    env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=False)

    from agentic_pr_dash.app import status_label, build_columns  # noqa: PLC0415
    env.filters["status_label"] = status_label
    return env, build_columns


def _render_board(cards: list[WorktreeCard]) -> str:
    env, build_columns = _make_jinja_env()
    tpl = env.get_template("partials/board.html")
    return tpl.render(columns=build_columns(cards))


# ---------------------------------------------------------------------------
# 3c. Comment count only — no detail leakage
# ---------------------------------------------------------------------------

def test_template_shows_comment_count_not_bodies():
    """Card with 3 ReviewComments: count shown, bodies/authors/paths NOT rendered."""
    card = _minimal_card(
        status=PRStatus.HAS_COMMENTS,
        review_comments=_review_comments(3),
    )
    html = _render_board([card])

    # Count "3" must appear in an element whose class contains "comment"
    # We look for the class marker near "3"
    assert 'class="card-comment-count"' in html or "card-comment" in html, (
        "Expected a 'card-comment*' element with the count"
    )
    # The number 3 must appear somewhere in a comment-related context
    import re  # noqa: PLC0415
    comment_count_pattern = re.compile(
        r'class="[^"]*comment[^"]*"[^>]*>[^<]*3', re.DOTALL
    )
    assert comment_count_pattern.search(html), (
        "Expected count '3' inside a comment-classed element"
    )

    # Sensitive detail must NOT appear
    assert "UNIQUE_COMMENT_BODY_XYZ" not in html, (
        "Comment body must NOT be rendered in the new contract"
    )
    assert "somereviewer" not in html, (
        "Comment author must NOT be rendered in the new contract"
    )
    assert "some/file.py" not in html, (
        "Comment file path must NOT be rendered in the new contract"
    )


# ---------------------------------------------------------------------------
# 3d. Field DOM order and class contract
# ---------------------------------------------------------------------------

def test_template_field_order_and_classes():
    """New template classes exist and appear in the required DOM order."""
    card = _minimal_card(
        pr_number=123,
        pr_url="https://github.com/Boundless-Studios/gaia-free/pull/123",
        pr_title="My Test PR",
        worktree_path="/tmp/wt/foo",
        worktree_name="foo",
        branch="bou-1551-test",
        last_updated_label="2026-06-11 12:00",
        # The Started row only renders when a start time is known — give the
        # card a PR creation time so the card-started contract is exercised.
        pr_created_at="2026-06-10T09:00:00Z",
        status=PRStatus.CLEAN,
    )
    html = _render_board([card])

    required_classes = ["card-started", "card-updated", "card-pr-link", "card-worktree-link"]
    for cls in required_classes:
        assert f'class="{cls}"' in html or f' {cls}' in html, (
            f"Expected class '{cls}' in rendered HTML"
        )

    # DOM ORDER: started < updated < pr-link < worktree-link
    idx_started = html.index("card-started")
    idx_updated = html.index("card-updated")
    idx_pr_link = html.index("card-pr-link")
    idx_worktree = html.index("card-worktree-link")

    assert idx_started < idx_updated, "card-started must appear before card-updated"
    assert idx_updated < idx_pr_link, "card-updated must appear before card-pr-link"
    assert idx_pr_link < idx_worktree, "card-pr-link must appear before card-worktree-link"


def test_template_single_state_badge():
    """Rendered card must contain exactly ONE element with class 'card-state'."""
    card = _minimal_card(status=PRStatus.CI_FAILING)
    html = _render_board([card])

    count = html.count('card-state')
    assert count == 1, f"Expected exactly 1 'card-state' element, found {count}"

    # The badge text must equal agent_state_label
    label = card.agent_state_label  # type: ignore[attr-defined]
    assert label in html, f"Expected agent_state_label {label!r} in rendered HTML"


def test_template_no_legacy_classes_outside_details():
    """Legacy noisy classes must NOT appear before the first <details> element."""
    card = _minimal_card(
        status=PRStatus.AGENT_WORKING,
        activity_message="doing stuff",
        activity_source="local",
    )
    html = _render_board([card])

    if "<details" in html:
        pre_details = html[: html.index("<details")]
    else:
        pre_details = html

    legacy = ["card-badge", "maintenance-state", "agent-pill", "card-activity"]
    for cls in legacy:
        assert cls not in pre_details, (
            f"Legacy class '{cls}' must not appear before <details> in the new template"
        )


# ---------------------------------------------------------------------------
# 3e. Diagnostics inside <details>
# ---------------------------------------------------------------------------

def test_template_diagnostics_inside_details():
    """Heartbeat, session id, and agent output must appear ONLY inside <details>."""
    heartbeat_dt = datetime(2026, 6, 11, 14, 30, 0, tzinfo=timezone.utc)
    card = _minimal_card(
        status=PRStatus.AGENT_WORKING,
        maintenance=_maintenance(
            MaintenanceStatus.RUNNING,
            last_heartbeat_at=heartbeat_dt,
            bead_id="gaia-free-zzz",
        ),
        runtime_session_id="sess-abc-123",
        agent_output=["line one of output"],
    )
    html = _render_board([card])

    assert "<details" in html, "Expected at least one <details> element"
    assert "</details>" in html, "Expected closing </details>"

    first_details = html.index("<details")
    last_details_close = html.rindex("</details>")

    # Each diagnostic must appear after <details opens and before </details> closes
    for needle in ["sess-abc-123", "line one of output"]:
        assert needle in html, f"Expected {needle!r} to appear somewhere in rendered HTML"
        idx = html.index(needle)
        assert idx > first_details, (
            f"{needle!r} must appear inside <details>, found before it"
        )
        assert idx < last_details_close, (
            f"{needle!r} must appear inside <details>, found after closing tag"
        )

    # The <details> element must carry class "card-details"
    assert "card-details" in html, "Expected class 'card-details' on the details element"


def test_template_surfaces_harness_state_and_token_usage():
    card = _minimal_card(
        runtime_session_id="conversation-1",
        runtime_chain_id="chain-1",
        runtime_generation=2,
        supervisor_state="awaiting_ack",
        context_percent=67.5,
        context_tokens=675_000,
        window_tokens=1_000_000,
        cumulative_tokens=9_500_000,
        context_confidence="degraded",
        runtime_quiescence="busy",
        runtime_active_turns=1,
        runtime_active_tools=2,
        runtime_active_subagents=1,
        runtime_active_critical_sections=0,
        runtime_checkpoint_fingerprint="abc123",
        runtime_outbox_depth=3,
        runtime_status_stale=True,
    )

    html = _render_board([card])

    assert "harness-summary" in html
    assert "Awaiting Ack" in html
    assert "67.5% context" in html
    assert "Context 675,000 / 1,000,000 · 67.5%" in html
    assert "Cumulative 9,500,000 tokens" in html
    assert "chain-1" in html
    assert "generation 2" in html
    assert "degraded" in html
    assert "busy" in html
    assert "1 turn · 2 tools · 1 subagent · 0 critical" in html
    assert "abc123" in html
    assert "Outbox 3" in html
    assert "Status stale" in html


# ---------------------------------------------------------------------------
# 3f. Ownership line rendering
# ---------------------------------------------------------------------------

def test_template_ownership_line_when_session_id_set():
    """Card with owner_session_id → ownership line rendered with session prefix and pid state."""
    now = datetime(2026, 6, 27, 10, 0, 0, tzinfo=timezone.utc)
    card = _minimal_card(
        owner_session_id="abc12345-dead-beef-cafe-000000000001",
        owner_pid=99999,
        owner_pid_alive=True,
        armed_at=now,
        loop_state="running",
    )
    html = _render_board([card])

    # Short session id prefix (first 8 chars) must appear
    assert "abc12345" in html, "Expected short session id prefix in ownership line"
    # pid alive/dead indicator
    assert "alive" in html, "Expected 'alive' indicator for live pid"
    # loop state
    assert "running" in html, "Expected loop_state in rendered card"
    # The ownership element class
    assert "card-ownership" in html, "Expected 'card-ownership' element in rendered HTML"


def test_template_ownership_dead_pid():
    """Card with owner_pid_alive=False → 'dead' shown in ownership line."""
    now = datetime(2026, 6, 27, 10, 0, 0, tzinfo=timezone.utc)
    card = _minimal_card(
        owner_session_id="deadbeef-0000-0000-0000-000000000000",
        owner_pid=2**20 - 1,
        owner_pid_alive=False,
        armed_at=now,
    )
    html = _render_board([card])
    assert "dead" in html, "Expected 'dead' pid indicator for stale ownership"
    assert "owner-pid-dead" in html or "dead" in html


def test_template_no_ownership_line_when_unowned():
    """Card without owner_session_id → no ownership line rendered."""
    card = _minimal_card(status=PRStatus.CLEAN)
    html = _render_board([card])
    assert "card-ownership" not in html, (
        "Expected NO ownership line on a card with no owner"
    )


# ---------------------------------------------------------------------------
# 3g. Comment threads block inside <details>
# ---------------------------------------------------------------------------

def test_template_thread_decisions_in_details():
    """Card with thread_decisions → comment threads block appears inside <details>."""
    threads = [
        ThreadDecision(
            thread_id="PRRT_abc12345",
            author="reviewer-x",
            created_at="2026-06-27T09:00:00Z",
            age_seconds=3600.0,
            decision="PICKED",
            marker_state=None,
        ),
        ThreadDecision(
            thread_id="PRRT_def67890",
            author="reviewer-y",
            created_at="2026-06-27T08:00:00Z",
            age_seconds=7200.0,
            decision="SKIP_RESOLVED",
            marker_state="resolved",
        ),
    ]
    card = _minimal_card(
        status=PRStatus.HAS_COMMENTS,
        thread_decisions=threads,
        # Need at least one other detail field so <details> renders
        latest_commit_sha="aabbccdd1234567",
    )
    html = _render_board([card])

    assert "<details" in html, "Expected <details> element in card with thread decisions"

    # Thread section header
    assert "Comment threads" in html, "Expected 'Comment threads' sub-section header"

    # Thread content inside details
    first_details = html.index("<details")
    last_details_close = html.rindex("</details>")

    for needle in ["PICKED", "SKIP_RESOLVED", "reviewer-x", "PRRT_abc"]:
        assert needle in html, f"Expected {needle!r} in rendered HTML"
        idx = html.index(needle)
        assert idx > first_details, f"{needle!r} must be inside <details>"
        assert idx < last_details_close, f"{needle!r} must be before </details>"

    # marker_state shown
    assert "resolved" in html, "Expected marker_state 'resolved' in thread decisions"


def test_template_no_thread_section_when_empty():
    """Card with empty thread_decisions → no 'Comment threads' sub-section."""
    card = _minimal_card(
        status=PRStatus.CLEAN,
        latest_commit_sha="aabbccdd1234567",
    )
    html = _render_board([card])
    assert "Comment threads" not in html, (
        "Expected no 'Comment threads' section when thread_decisions is empty"
    )
