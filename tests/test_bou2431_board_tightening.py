"""BOU-2431: the board fits on screen, and nothing it drops disappears.

The board had grown to eight columns and required horizontal scrolling. Three
distinctions were folded away — decision-vs-attention, waiting-vs-working, and
no-PR-vs-PR — and each fold is only acceptable because the signal it carried
still lands somewhere. These tests pin exactly that: the shape, the ordering
that replaces the dropped column, and the tab that catches the cards which
left the board.
"""

from __future__ import annotations

from agentic_pr_dash.app import (
    KANBAN_COLUMNS,
    VALID_DASHBOARD_TABS,
    _canonical_dashboard_tab,
    build_columns,
    no_pr_cards,
)
from agentic_pr_dash.models import PRStatus, WorktreeCard


def _card(status: PRStatus, name: str) -> WorktreeCard:
    return WorktreeCard(id=f"wt-{name}", worktree_name=name, branch=name, status=status)


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def test_the_board_is_five_columns() -> None:
    assert [column["id"] for column in KANBAN_COLUMNS] == [
        "needs_attention",
        "in_progress",
        "pending",
        "ready_cleanup",
        "done",
    ]


def test_no_status_lands_in_two_columns() -> None:
    """Overlapping columns would render the same card twice."""
    seen: set[PRStatus] = set()
    for column in KANBAN_COLUMNS:
        overlap = seen & set(column["statuses"])
        assert not overlap, f"{column['id']} re-maps {overlap}"
        seen |= set(column["statuses"])


def test_every_status_has_a_home_on_the_board_or_the_tab() -> None:
    """A status with neither a column nor the tab would vanish silently."""
    columned: set[PRStatus] = set()
    for column in KANBAN_COLUMNS:
        columned |= set(column["statuses"])

    homeless = set(PRStatus) - columned - {PRStatus.NO_PR}
    assert not homeless, f"statuses with nowhere to render: {homeless}"


# ---------------------------------------------------------------------------
# The folds
# ---------------------------------------------------------------------------

def test_decision_cards_sort_ahead_of_the_rest_of_needs_attention() -> None:
    """The BOU-2402 guarantee, preserved without its own column."""
    cards = [
        _card(PRStatus.CI_FAILING, "ci-red"),
        _card(PRStatus.WAITING_HUMAN_DECISION, "asks-you"),
        _card(PRStatus.HAS_COMMENTS, "commented"),
    ]

    column = next(c for c in build_columns(cards) if c["id"] == "needs_attention")

    assert [card.worktree_name for card in column["cards"]] == [
        "asks-you",
        "ci-red",
        "commented",
    ]


def test_the_fold_is_a_sort_not_a_filter() -> None:
    """Every attention status still renders — decision first, none dropped."""
    statuses = [
        PRStatus.WAITING_HUMAN_DECISION,
        PRStatus.CI_FAILING,
        PRStatus.HAS_COMMENTS,
        PRStatus.CI_AND_COMMENTS,
        PRStatus.MERGE_CONFLICT,
        PRStatus.AGENT_FAILED,
    ]
    cards = [_card(status, status.value) for status in statuses]

    column = next(c for c in build_columns(cards) if c["id"] == "needs_attention")

    assert column["count"] == len(statuses)
    assert column["cards"][0].status is PRStatus.WAITING_HUMAN_DECISION


def test_an_agent_waiting_is_still_agent_working() -> None:
    """The dropped "Waiting" column folded here, not into the void."""
    cards = [_card(PRStatus.AGENT_WAITING, "polling")]

    column = next(c for c in build_columns(cards) if c["id"] == "in_progress")

    assert [card.worktree_name for card in column["cards"]] == ["polling"]


# ---------------------------------------------------------------------------
# The tab
# ---------------------------------------------------------------------------

def test_no_pr_worktrees_leave_the_board() -> None:
    cards = [_card(PRStatus.NO_PR, "scratch"), _card(PRStatus.CLEAN, "shipped")]

    rendered = [card for column in build_columns(cards) for card in column["cards"]]

    assert [card.worktree_name for card in rendered] == ["shipped"]


def test_no_pr_worktrees_land_on_the_tab() -> None:
    cards = [_card(PRStatus.NO_PR, "scratch"), _card(PRStatus.CLEAN, "shipped")]

    assert [card.worktree_name for card in no_pr_cards(cards)] == ["scratch"]


def test_the_worktrees_tab_is_reachable() -> None:
    assert "worktrees" in VALID_DASHBOARD_TABS
    assert _canonical_dashboard_tab("worktrees") == "worktrees"


def test_an_unknown_tab_still_falls_back_to_the_board() -> None:
    assert _canonical_dashboard_tab("nonsense") == "board"


# ---------------------------------------------------------------------------
# Rendering — a template error here would ship as a blank tab
# ---------------------------------------------------------------------------

def _render_worktrees(cards: list[WorktreeCard]) -> str:
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader

    import agentic_pr_dash

    templates_dir = Path(agentic_pr_dash.__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=False)
    return env.get_template("partials/worktrees.html").render(no_pr_cards=cards)


def test_the_tab_renders_each_worktree() -> None:
    html = _render_worktrees([_card(PRStatus.NO_PR, "scratch-pad")])

    assert "scratch-pad" in html
    assert "1 worktree" in html


def test_the_empty_tab_says_so_rather_than_rendering_blank() -> None:
    html = _render_worktrees([])

    assert "Every worktree has an open PR." in html
    assert "0 worktrees" in html
