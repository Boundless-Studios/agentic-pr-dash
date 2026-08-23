"""BOU-3095 PR #169 review round 3 (codex).

* the all-empty safeguard covered only the cheap pass, so the scheduled
  follow-up tick could still prune the whole board when the confirming relist
  failed — i.e. it failed exactly when it was needed;
* freshness completeness was computed from the roots the resolver happened to
  return, so a silently dropped sibling read as complete and fresh while its
  cards went unrefreshed;
* the Worktrees tab renders the freshness indicator but never refreshes it.
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


def _probe_pr(number: int) -> dict:
    raw = _raw_pr(number)
    raw["author"] = {"login": "alice"}
    return raw


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


def _orchestrator(clock: ManualClock, *, budget: int = 0):
    return orchestrator_module.Orchestrator(
        repo_cwd="/repos/widgets",
        observation_controller=ObservationController(clock=clock),
        quota_ledger=QuotaLedger(clock=clock, background_hourly_budget=budget),
    )


def _tracked_numbers(orch) -> set[int]:
    return {number for _repo, number in orch.prs}


# ---------------------------------------------------------------------------
# 1 (P1) — the all-empty safeguard must survive the confirming relist failing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_probe_never_prunes_when_the_confirming_relist_fails(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """The safeguard has to hold on the follow-up tick, not just the first one.

    The cheap pass defers an all-empty result to a rich relist. If that relist
    is then quota-denied, exposing the empty probe set as authoritative prunes
    the entire board — the safeguard failing at precisely the moment it exists
    for.
    """
    clock = ManualClock()
    probe_prs: list[dict] = [_probe_pr(7), _probe_pr(8)]

    monkeypatch.setattr(
        github_api, "list_open_prs", lambda cwd=None: [_raw_pr(7), _raw_pr(8)]
    )
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(200, list(probe_prs), etag='"v2"')
        ),
    )

    orch = _orchestrator(clock, budget=0)
    await orch.refresh_prs(force=True)
    assert _tracked_numbers(orch) == {7, 8}

    # Spuriously empty from here on, with the rich relist denied (budget 0).
    probe_prs.clear()

    # Cheap pass defers and schedules the relist...
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()
    assert _tracked_numbers(orch) == {7, 8}

    # ...and the scheduled follow-up tick, where the relist fails, must also
    # refuse to prune.
    clock.advance(timedelta(seconds=orchestrator_module.POLL_INTERVAL_SECONDS))
    await orch.refresh_prs()
    assert _tracked_numbers(orch) == {7, 8}, (
        "the empty probe was exposed as authoritative once the confirming rich "
        "relist failed, wiping the board"
    )

    # And it must keep holding while GraphQL stays unavailable.
    clock.advance(timedelta(minutes=20))
    await orch.refresh_prs()
    assert _tracked_numbers(orch) == {7, 8}


@pytest.mark.asyncio
async def test_a_successful_relist_still_prunes_a_genuine_close_out(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """The safeguard must not become a permanent refusal to prune."""
    clock = ManualClock()
    open_prs: list[dict] = [_raw_pr(7)]

    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: list(open_prs))
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(
                200, [_probe_pr(p["number"]) for p in open_prs], etag='"v2"'
            )
        ),
    )

    orch = _orchestrator(clock, budget=500)
    await orch.refresh_prs(force=True)
    assert _tracked_numbers(orch) == {7}

    open_prs.clear()  # genuinely closed
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()
    clock.advance(timedelta(seconds=orchestrator_module.POLL_INTERVAL_SECONDS))
    await orch.refresh_prs()

    assert _tracked_numbers(orch) == set(), (
        "a real close-out confirmed by a successful relist must still prune"
    )


# ---------------------------------------------------------------------------
# 2 (P2) — a silently dropped sibling root must not read as complete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dropped_sibling_root_keeps_freshness_incomplete(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """_resolve_maintenance_roots drops a root whose git command fails.

    Its cards stay in self.prs and stop being refreshed, so reporting the
    remaining roots as complete and fresh is a lie about the whole board.
    """
    clock = ManualClock()
    roots = ["/repos/widgets", "/repos/gadgets"]
    _install_boundaries(monkeypatch, roots)
    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: [
            _raw_pr(9, repo="org/gadgets")
            if cwd == "/repos/gadgets"
            else _raw_pr(7, repo="org/widgets")
        ],
    )
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(304, [], etag='"v1"')
        ),
    )

    orch = _orchestrator(clock, budget=500)
    await orch.refresh_prs(force=True)
    assert orch.open_set_freshness().complete is True

    # The sibling's git worktree list times out; the resolver silently omits it.
    roots.remove("/repos/gadgets")
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    freshness = orch.open_set_freshness()
    assert freshness.complete is False, (
        "a dropped sibling root was excluded from the completeness calculation, "
        "so the board reported fully fresh while that repo went unrefreshed"
    )


# ---------------------------------------------------------------------------
# 3 (P2) — every polling tab must refresh the freshness indicator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "partial_url",
    ["/partials/board", "/partials/runner-issues", "/partials/worktrees"],
)
def test_every_polling_partial_refreshes_the_observation_slot(
    partial_url: str,
) -> None:
    client = TestClient(app.app)

    partial = client.get(partial_url).text

    assert partial.count('id="observation-age-slot"') == 1, (
        f"{partial_url} polls every five seconds but never replaces the header "
        "indicator, so an initially fresh label survives an outage"
    )
    assert 'hx-swap-oob="true"' in partial


@pytest.mark.parametrize("tab", ["board", "runner_issues", "worktrees"])
def test_every_full_page_renders_the_slot_once(tab: str) -> None:
    client = TestClient(app.app)

    client.get("/partials/board")
    client.get("/partials/runner-issues")
    client.get("/partials/worktrees")
    page = client.get(f"/?tab={tab}").text

    assert page.count('id="observation-age-slot"') == 1
    assert "hx-swap-oob" not in page
