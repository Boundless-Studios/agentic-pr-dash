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
import time

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
        return _cp(stdout='[{"number": 1, "author": {"login": "ilganeli"}}]')

    monkeypatch.setattr(github_api, "_run", fake_run)
    assert github_api.list_open_prs(str(tmp_path)) == [{"number": 1, "author": {"login": "ilganeli"}}]
    assert seen and "--author" not in seen[0]


def test_list_open_prs_defaults_to_at_me(tmp_path, monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(github_api, "_repo_hostname", lambda cwd=None: "ghe.example")

    def fake_run(cmd, timeout_s=20, cwd=None):
        seen.append(cmd)
        if cmd[:3] == ["gh", "api", "user"]:
            return _cp(stdout="viewer\n")
        return _cp(stdout='[{"number": 1, "author": {"login": "viewer"}}, '
                          '{"number": 2, "author": {"login": "someone-else"}}]')

    monkeypatch.setattr(github_api, "_run", fake_run)
    assert github_api.list_open_prs(str(tmp_path)) == [
        {"number": 1, "author": {"login": "viewer"}}
    ]
    assert "--author" not in seen[0]
    assert seen[1][:3] == ["gh", "api", "user"]
    assert seen[1][seen[1].index("--hostname") + 1] == "ghe.example"


def test_repo_hostname_prefers_host_qualified_gh_repo(monkeypatch):
    monkeypatch.setenv("GH_REPO", "enterprise.example/owner/repo")
    monkeypatch.setattr(
        github_api.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("GH_REPO must avoid consulting origin"),
    )

    assert github_api._repo_hostname("/repo") == "enterprise.example"


def test_list_open_prs_rejects_non_list_before_author_filtering(tmp_path, monkeypatch):
    monkeypatch.setattr(github_api, "_run", lambda *args, **kwargs: _cp(stdout='{"number": 1}'))

    assert github_api.list_open_prs(str(tmp_path)) is None
    failure = github_api.last_list_open_prs_failure()
    assert failure is not None
    assert failure.reason == "not-a-list"


def test_list_open_prs_requests_complete_pagination(tmp_path, monkeypatch):
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return _cp(stdout="[]")

    monkeypatch.setattr(github_api, "_run", fake_run)
    assert github_api.list_open_prs(str(tmp_path)) == []
    limits = [seen[0][i + 1] for i, value in enumerate(seen[0][:-1]) if value == "--limit"]
    assert limits == [str(2**31 - 1)]


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
        return _cp(stdout='[{"number": 1, "author": {"login": "ilganeli"}}]')

    monkeypatch.setattr(pr_state.subprocess, "run", fake_run)
    assert pr_state._gh_pr_list_json(str(tmp_path), [], "number") == [{"number": 1, "author": {"login": "ilganeli"}}]
    assert seen and "--author" not in seen[0]
    assert seen[0][seen[0].index("--json") + 1] == "number,author"


def test_gh_pr_list_json_resolves_at_me_and_requests_author(tmp_path, monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(github_api, "_repo_hostname", lambda cwd=None: "ghe.example")

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        if cmd[:3] == ["gh", "api", "user"]:
            return _cp(stdout="viewer\n")
        return _cp(stdout='[{"number": 1, "author": {"login": "viewer"}}, '
                          '{"number": 2, "author": {"login": "other"}}]')

    monkeypatch.setattr(github_api, "_run", fake_run)
    assert pr_state._gh_pr_list_json(str(tmp_path), [], "number") == [
        {"number": 1, "author": {"login": "viewer"}}
    ]
    assert seen[0][seen[0].index("--json") + 1] == "number,author"
    assert seen[1][:3] == ["gh", "api", "user"]
    assert seen[1][seen[1].index("--hostname") + 1] == "ghe.example"


def test_gh_pr_list_json_propagates_deadline_to_at_me_viewer_lookup(
    tmp_path, monkeypatch
):
    seen: list[dict] = []
    deadline = time.monotonic() + 30

    def fake_run(cmd, **kwargs):
        seen.append(kwargs)
        if cmd[:3] == ["gh", "api", "user"]:
            return _cp(stdout="viewer\n")
        return _cp(stdout='[{"number": 1, "author": {"login": "viewer"}}]')

    monkeypatch.setattr(github_api, "_run", fake_run)
    assert pr_state._gh_pr_list_json(
        str(tmp_path), [], "number", deadline=deadline
    )
    assert seen[0]["deadline"] == deadline
    assert seen[1]["deadline"] == deadline


def test_gh_pr_list_json_recomputes_viewer_timeout_from_shared_deadline(
    tmp_path, monkeypatch
):
    now = [100.0]
    seen: list[dict] = []

    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    def fake_run(cmd, **kwargs):
        seen.append(kwargs)
        if cmd[:3] == ["gh", "pr", "list"]:
            now[0] = 108.0
            return _cp(stdout='[{"number": 1, "author": {"login": "viewer"}}]')
        return _cp(stdout="viewer\n")

    monkeypatch.setattr(github_api, "_run", fake_run)

    assert pr_state._gh_pr_list_json(
        str(tmp_path), [], "number", timeout=15, deadline=110.0
    )
    assert seen[0]["timeout_s"] == 10.0
    assert seen[1]["timeout_s"] == 2.0
    assert seen[1]["deadline"] == 110.0
