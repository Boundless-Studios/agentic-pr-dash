"""BOU-2810: one PR-state resolution per repo, shared by every reader.

``gh pr list`` and ``gh pr view`` are GraphQL calls against a shared
App-installation token with a single 5000-point hourly budget. The old shape had
each reader resolve PR state independently — the dashboard, the loop, each
session's waiter, the stop-gate's branch probe, and a per-PR ``gh pr view`` for
every tracked PR. These tests pin that they now converge on ONE snapshot, and —
just as important — that the fall-throughs which must still hit GitHub really do.
"""

from __future__ import annotations

import subprocess

import pytest

from agentic_pr_dash import github_api
from agentic_pr_dash.config import load as load_config


@pytest.fixture(autouse=True)
def isolated_snapshot_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("APD_PR_SNAPSHOT_DIR", str(tmp_path / "snap"))


def _pr(number: int, branch: str, *, draft: bool = False) -> dict:
    return {
        "number": number,
        "headRefName": branch,
        "isDraft": draft,
        "title": f"PR {number}",
        "state": "OPEN",
    }


def _seed(tmp_path, prs):
    """Populate the shared snapshot exactly as a real fetch would."""
    path = github_api._pr_snapshot_path(str(tmp_path))
    github_api._write_pr_snapshot(path, prs, load_config(str(tmp_path)).pr_author)


def _ok(stdout: str):
    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    return _run


# --------------------------------------------------------------------------- #
# per-PR reads are served from the shared list, not their own gh pr view
# --------------------------------------------------------------------------- #

def test_resolve_pr_serves_from_snapshot_without_calling_gh(tmp_path, monkeypatch):
    _seed(tmp_path, [_pr(1, "a"), _pr(2, "b", draft=True)])
    monkeypatch.setattr(
        github_api, "_run",
        lambda *a, **k: pytest.fail("resolve_pr must not shell out on a snapshot hit"),
    )

    assert github_api.resolve_pr(2, "number,isDraft", str(tmp_path)) == {
        "number": 2, "isDraft": True,
    }


def test_resolve_pr_returns_only_requested_fields(tmp_path, monkeypatch):
    """A caller must not silently start depending on a field it never asked for."""
    _seed(tmp_path, [_pr(7, "x")])
    monkeypatch.setattr(github_api, "_run", lambda *a, **k: pytest.fail("no gh call"))

    assert github_api.resolve_pr(7, "number", str(tmp_path)) == {"number": 7}


def test_resolve_pr_field_convenience(tmp_path, monkeypatch):
    _seed(tmp_path, [_pr(9, "y", draft=True)])
    monkeypatch.setattr(github_api, "_run", lambda *a, **k: pytest.fail("no gh call"))

    assert github_api.resolve_pr_field(9, "isDraft", str(tmp_path)) is True


# --------------------------------------------------------------------------- #
# fall-throughs: the snapshot must never be allowed to answer these
# --------------------------------------------------------------------------- #

def test_pr_absent_from_snapshot_falls_through(tmp_path, monkeypatch):
    """Someone else's PR, or a closed one, is legitimately not in an
    ``--author --state open`` list. Answering from the snapshot would report a
    real PR as nonexistent."""
    _seed(tmp_path, [_pr(1, "a")])
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, '{"number": 42, "isDraft": false}', "")

    monkeypatch.setattr(github_api, "_run", _fake_run)

    assert github_api.resolve_pr(42, "number,isDraft", str(tmp_path)) == {
        "number": 42, "isDraft": False,
    }
    assert calls, "a PR outside the snapshot must reach gh"


def test_field_outside_the_superset_falls_through(tmp_path, monkeypatch):
    """``statusCheckRollup`` is deliberately not in the snapshot — it is huge and
    changes far faster than the TTL. Serving it stale would misreport CI."""
    _seed(tmp_path, [_pr(1, "a")])
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, '{"statusCheckRollup": []}', "")

    monkeypatch.setattr(github_api, "_run", _fake_run)

    github_api.resolve_pr(1, "statusCheckRollup", str(tmp_path))
    assert calls, "a field the snapshot never fetched must not be served from it"


def test_force_bypasses_the_snapshot(tmp_path, monkeypatch):
    """Callers about to act on the result can demand live state."""
    _seed(tmp_path, [_pr(1, "a")])
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, '{"number": 1}', "")

    monkeypatch.setattr(github_api, "_run", _fake_run)

    github_api.resolve_pr(1, "number", str(tmp_path), force=True)
    assert calls, "force=True must always reach gh"


# --------------------------------------------------------------------------- #
# peek: read-only, never fetches
# --------------------------------------------------------------------------- #

def test_peek_never_fetches_on_a_cold_snapshot(tmp_path, monkeypatch):
    """The Stop-hook path bounds its own gh call with the remaining budget, so
    the peek it uses must never issue one itself."""
    monkeypatch.setattr(
        github_api, "list_open_prs",
        lambda cwd=None: pytest.fail("peek must not fetch"),
    )
    assert github_api.peek_pr_snapshot(str(tmp_path)) is None


def test_peek_returns_a_fresh_snapshot(tmp_path):
    _seed(tmp_path, [_pr(3, "c")])
    peeked = github_api.peek_pr_snapshot(str(tmp_path))
    assert peeked is not None
    assert [p["number"] for p in peeked] == [3]


# --------------------------------------------------------------------------- #
# the branch probe converges on the same snapshot
# --------------------------------------------------------------------------- #

def test_branch_resolution_uses_the_shared_snapshot(tmp_path, monkeypatch):
    from agentic_pr_dash._maintenance import pr_state

    _seed(tmp_path, [_pr(11, "feature-a"), _pr(12, "feature-b", draft=True)])
    monkeypatch.setattr(
        pr_state, "_gh_pr_list_json",
        lambda *a, **k: pytest.fail("branch resolution must reuse the snapshot"),
    )

    assert pr_state._resolve_open_pr_for_branch(str(tmp_path), "feature-b") == (12, True)


def test_branch_resolution_is_exact_not_prefix(tmp_path, monkeypatch):
    """``gh pr list --head fix`` also returns ``fix-123``; the snapshot path
    matches exactly, which is the intended semantics."""
    from agentic_pr_dash._maintenance import pr_state

    _seed(tmp_path, [_pr(20, "fix-123")])
    monkeypatch.setattr(
        pr_state, "_gh_pr_list_json", lambda *a, **k: pytest.fail("no gh call"))

    assert pr_state._resolve_open_pr_for_branch(str(tmp_path), "fix") is None


def test_stop_hook_list_spawns_nothing_when_budget_is_gone(tmp_path, monkeypatch):
    """Regression: peeking before the budget check shelled out via
    ``config.load`` -> ``gh repo view``, breaking the zero-subprocess contract
    AND adding a GraphQL call to the hot path."""
    from agentic_pr_dash._maintenance import pr_state

    calls = []
    monkeypatch.setattr(pr_state.subprocess, "run", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(
        github_api, "peek_pr_snapshot",
        lambda *a, **k: pytest.fail("no peek when the budget is exhausted"),
    )

    assert pr_state._list_my_open_prs(str(tmp_path), timeout=0) == {}
    assert calls == []
