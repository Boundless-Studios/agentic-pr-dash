"""Tests for escalation_failure_threshold config field (BOU-1789 Task 7)."""
from __future__ import annotations

import os

import pytest

from agentic_pr_dash import config


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def test_default_threshold_is_3(tmp_path, monkeypatch):
    """Default escalation_failure_threshold is 3 when not set anywhere."""
    monkeypatch.delenv("AGENTIC_PR_DASH_ESCALATION_THRESHOLD", raising=False)
    monkeypatch.delenv("GAIA_PR_WATCH_ESCALATION_THRESHOLD", raising=False)
    cfg = config.load(str(tmp_path))
    assert cfg.escalation_failure_threshold == 3


def test_env_overrides_default(tmp_path, monkeypatch):
    """AGENTIC_PR_DASH_ESCALATION_THRESHOLD env var overrides the default."""
    monkeypatch.setenv("AGENTIC_PR_DASH_ESCALATION_THRESHOLD", "5")
    config.load.cache_clear()
    cfg = config.load(str(tmp_path))
    assert cfg.escalation_failure_threshold == 5


def test_toml_sets_threshold(tmp_path, monkeypatch):
    """escalation_failure_threshold in TOML is honored."""
    monkeypatch.delenv("AGENTIC_PR_DASH_ESCALATION_THRESHOLD", raising=False)
    toml_file = tmp_path / "agentic-pr-dash.toml"
    toml_file.write_text("escalation_failure_threshold = 7\n", encoding="utf-8")
    config.load.cache_clear()
    cfg = config.load(str(tmp_path))
    assert cfg.escalation_failure_threshold == 7


def test_env_takes_precedence_over_toml(tmp_path, monkeypatch):
    """Env var takes precedence over TOML for escalation_failure_threshold."""
    toml_file = tmp_path / "agentic-pr-dash.toml"
    toml_file.write_text("escalation_failure_threshold = 7\n", encoding="utf-8")
    monkeypatch.setenv("AGENTIC_PR_DASH_ESCALATION_THRESHOLD", "2")
    config.load.cache_clear()
    cfg = config.load(str(tmp_path))
    assert cfg.escalation_failure_threshold == 2
