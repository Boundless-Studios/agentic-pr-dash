"""BOU-3095 PR #169 review round 6 (codex).

Two quota regressions from the round-3 design change (the probe schedules the
relist), plus one more false-authority surface:

* with no ETag/Last-Modified available, every probe is an unconditional 200, so
  "a 200 schedules a relist" became a rich GraphQL relist every ~60s per root;
* the ETag covers the repo-wide ``/pulls`` page and author filtering happens
  client-side, so ANY other author's activity in a busy repo did the same;
* the Runner Issues tab renders authoritative "0 issues" / "No runner issues
  identified" text regardless of ``board_loaded``.

Both quota bugs share a fix: schedule on a change to the FILTERED projection we
actually track, not on the raw conditional result.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agentic_pr_dash import app, github_api, orchestrator as orchestrator_module
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


def _raw_pr(number: int = 7, *, updated_at: str = "2026-08-22T00:00:00Z") -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "headRefName": f"feature/{number}",
        "headRefOid": "head-1",
        "baseRefName": "main",
        "url": f"https://github.com/org/widgets/pull/{number}",
        "isDraft": False,
        "reviewDecision": "",
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "labels": [],
        "createdAt": "2026-08-22T00:00:00Z",
        "updatedAt": updated_at,
    }


def _probe_pr(number: int, *, updated_at: str = "2026-08-22T00:00:00Z") -> dict:
    raw = _raw_pr(number, updated_at=updated_at)
    raw["author"] = {"login": "alice"}
    return raw


@pytest.fixture
def boundaries(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        orchestrator_module, "_resolve_maintenance_roots", lambda cwd: ["/repos/widgets"]
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


def _orchestrator(clock: ManualClock, *, budget: int = 500):
    return orchestrator_module.Orchestrator(
        repo_cwd="/repos/widgets",
        observation_controller=ObservationController(clock=clock),
        quota_ledger=QuotaLedger(clock=clock, background_hourly_budget=budget),
    )


async def _tick(orch, clock, seconds: int = 90) -> None:
    clock.advance(timedelta(seconds=seconds))
    await orch.refresh_prs()


# ---------------------------------------------------------------------------
# 1 — no validator available: every probe is a 200, but nothing changed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validatorless_probes_do_not_relist_every_window(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """``gh api --include`` can return a 200 with no ETag/Last-Modified.

    The bootstrap then installs no validator, so every fast probe is an
    unconditional 200. "A 200 schedules a relist" therefore became a rich
    GraphQL relist roughly every 60-75s per root — against the same shared
    budget whose exhaustion caused the bug this PR is fixing.
    """
    clock = ManualClock()
    calls = {"list": 0}

    def list_open_prs(cwd=None):
        calls["list"] += 1
        return [_raw_pr(7)]

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        # No etag, no last_modified — a validator can never be installed.
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(200, [_probe_pr(7)], truncated=False)
        ),
    )

    orch = _orchestrator(clock)
    await orch.refresh_prs(force=True)
    seeded = calls["list"]

    for _ in range(5):
        await _tick(orch, clock)
        await _tick(orch, clock, orchestrator_module.POLL_INTERVAL_SECONDS)

    assert calls["list"] == seeded, (
        f"an unchanged projection triggered {calls['list'] - seeded} rich "
        "relists; a validatorless 200 must not schedule one every window"
    )


@pytest.mark.asyncio
async def test_a_real_change_still_relists_without_a_validator(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """The latency fix must survive: a genuine change still reconciles fast."""
    clock = ManualClock()
    open_prs = [_raw_pr(7), _raw_pr(8)]
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: list(open_prs))
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(
                200, [_probe_pr(p["number"]) for p in open_prs], truncated=False
            )
        ),
    )

    orch = _orchestrator(clock)
    await orch.refresh_prs(force=True)
    assert {n for _r, n in orch.prs} == {7, 8}

    open_prs[:] = [_raw_pr(7)]
    await _tick(orch, clock)
    await _tick(orch, clock, orchestrator_module.POLL_INTERVAL_SECONDS)

    assert {n for _r, n in orch.prs} == {7}


# ---------------------------------------------------------------------------
# 2 — another author's churn must not spend our GraphQL budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unrelated_author_activity_does_not_schedule_a_relist(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """The ETag covers the repo-wide page; author filtering is client-side.

    In a busy repo any other author's open/update/merge flips the conditional
    request to 200 even though our filtered projection is byte-identical.
    """
    clock = ManualClock()
    calls = {"list": 0}
    etag = {"value": '"v1"'}

    def list_open_prs(cwd=None):
        calls["list"] += 1
        return [_raw_pr(7)]

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)

    def probe(owner, repo, *, etag=None, last_modified=None, author=None, cwd=None):
        # Someone else keeps changing the page, so this is always a 200 with a
        # new ETag — but OUR author's filtered projection never moves.
        return github_api.ConditionalPRListProbe(
            200, [_probe_pr(7)], etag='"other-author-churn"', truncated=False
        )

    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)

    orch = _orchestrator(clock)
    await orch.refresh_prs(force=True)
    seeded = calls["list"]
    assert etag["value"]  # keep the fixture honest

    for _ in range(5):
        await _tick(orch, clock)
        await _tick(orch, clock, orchestrator_module.POLL_INTERVAL_SECONDS)

    assert calls["list"] == seeded, (
        f"unrelated authors' activity caused {calls['list'] - seeded} rich "
        "relists against the shared background budget"
    )


@pytest.mark.asyncio
async def test_our_own_comment_activity_still_schedules_a_relist(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """updatedAt moving on OUR PR is a real change and must reconcile."""
    clock = ManualClock()
    calls = {"list": 0}
    updated = {"value": "2026-08-22T00:00:00Z"}

    def list_open_prs(cwd=None):
        calls["list"] += 1
        return [_raw_pr(7, updated_at=updated["value"])]

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(
                200, [_probe_pr(7, updated_at=updated["value"])], truncated=False
            )
        ),
    )

    orch = _orchestrator(clock)
    await orch.refresh_prs(force=True)
    seeded = calls["list"]

    updated["value"] = "2026-08-22T04:45:00Z"
    await _tick(orch, clock)
    await _tick(orch, clock, orchestrator_module.POLL_INTERVAL_SECONDS)

    assert calls["list"] > seeded, (
        "a comment on our own PR moved updatedAt but no reconciliation followed"
    )


# ---------------------------------------------------------------------------
# 3 — the Runner Issues tab must honour board_loaded too
# ---------------------------------------------------------------------------


def test_runner_issues_tab_does_not_assert_zero_before_observation(
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        app.orchestrator,
        "observation_controller",
        ObservationController(clock=lambda: now),
    )
    monkeypatch.setattr(
        app.orchestrator,
        "open_set_freshness",
        lambda: orchestrator_module.ObservationFreshness(
            observed_at=None,
            degraded_reason="rich metadata deferred: quota background_hourly_budget",
            complete=False,
        ),
    )

    context = app.runner_dashboard_context()
    template = app.templates.env.get_template("partials/runner_issues.html")
    html = template.render(**context)

    assert context["board_loaded"] is False
    assert "No runner issues identified" not in html, (
        "the Runner Issues tab asserted an authoritative empty result while "
        "GitHub had never been observed"
    )
    assert "No GitHub jobs currently running" not in html


def test_runner_issues_tab_reports_normally_once_observed(monkeypatch) -> None:
    now = datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        app.orchestrator,
        "observation_controller",
        ObservationController(clock=lambda: now),
    )
    monkeypatch.setattr(
        app.orchestrator,
        "open_set_freshness",
        lambda: orchestrator_module.ObservationFreshness(
            observed_at=now - timedelta(seconds=5),
            degraded_reason=None,
            complete=True,
        ),
    )

    context = app.runner_dashboard_context()
    template = app.templates.env.get_template("partials/runner_issues.html")
    html = template.render(**context)

    assert context["board_loaded"] is True
    assert "No runner issues identified" in html


def test_runner_issues_partial_still_serves(monkeypatch) -> None:
    """Whatever the state, the endpoint must not break."""
    client = TestClient(app.app)
    assert client.get("/partials/runner-issues").status_code == 200
