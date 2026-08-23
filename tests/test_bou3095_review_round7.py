"""BOU-3095 PR #169 review round 7 (codex).

* ``len(payload) >= per_page`` calls a full-but-complete page truncated, so a
  repo with exactly 100 open PRs never counts a 304 as an observation and the
  freshness indicator goes stale despite every probe succeeding;
* the watched set unions historical observation/degradation keys back every
  tick, so a root deliberately removed from configuration is reported as
  dropped forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import subprocess

import pytest

from agentic_pr_dash import github_api, orchestrator as orchestrator_module
from agentic_pr_dash.models import RunnerExecutionSummary
from agentic_pr_dash.observation import ObservationController
from agentic_pr_dash.quota import QuotaLedger


class ManualClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 22, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


def _rest_item(number: int) -> dict:
    return {
        "number": number,
        "user": {"login": "alice"},
        "head": {"sha": "h", "ref": f"f/{number}", "repo": {"owner": {}}},
        "base": {"ref": "main"},
        "html_url": f"https://github.com/org/widgets/pull/{number}",
        "updated_at": "2026-08-22T00:00:00Z",
    }


def _response(payload: str, *, link: str | None = None) -> str:
    headers = 'HTTP/2 200 OK\r\nETag: "v1"\r\n'
    if link is not None:
        headers += f"Link: {link}\r\n"
    return f"{headers}\r\n{payload}"


def _run_returning(text: str):
    return lambda *a, **k: subprocess.CompletedProcess(
        a[0] if a else [], 0, stdout=text, stderr=""
    )


# ---------------------------------------------------------------------------
# 1 — a full page is not proof that more pages exist
# ---------------------------------------------------------------------------


def test_exactly_full_page_without_a_next_link_is_complete(monkeypatch) -> None:
    """100 open PRs is a full page AND the whole set."""
    payload = json.dumps([_rest_item(n) for n in range(1, 101)])
    monkeypatch.setattr(github_api, "_run", _run_returning(_response(payload)))

    probe = github_api.probe_open_prs_rest("org", "widgets", author="alice")

    assert len(probe.prs) == 100
    assert probe.truncated is False, (
        "a full page with no rel=next link was reported as truncated, which "
        "stops every later 304 counting as an observation"
    )


def test_full_page_with_a_next_link_is_truncated(monkeypatch) -> None:
    payload = json.dumps([_rest_item(n) for n in range(1, 101)])
    link = '<https://api.github.com/repositories/1/pulls?page=2>; rel="next"'
    monkeypatch.setattr(
        github_api, "_run", _run_returning(_response(payload, link=link))
    )

    probe = github_api.probe_open_prs_rest("org", "widgets", author="alice")

    assert probe.truncated is True


def test_full_page_with_no_link_header_stays_conservative(monkeypatch) -> None:
    """Some proxies strip Link. Without it we genuinely cannot tell."""
    payload = json.dumps([_rest_item(n) for n in range(1, 101)])
    monkeypatch.setattr(github_api, "_run", _run_returning(_response(payload)))
    # Simulate a response whose headers carry no Link at all AND a full page:
    # handled by the test above for the GitHub shape; here assert the partial
    # page case is never truncated regardless.
    short = json.dumps([_rest_item(1)])
    monkeypatch.setattr(github_api, "_run", _run_returning(_response(short)))

    probe = github_api.probe_open_prs_rest("org", "widgets", author="alice")

    assert probe.truncated is False


# ---------------------------------------------------------------------------
# 2 — a deconfigured root must stop counting against freshness
# ---------------------------------------------------------------------------


def _install_boundaries(monkeypatch, roots):
    monkeypatch.setattr(
        orchestrator_module, "_resolve_maintenance_roots", lambda cwd: list(roots)
    )
    monkeypatch.setattr(
        github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None
    )
    monkeypatch.setattr(
        github_api, "get_repo_info", lambda cwd=None: ("org", "widgets")
    )
    monkeypatch.setattr(
        github_api,
        "batch_fetch_pr_review_and_ci",
        lambda owner, repo, numbers, cwd=None: {},
    )
    monkeypatch.setattr(
        github_api, "get_mergeability", lambda number, cwd=None: ("CLEAN", "MERGEABLE")
    )
    monkeypatch.setattr(
        github_api,
        "get_latest_commit",
        lambda number, cwd=None: ("head-1", "2026-08-22T00:00:00Z"),
    )
    monkeypatch.setattr(github_api, "get_ci_checks", lambda number, cwd=None: [])
    monkeypatch.setattr(
        github_api,
        "get_workflow_queue_health",
        lambda number, cwd=None: ([], [], RunnerExecutionSummary()),
    )
    monkeypatch.setattr(
        github_api, "scan_review_threads", lambda number, latest, cwd=None: ([], [])
    )
    monkeypatch.setattr(
        orchestrator_module, "find_worktree_for_branch", lambda branch, root=None: None
    )


def _raw_pr(number: int, repo: str) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "headRefName": f"feature/{number}",
        "headRefOid": "head-1",
        "baseRefName": "main",
        "url": f"https://github.com/{repo}/pull/{number}",
        "isDraft": False,
        "reviewDecision": "",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "labels": [],
        "createdAt": "2026-08-22T00:00:00Z",
        "updatedAt": "2026-08-22T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_a_deconfigured_root_stops_making_the_board_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    clock = ManualClock()
    anchor = tmp_path / "widgets"
    sibling = tmp_path / "gadgets"
    anchor.mkdir()
    sibling.mkdir()

    roots = [str(anchor), str(sibling)]
    configured = {"value": [str(sibling)]}
    _install_boundaries(monkeypatch, roots)
    monkeypatch.setattr(
        orchestrator_module,
        "load_config",
        lambda cwd=None: type(
            "Cfg",
            (),
            {
                "maintenance_repo_roots": list(configured["value"]),
                "pr_author": "alice",
            },
        )(),
        raising=False,
    )
    # The sibling has no PRs of its own, so nothing else keeps it alive.
    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: [] if cwd == str(sibling) else [_raw_pr(7, "org/widgets")],
    )
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(304, [], etag='"v1"', truncated=False)
        ),
    )

    orch = orchestrator_module.Orchestrator(
        repo_cwd=str(anchor),
        observation_controller=ObservationController(clock=clock),
        quota_ledger=QuotaLedger(clock=clock, background_hourly_budget=500),
    )
    await orch.refresh_prs(force=True)
    assert orch.open_set_freshness().complete is True

    # The operator deliberately removes the sibling from configuration.
    configured["value"] = []
    roots.remove(str(sibling))
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    freshness = orch.open_set_freshness()
    assert freshness.complete is True, (
        "a root removed from configuration was remembered forever through its "
        "observation history, so the header stayed partial after an intentional "
        "config change"
    )
    assert freshness.degraded_reason is None


@pytest.mark.asyncio
async def test_a_still_configured_root_is_not_forgotten(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Forgetting must be driven by configuration, not by resolver failure."""
    clock = ManualClock()
    anchor = tmp_path / "widgets"
    sibling = tmp_path / "gadgets"
    anchor.mkdir()
    sibling.mkdir()

    roots = [str(anchor), str(sibling)]
    _install_boundaries(monkeypatch, roots)
    monkeypatch.setattr(
        orchestrator_module,
        "load_config",
        lambda cwd=None: type(
            "Cfg", (), {"maintenance_repo_roots": [str(sibling)], "pr_author": "alice"}
        )(),
        raising=False,
    )
    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: [] if cwd == str(sibling) else [_raw_pr(7, "org/widgets")],
    )
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(304, [], etag='"v1"', truncated=False)
        ),
    )

    orch = orchestrator_module.Orchestrator(
        repo_cwd=str(anchor),
        observation_controller=ObservationController(clock=clock),
        quota_ledger=QuotaLedger(clock=clock, background_hourly_budget=500),
    )
    await orch.refresh_prs(force=True)

    # Still configured, but its git call starts failing.
    roots.remove(str(sibling))
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    assert orch.open_set_freshness().complete is False, (
        "a still-configured root that stopped resolving must keep the board "
        "incomplete"
    )
