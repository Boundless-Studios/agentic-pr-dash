"""BOU-3095 PR #169 review round 4 (codex).

Five findings, mostly in the freshness machinery this PR added:

* rendering a partial resolved repo roots on the request path, which shells out
  ``git worktree list`` with a 10-second timeout PER ROOT — on the event loop,
  on every five-second poll, from three tabs;
* a 304 against a validator that was established from a truncated page proves
  only that page, not the whole open set;
* a sibling root whose very first resolution fails is invisible to the retention
  logic, so the board still reports complete;
* ``board_loaded`` came from the local scan alone, so a GitHub outage at startup
  produced confident "No worktrees" columns — the false-empty board again;
* the stale threshold was documented as three validation windows but hardcoded.
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


def _raw_pr(number: int = 7, *, repo: str = "org/widgets") -> dict:
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


@pytest.fixture
def boundaries(monkeypatch: pytest.MonkeyPatch):
    _install_boundaries(monkeypatch, ["/repos/widgets"])


def _orchestrator(clock: ManualClock, *, budget: int = 500):
    return orchestrator_module.Orchestrator(
        repo_cwd="/repos/widgets",
        observation_controller=ObservationController(clock=clock),
        quota_ledger=QuotaLedger(clock=clock, background_hourly_budget=budget),
    )


# ---------------------------------------------------------------------------
# 1 (P2, high impact) — rendering must not resolve roots on the event loop
# ---------------------------------------------------------------------------


def test_observation_context_does_not_resolve_repo_roots(monkeypatch) -> None:
    """_resolve_maintenance_roots runs `git worktree list` per root, 10s timeout.

    Calling it from a partial render puts that on the async request thread, on
    every five-second poll, from three tabs at once.
    """
    calls: list[str] = []

    def exploding_resolver(cwd):
        calls.append(cwd)
        raise AssertionError(
            "rendering resolved repo roots — this shells out git per root on "
            "the event loop"
        )

    monkeypatch.setattr(
        orchestrator_module, "_resolve_maintenance_roots", exploding_resolver
    )

    app._observation_context()

    assert calls == []


def test_open_set_freshness_does_not_resolve_repo_roots(monkeypatch) -> None:
    """Count calls rather than raising: _repo_roots swallows exceptions."""
    clock = ManualClock()
    _install_boundaries(monkeypatch, ["/repos/widgets"])
    orch = _orchestrator(clock)

    calls: list[str] = []
    monkeypatch.setattr(
        orchestrator_module,
        "_resolve_maintenance_roots",
        lambda cwd: calls.append(cwd) or ["/repos/widgets"],
    )

    orch.open_set_freshness()

    assert calls == [], (
        "open_set_freshness resolved repo roots, which shells out git per root"
    )


# ---------------------------------------------------------------------------
# 2 (P1) — a 304 against a truncated validator proves only that page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_304_from_a_truncated_validator_is_not_a_full_observation(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    clock = ManualClock()
    truncated = {"value": True}

    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr(7)])

    def probe(owner, repo, *, etag=None, last_modified=None, author=None, cwd=None):
        if etag is None:
            # Establishing the validator from a full first page.
            return github_api.ConditionalPRListProbe(
                200, [_raw_pr(7)], etag='"v1"', truncated=truncated["value"]
            )
        return github_api.ConditionalPRListProbe(304, [], etag='"v1"')

    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)

    orch = _orchestrator(clock)
    await orch.refresh_prs(force=True)
    baseline = orch.open_set_freshness().observed_at

    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    freshness = orch.open_set_freshness()
    assert freshness.observed_at == baseline, (
        "a 304 against a validator built from a truncated page refreshed the "
        "observation timestamp, claiming the whole open set was confirmed"
    )


@pytest.mark.asyncio
async def test_304_from_a_complete_validator_is_a_full_observation(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """The normal case must still count, or the age indicator would never reset."""
    clock = ManualClock()
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr(7)])

    def probe(owner, repo, *, etag=None, last_modified=None, author=None, cwd=None):
        if etag is None:
            return github_api.ConditionalPRListProbe(
                200, [_raw_pr(7)], etag='"v1"', truncated=False
            )
        return github_api.ConditionalPRListProbe(304, [], etag='"v1"')

    monkeypatch.setattr(github_api, "probe_open_prs_rest", probe)

    orch = _orchestrator(clock)
    await orch.refresh_prs(force=True)
    baseline = orch.open_set_freshness().observed_at

    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    assert orch.open_set_freshness().observed_at != baseline
    assert orch.open_set_freshness().age_seconds(clock.current) == 0.0


# ---------------------------------------------------------------------------
# 3 (P2) — a root that has NEVER resolved must still count as watched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_never_resolved_configured_root_keeps_freshness_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Round 3 only retained roots that had succeeded at least once.

    A sibling whose very first `git worktree list` fails has never been
    observed, degraded, or assigned a PR, so it was absent from every retained
    set and the anchor happily reported complete.
    """
    clock = ManualClock()
    anchor = tmp_path / "widgets"
    sibling = tmp_path / "gadgets"
    anchor.mkdir()
    sibling.mkdir()

    # The resolver only ever returns the anchor: the sibling's git call fails.
    _install_boundaries(monkeypatch, [str(anchor)])
    monkeypatch.setattr(
        orchestrator_module,
        "load_config",
        lambda cwd=None: type(
            "Cfg", (), {"maintenance_repo_roots": [str(sibling)], "pr_author": "alice"}
        )(),
        raising=False,
    )
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr(7)])
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(304, [], etag='"v1"')
        ),
    )

    orch = orchestrator_module.Orchestrator(
        repo_cwd=str(anchor),
        observation_controller=ObservationController(clock=clock),
        quota_ledger=QuotaLedger(clock=clock, background_hourly_budget=500),
    )
    await orch.refresh_prs(force=True)

    assert orch.open_set_freshness().complete is False, (
        "a configured sibling that never resolved was excluded from the watched "
        "set, so the board reported complete while that repo was absent"
    )


# ---------------------------------------------------------------------------
# 4 (P2) — board_loaded must also require a GitHub observation
# ---------------------------------------------------------------------------


def test_board_stays_loading_until_github_has_been_observed(monkeypatch) -> None:
    """Otherwise a startup outage yields confident "No worktrees" columns."""
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

    context = app._dashboard_context_from_cards(
        [], 0, 0, show_agent_worktrees=False, active_tab="board"
    )
    template = app.templates.env.get_template("partials/board.html")
    html = template.render(**context)

    assert context["board_loaded"] is False, (
        "the local scan finished but GitHub was never observed, so the board "
        "must not assert an empty PR set"
    )
    assert "No worktrees" not in html


def test_board_loads_once_github_has_been_observed(monkeypatch) -> None:
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
            observed_at=now - timedelta(seconds=10),
            degraded_reason=None,
            complete=True,
        ),
    )

    context = app._dashboard_context_from_cards(
        [], 0, 0, show_agent_worktrees=False, active_tab="board"
    )

    assert context["board_loaded"] is True


# ---------------------------------------------------------------------------
# 5 (P2) — the stale threshold must track the configured probe interval
# ---------------------------------------------------------------------------


def test_stale_threshold_tracks_the_validation_interval(monkeypatch) -> None:
    """Documented as three validation windows; it must actually be derived.

    Asserting the number equals 3x60 would pass on today's default by
    coincidence, so drive a non-default interval and check the threshold moves
    with it.
    """
    assert app._stale_after_seconds() == pytest.approx(
        3 * orchestrator_module.LIST_VALIDATION_INTERVAL.total_seconds()
    )

    monkeypatch.setattr(
        orchestrator_module, "LIST_VALIDATION_INTERVAL", timedelta(seconds=300)
    )
    assert app._stale_after_seconds() == pytest.approx(900), (
        "a tuned APD_LIST_VALIDATION_INTERVAL_S left the stale threshold at its "
        "hardcoded default, so healthy data reads as stale before the next "
        "probe is even due"
    )


def test_a_tuned_interval_does_not_mark_healthy_data_stale(monkeypatch) -> None:
    """The concrete symptom: a 5-minute interval with a 180s threshold."""
    now = datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        orchestrator_module, "LIST_VALIDATION_INTERVAL", timedelta(seconds=300)
    )
    monkeypatch.setattr(
        app.orchestrator,
        "observation_controller",
        ObservationController(clock=lambda: now),
    )
    monkeypatch.setattr(
        app.orchestrator,
        "open_set_freshness",
        lambda: orchestrator_module.ObservationFreshness(
            # Four minutes old: stale under a fixed 180s, healthy under 3x300s.
            observed_at=now - timedelta(seconds=240),
            degraded_reason=None,
            complete=True,
        ),
    )

    assert app._observation_context()["stale"] is False
