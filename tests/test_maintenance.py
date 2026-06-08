import pytest

from agentic_pr_dash import config, maintenance
from agentic_pr_dash.models import PRData, ReviewComment


@pytest.fixture(autouse=True)
def _clear_cache():
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _pr(**kw) -> PRData:
    base = dict(number=1, title="t", branch="b", url="https://github.com/o/r/pull/1")
    base.update(kw)
    return PRData(**base)


def _comment() -> ReviewComment:
    return ReviewComment(id=10, author="reviewer", body="please fix", created_at="2026-01-01T00:00:00Z")


def test_blockers_none():
    assert maintenance.blockers_for_pr(_pr()) == []


def test_blockers_merge_conflict():
    assert maintenance.blockers_for_pr(_pr(merge_state="DIRTY")) == ["merge_conflict"]


def test_blockers_ci_failure():
    assert maintenance.blockers_for_pr(_pr(failing_checks=["unit-tests"])) == ["ci_failure"]


def test_blockers_review_comments():
    assert maintenance.blockers_for_pr(_pr(review_comments=[_comment()])) == ["review_comments"]


def test_blockers_all_three():
    pr = _pr(merge_state="DIRTY", failing_checks=["ci"], review_comments=[_comment()])
    assert maintenance.blockers_for_pr(pr) == ["merge_conflict", "ci_failure", "review_comments"]


def test_pr_url_uses_fallback():
    assert maintenance.pr_url(5, "https://x/pull/5") == "https://x/pull/5"


def test_prompt_is_tool_neutral_by_default(tmp_path):
    # No prompt_template configured -> generic wording, no "feature-pipeline",
    # and the completion hint references the generalized CLI, not the old module.
    config.load.cache_clear()
    prompt = maintenance.build_maintenance_prompt(
        _pr(worktree_path=str(tmp_path), review_comments=[_comment()])
    )
    assert "feature-pipeline" not in prompt
    assert "pr_dashboard" not in prompt
    assert "agentic-pr-dash complete" in prompt


def test_prompt_template_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_PR_DASH_PROMPT_TEMPLATE", "FOLLOW THE HOUSE RULES.")
    config.load.cache_clear()
    prompt = maintenance.build_maintenance_prompt(_pr(worktree_path=str(tmp_path)))
    assert "FOLLOW THE HOUSE RULES." in prompt
