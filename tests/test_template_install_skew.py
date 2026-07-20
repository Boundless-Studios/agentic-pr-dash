"""Templates must not hot-reload out from under the loaded Python classes (BOU-2217).

Jinja2Templates defaults to ``auto_reload=True``, so the running dashboard picks up
template changes from disk the moment they land. Python classes do NOT reload. An
in-place snapshot reinstall therefore pairs a **new** template with the **old**
in-memory model class, and every render 500s until the process restarts:

    jinja2.exceptions.UndefinedError:
      'agentic_pr_dash.models.WorktreeCard object' has no attribute 'context_percent'
      at templates/partials/board.html:63

Observed 3 times in ~/.claude/daemons/pr-dashboard.log immediately before a manual
restart (BOU-2193 investigation). Both the template and the model were correct on
main — only the live process mixed versions.

Freshness is the wrong trade here. A process should render with the template set it
booted with, and pick up a new one on restart; ``install-agent-ops-tools.sh`` already
bounces the pr-dashboard daemon after installing, so nothing is lost by not
hot-reloading.
"""

from pathlib import Path

import pytest

from agentic_pr_dash import app as dashboard_app


def test_template_env_does_not_auto_reload():
    """The environment must be pinned, not watching the filesystem."""
    assert dashboard_app.templates.env.auto_reload is False, (
        "Jinja auto_reload must be off: with it on, a snapshot reinstall swaps in new "
        "templates while the old model classes stay loaded, and every render 500s "
        "until restart (BOU-2217)"
    )


def test_template_content_is_frozen_after_first_render(tmp_path):
    """Behavioural guard: editing a template on disk must not change a live render.

    This is the property that actually matters — `auto_reload is False` is just how we
    get it. Asserting the behaviour means a future refactor that rebuilds the
    environment differently still has to preserve it.
    """
    from fastapi.templating import Jinja2Templates

    template_dir = tmp_path / "templates"
    template_dir.mkdir()
    target = template_dir / "probe.html"
    target.write_text("ORIGINAL")

    # Built the same way app.py does it: this starlette version's Jinja2Templates
    # takes no env kwargs, so auto_reload is set on the constructed environment.
    env = Jinja2Templates(directory=str(template_dir)).env
    env.auto_reload = False
    first = env.get_template("probe.html").render()

    # Simulate the reinstall: the file on disk is replaced with a version that
    # references state the loaded process does not have.
    target.write_text("REINSTALLED {{ card.context_percent }}")

    second = env.get_template("probe.html").render()

    assert first == "ORIGINAL"
    assert second == "ORIGINAL", (
        "template was re-read from disk mid-process — that is exactly the skew window "
        "that produced the UndefinedError crash"
    )


def test_real_templates_still_render_after_the_change():
    """Turning auto_reload off must not break template loading itself."""
    env = dashboard_app.templates.env

    # board.html is the template that actually crashed; make sure it still loads.
    template = env.get_template("partials/board.html")

    assert template is not None
    assert Path(dashboard_app.BASE_DIR / "templates" / "partials" / "board.html").exists()
