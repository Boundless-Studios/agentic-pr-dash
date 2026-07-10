"""``pr_author`` config — PR discovery under an isolated automation identity.

BOU-1923 gave automation its own gh identity via a GitHub App installation
token in ``GH_TOKEN``. Every PR-discovery path ran ``gh pr list --author
"@me"``, and for an App token ``@me`` resolves to the App bot — which authored
no PRs. Result observed live: the dashboard board rendered zero cards while
the operator had open PRs, and ``await`` waiters exited ``{"outcome": "idle",
"pr": null}`` on branches with an open PR. ``pr_author`` (toml or
``AGENTIC_PR_DASH_PR_AUTHOR``) pins discovery to the operator's login; the
``@me`` default preserves existing single-identity behavior.
"""

from __future__ import annotations

import subprocess

import pytest

from agentic_pr_dash import config, github_api
from agentic_pr_dash._maintenance import pr_state


@pytest.fixture(autouse=True)
def _clear_cache():
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _cp(returncode=0, stdout="[]", stderr=""):
    return subprocess.CompletedProcess(["gh"], returncode, stdout, stderr)


# --------------------------------------------------------------------------- #
# config resolution
# --------------------------------------------------------------------------- #

def test_pr_author_defaults_to_at_me(tmp_path):
    assert config.load(str(tmp_path)).pr_author == "@me"


def test_pr_author_from_toml(tmp_path):
    (tmp_path / "agentic-pr-dash.toml").write_text(
        'pr_author = "ilganeli"\n', encoding="utf-8"
    )
    assert config.load(str(tmp_path)).pr_author == "ilganeli"


def test_pr_author_env_wins_over_toml(tmp_path, monkeypatch):
    (tmp_path / "agentic-pr-dash.toml").write_text(
        'pr_author = "from-toml"\n', encoding="utf-8"
    )
    monkeypatch.setenv("AGENTIC_PR_DASH_PR_AUTHOR", "from-env")
    config.load.cache_clear()
    assert config.load(str(tmp_path)).pr_author == "from-env"


# --------------------------------------------------------------------------- #
# list_open_prs (dashboard board / snapshot cache)
# --------------------------------------------------------------------------- #

def test_list_open_prs_uses_configured_author(tmp_path, monkeypatch):
    (tmp_path / "agentic-pr-dash.toml").write_text(
        'pr_author = "ilganeli"\n', encoding="utf-8"
    )
    seen: list[list[str]] = []

    def fake_run(cmd, timeout_s=20, cwd=None):
        seen.append(cmd)
        return _cp()

    monkeypatch.setattr(github_api, "_run", fake_run)
    assert github_api.list_open_prs(str(tmp_path)) == []
    assert seen and "--author" in seen[0]
    assert seen[0][seen[0].index("--author") + 1] == "ilganeli"


def test_list_open_prs_defaults_to_at_me(tmp_path, monkeypatch):
    seen: list[list[str]] = []

    def fake_run(cmd, timeout_s=20, cwd=None):
        seen.append(cmd)
        return _cp()

    monkeypatch.setattr(github_api, "_run", fake_run)
    assert github_api.list_open_prs(str(tmp_path)) == []
    assert seen[0][seen[0].index("--author") + 1] == "@me"


# --------------------------------------------------------------------------- #
# _gh_pr_list_json (maintenance / waiter / reconcile PR-state probes)
# --------------------------------------------------------------------------- #

def test_gh_pr_list_json_uses_configured_author(tmp_path, monkeypatch):
    (tmp_path / "agentic-pr-dash.toml").write_text(
        'pr_author = "ilganeli"\n', encoding="utf-8"
    )
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return _cp()

    monkeypatch.setattr(pr_state.subprocess, "run", fake_run)
    assert pr_state._gh_pr_list_json(str(tmp_path), [], "number") == []
    assert seen and seen[0][seen[0].index("--author") + 1] == "ilganeli"
