from pathlib import Path

import pytest

from agentic_pr_dash import config


@pytest.fixture(autouse=True)
def _clear_cache():
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def test_defaults(tmp_path):
    c = config.load(str(tmp_path))
    assert c.state_dir.name == ".agentic-pr-dash"
    assert c.tracker == "none"
    assert c.discovery_names == ("claude", "codex")
    assert c.lease_seconds == 1800
    assert c.heartbeat_ttl_seconds == 600
    assert c.runner_label is None


def test_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_PR_DASH_TRACKER", "beads")
    monkeypatch.setenv("AGENTIC_PR_DASH_STATE_DIR", ".custom")
    monkeypatch.setenv("AGENTIC_PR_DASH_LEASE_SECONDS", "42")
    config.load.cache_clear()
    c = config.load(str(tmp_path))
    assert c.tracker == "beads"
    assert c.state_dir.name == ".custom"
    assert c.lease_seconds == 42


def test_legacy_gaia_env_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIC_PR_DASH_TRACKER", raising=False)
    monkeypatch.setenv("GAIA_TRACKER", "github-issues")
    config.load.cache_clear()
    c = config.load(str(tmp_path))
    assert c.tracker == "github-issues"


def test_legacy_gaia_state_dir_adopted(tmp_path):
    # A pre-existing .gaia dir is adopted so current installs keep their markers.
    (tmp_path / ".gaia").mkdir()
    config.load.cache_clear()
    c = config.load(str(tmp_path))
    assert c.state_dir.name == ".gaia"


def test_toml_file(tmp_path):
    (tmp_path / "agentic-pr-dash.toml").write_text(
        '[project]\ntracker = "github-issues"\nrunner_label = "my-fleet"\n'
        'discovery_names = ["claude", "aider"]\n',
        encoding="utf-8",
    )
    config.load.cache_clear()
    c = config.load(str(tmp_path))
    assert c.tracker == "github-issues"
    assert c.runner_label == "my-fleet"
    assert c.discovery_names == ("claude", "aider")


def test_per_worktree_marker_paths(tmp_path):
    c = config.load(str(tmp_path))
    assert c.watch_marker_for("/tmp/wt") == Path("/tmp/wt/.agentic-pr-dash/pr-watch.armed")
    assert c.session_marker_for("/tmp/wt") == Path("/tmp/wt/.agentic-pr-dash/pr-watch.session")
    assert c.maintenance_dir_for("/tmp/wt") == Path("/tmp/wt/.agentic-pr-dash/pr-maintenance")


def test_env_beats_toml(tmp_path, monkeypatch):
    (tmp_path / "agentic-pr-dash.toml").write_text('[project]\ntracker = "beads"\n', encoding="utf-8")
    monkeypatch.setenv("AGENTIC_PR_DASH_TRACKER", "none")
    config.load.cache_clear()
    c = config.load(str(tmp_path))
    assert c.tracker == "none"


def test_explicit_config_path_env(tmp_path, monkeypatch):
    # AGENTIC_PR_DASH_CONFIG points at a config file outside the cwd tree.
    cfg = tmp_path / "elsewhere.toml"
    cfg.write_text('[project]\ntracker = "github-issues"\nrunner_label = "fleet"\n', encoding="utf-8")
    other = tmp_path / "work"
    other.mkdir()
    monkeypatch.setenv("AGENTIC_PR_DASH_CONFIG", str(cfg))
    config.load.cache_clear()
    c = config.load(str(other))
    assert c.tracker == "github-issues"
    assert c.runner_label == "fleet"
