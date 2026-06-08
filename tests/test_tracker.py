from pathlib import Path

from agentic_pr_dash import tracker
from agentic_pr_dash.config import Config


def _cfg(name: str) -> Config:
    return Config(
        repo=None,
        state_dir=Path(".agentic-pr-dash"),
        tracker=name,
        executor="",
        discovery_names=("claude",),
        runner_label=None,
        lease_seconds=1800,
        heartbeat_ttl_seconds=600,
        prompt_template=None,
        session_registry_path=None,
    )


def test_default_is_noop():
    t = tracker.get_tracker(_cfg("none"))
    assert isinstance(t, tracker.NoOpTracker)
    assert t.open_task(pr=1, branch="x", title="t", body="b") is None
    assert t.find_task(pr=1, branch="x") is None
    t.close_task("whatever")  # must not raise


def test_unknown_tracker_falls_back_to_noop():
    assert isinstance(tracker.get_tracker(_cfg("nope")), tracker.NoOpTracker)


def test_selection():
    assert isinstance(tracker.get_tracker(_cfg("beads")), tracker.BeadsTracker)
    assert isinstance(tracker.get_tracker(_cfg("github-issues")), tracker.GitHubIssuesTracker)
    assert isinstance(tracker.get_tracker(_cfg("github")), tracker.GitHubIssuesTracker)
    assert isinstance(tracker.get_tracker(_cfg("off")), tracker.NoOpTracker)


def test_beads_label_strips_prefixes():
    b = tracker.BeadsTracker()
    assert b._label("feature/foo-bar") == "foo-bar"
    assert b._label("fix/x") == "x"
    assert b._label("plain") == "plain"
    assert b._label("a/b/c") == "a-b-c"


def test_tracker_satisfies_protocol():
    for t in (tracker.NoOpTracker(), tracker.BeadsTracker(), tracker.GitHubIssuesTracker()):
        assert isinstance(t, tracker.TaskTracker)
