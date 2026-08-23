"""BOU-3095 PR #169 review round 8 (codex).

Three consequences of the round-6 projection comparison:

* an unchanged projection behind a 200 is a successful observation of the
  tracked open set, but was not counted as one — so the freshness label aged
  while every probe was succeeding;
* a detected change whose reconciling relist then failed advanced the baseline
  anyway, so the change was forgotten until the slow clock;
* ``board_loaded`` required *an* observation, not observation of every watched
  root, so a multi-repo board could assert "no worktrees" with a whole sibling
  repo missing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

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


# ---------------------------------------------------------------------------
# 1 — an unchanged projection behind a 200 IS an observation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unchanged_projection_behind_a_200_counts_as_observed(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """Otherwise the label ages while every probe succeeds.

    A repo-wide ETag that moved because another author was active — or a setup
    where no validator can be installed — yields a 200 whose filtered projection
    is identical. That is a successful confirmation that our open set is
    current, and must reset the observation age.
    """
    clock = ManualClock()
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr(7)])
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(
                200, [_probe_pr(7)], etag='"churn"', truncated=False
            )
        ),
    )

    orch = _orchestrator(clock)
    await orch.refresh_prs(force=True)

    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    assert orch.open_set_freshness().age_seconds(clock.current) == 0.0, (
        "a successful probe confirming an unchanged projection did not reset "
        "the observation age, so the header goes stale while nothing is wrong"
    )


@pytest.mark.asyncio
async def test_changed_projection_is_not_an_observation_until_reconciled(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """The board is not current while a detected change is unreconciled."""
    clock = ManualClock()
    updated = {"value": "2026-08-22T00:00:00Z"}
    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: [_raw_pr(7, updated_at=updated["value"])],
    )
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(
                200, [_probe_pr(7, updated_at=updated["value"])], truncated=False
            )
        ),
    )

    orch = _orchestrator(clock, budget=0)  # relist will be denied
    await orch.refresh_prs(force=True)

    updated["value"] = "2026-08-22T05:00:00Z"
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    assert orch.open_set_freshness().age_seconds(clock.current) == 90.0


# ---------------------------------------------------------------------------
# 2 — a detected change must survive a failed reconciling relist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detected_change_is_retried_after_a_denied_relist(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """Advancing the baseline on a failed relist forgets the change.

    The event is then postponed by a full reconciliation interval and later
    probes compare against the new baseline, so nothing re-schedules it.
    """
    clock = ManualClock()
    open_prs = [_raw_pr(7), _raw_pr(8)]
    budget = {"value": 0}

    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: list(open_prs))
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(
                200,
                [_probe_pr(p["number"]) for p in open_prs],
                etag='"v2"',
                truncated=False,
            )
        ),
    )

    ledger = QuotaLedger(clock=clock, background_hourly_budget=500)
    orch = orchestrator_module.Orchestrator(
        repo_cwd="/repos/widgets",
        observation_controller=ObservationController(clock=clock),
        quota_ledger=ledger,
    )
    await orch.refresh_prs(force=True)
    assert {n for _r, n in orch.prs} == {7, 8}

    # PR 8 merges while the rich relist is unavailable.
    open_prs[:] = [_raw_pr(7)]
    monkeypatch.setattr(
        orchestrator_module.Orchestrator,
        "_rich_metadata_list",
        lambda self, root, now, *, force=False, bootstrap=True: _denied(self, root),
    )

    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()
    clock.advance(timedelta(seconds=orchestrator_module.POLL_INTERVAL_SECONDS))
    await orch.refresh_prs()
    assert {n for _r, n in orch.prs} == {7, 8}  # still unreconciled, as expected

    # The relist becomes available again. The change must still be pending.
    monkeypatch.undo()
    _reinstall(monkeypatch, open_prs)
    assert budget["value"] == 0  # keep the fixture honest

    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()
    clock.advance(timedelta(seconds=orchestrator_module.POLL_INTERVAL_SECONDS))
    await orch.refresh_prs()

    assert {n for _r, n in orch.prs} == {7}, (
        "the projection baseline advanced on a failed relist, so the detected "
        "change was forgotten until the slow reconciliation clock"
    )


async def _denied(self, root):
    return self._metadata_cache.get(root), False


def _reinstall(monkeypatch, open_prs):
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
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: list(open_prs))
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(
                200,
                [_probe_pr(p["number"]) for p in open_prs],
                etag='"v2"',
                truncated=False,
            )
        ),
    )


# ---------------------------------------------------------------------------
# 3 — board_loaded needs every watched root, not merely one
# ---------------------------------------------------------------------------


def test_board_stays_loading_while_a_watched_root_is_unobserved(monkeypatch) -> None:
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
            degraded_reason="1 watched repo root(s) are not being refreshed",
            complete=False,
        ),
    )

    context = app._dashboard_context_from_cards(
        [], 0, 0, show_agent_worktrees=False, active_tab="board"
    )
    html = app.templates.env.get_template("partials/board.html").render(**context)

    assert context["board_loaded"] is False, (
        "the anchor was observed but a sibling repo was not, so an empty board "
        "is not an answer about the whole watched set"
    )
    assert "No worktrees" not in html


def test_board_loads_when_every_watched_root_is_observed(monkeypatch) -> None:
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

    context = app._dashboard_context_from_cards(
        [], 0, 0, show_agent_worktrees=False, active_tab="board"
    )

    assert context["board_loaded"] is True
