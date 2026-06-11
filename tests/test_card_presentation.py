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

from agentic_pr_dash.models import (
    MaintenanceState,
    MaintenanceStatus,
    PRStatus,
    ReviewComment,
    WorktreeCard,
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
    "failed", "working", "queued", "awaiting_fixes",
    "ci_failing", "ci_pending", "merge_conflict", "no_pr", "clean",
]


def test_agent_state_label_non_empty_and_distinct():
    """Each of the 9 states must map to a non-empty, distinct human label."""
    labels: dict[str, str] = {}
    for state_val in _ALL_STATES:
        # Build a card that produces this agent_state
        if state_val == "failed":
            card = _minimal_card(status=PRStatus.AGENT_FAILED)
        elif state_val == "working":
            card = _minimal_card(status=PRStatus.AGENT_WORKING)
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
        # started_at requires pr_created_at which doesn't exist yet — use the
        # field directly once the contract lands; for now test class presence
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
