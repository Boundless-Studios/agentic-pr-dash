"""Tests for github_api.required_checks_pending (BOU-1789 Task 1)."""
from __future__ import annotations

import json
import types

import pytest

from agentic_pr_dash import github_api


def _make_result(returncode: int, stdout: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def test_required_checks_pending_true_when_in_progress(monkeypatch):
    """When a required check has bucket==pending, returns True."""
    checks = [
        {"name": "build", "bucket": "pending", "state": "in_progress"},
    ]
    monkeypatch.setattr(github_api, "_run",
                        lambda cmd, **kw: _make_result(0, json.dumps(checks)))
    assert github_api.required_checks_pending(42) is True


def test_required_checks_pending_false_when_all_terminal(monkeypatch):
    """When all required checks are terminal (pass/fail), returns False."""
    checks = [
        {"name": "build", "bucket": "pass", "state": "completed"},
        {"name": "test", "bucket": "fail", "state": "completed"},
    ]
    monkeypatch.setattr(github_api, "_run",
                        lambda cmd, **kw: _make_result(0, json.dumps(checks)))
    assert github_api.required_checks_pending(42) is False


def test_required_checks_pending_false_on_gh_error(monkeypatch):
    """When gh exits non-zero, returns False (fail-safe)."""
    monkeypatch.setattr(github_api, "_run",
                        lambda cmd, **kw: _make_result(1, ""))
    assert github_api.required_checks_pending(42) is False


def test_required_checks_pending_false_on_json_error(monkeypatch):
    """When gh returns invalid JSON, returns False (fail-safe)."""
    monkeypatch.setattr(github_api, "_run",
                        lambda cmd, **kw: _make_result(0, "not-json"))
    assert github_api.required_checks_pending(42) is False


def test_required_checks_pending_false_empty_list(monkeypatch):
    """When no required checks exist, returns False (no pending checks)."""
    monkeypatch.setattr(github_api, "_run",
                        lambda cmd, **kw: _make_result(0, "[]"))
    assert github_api.required_checks_pending(42) is False


def test_required_checks_pending_uses_required_flag(monkeypatch):
    """The gh command includes --required flag."""
    captured_cmd = {}
    def fake_run(cmd, **kw):
        captured_cmd["cmd"] = cmd
        return _make_result(0, "[]")
    monkeypatch.setattr(github_api, "_run", fake_run)
    github_api.required_checks_pending(42, cwd="/some/path")
    assert "--required" in captured_cmd["cmd"]
    assert "42" in captured_cmd["cmd"]
