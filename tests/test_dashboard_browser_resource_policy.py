"""Browser resource policy for the installed standalone dashboard app."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "src" / "agentic_pr_dash" / "templates" / "dashboard.html"
APP_JS = ROOT / "src" / "agentic_pr_dash" / "static" / "app.js"


def test_every_periodic_partial_is_lifecycle_gated_and_resumable() -> None:
    source = TEMPLATE.read_text(encoding="utf-8")
    periodic_pollers = source.count('hx-trigger="every ')

    # Seven pollers exist in the template; exactly one of the three main-panel
    # variants renders, so a live page runs at most five concurrently.
    assert periodic_pollers == 7, "measurement changed: update the resource budget"
    assert source.count('data-dashboard-poller') == periodic_pollers
    assert source.count(', dashboardRefresh"') == periodic_pollers


def test_background_polling_stops_and_resumes_once() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert "document.hidden" in source
    assert "document.hasFocus()" in source
    assert source.count("addEventListener('visibilitychange'") == 1
    assert source.count("addEventListener('focus'") == 1
    assert source.count("addEventListener('blur'") == 1
    assert source.count("addEventListener('htmx:beforeRequest'") == 1
    assert "event.preventDefault()" in source
    assert "htmx.trigger(poller, 'dashboardRefresh')" in source


def test_board_swap_does_not_force_layout_for_each_card() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert "offsetHeight" not in source
    assert "offsetWidth" not in source
    assert "getBoundingClientRect" not in source
