"""BOU-3095 PR #169 review round 2 (codex).

Three more holes in making the cheap REST probe authoritative:

* the probe reads only page 1 of the repo's open PRs and filters by author
  *afterwards*, so on a repo with >100 open PRs an older still-open PR of the
  configured author falls outside the page — and pruning from that truncated
  body removes it from the board;
* on a rich-due tick the probe's ``updatedAt`` values are dropped on the floor,
  which is the one tick where the probe actually detected a change;
* the runner tab renders the freshness indicator but never refreshes it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import subprocess

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


def _raw_pr(number: int = 7, *, updated_at: str | None = "2026-08-22T00:00:00Z") -> dict:
    raw = {
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
    }
    if updated_at is not None:
        raw["updatedAt"] = updated_at
    return raw


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


def _orchestrator(clock: ManualClock, *, budget: int = 0):
    return orchestrator_module.Orchestrator(
        repo_cwd="/repos/widgets",
        observation_controller=ObservationController(clock=clock),
        quota_ledger=QuotaLedger(clock=clock, background_hourly_budget=budget),
    )


def _tracked_numbers(orch) -> set[int]:
    return {number for _repo, number in orch.prs}


# ---------------------------------------------------------------------------
# 1 (P1) — a truncated probe page is not an authoritative open set
# ---------------------------------------------------------------------------


def test_probe_reports_a_full_page_as_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full page means "there may be more", and the author filter runs after."""
    items = [
        {
            "number": n,
            "user": {"login": "someone-else"},
            "head": {"sha": "h", "ref": f"f/{n}", "repo": {"owner": {}}},
            "base": {"ref": "main"},
            "html_url": f"https://github.com/org/widgets/pull/{n}",
        }
        for n in range(1, 101)
    ]
    payload = json.dumps(items)
    monkeypatch.setattr(
        github_api,
        "_run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0] if a else [],
            0,
            stdout=f'HTTP/2 200 OK\r\nETag: "v2"\r\n\r\n{payload}',
            stderr="",
        ),
    )

    probe = github_api.probe_open_prs_rest("org", "widgets", author="alice")

    assert probe.changed is True
    assert probe.prs == []          # none authored by alice on this page
    assert probe.truncated is True  # ...but the page was full, so we cannot say


def test_probe_reports_a_partial_page_as_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        [
            {
                "number": 7,
                "user": {"login": "alice"},
                "head": {"sha": "h", "ref": "f/7", "repo": {"owner": {}}},
                "base": {"ref": "main"},
                "html_url": "https://github.com/org/widgets/pull/7",
            }
        ]
    )
    monkeypatch.setattr(
        github_api,
        "_run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0] if a else [],
            0,
            stdout=f'HTTP/2 200 OK\r\nETag: "v2"\r\n\r\n{payload}',
            stderr="",
        ),
    )

    probe = github_api.probe_open_prs_rest("org", "widgets", author="alice")

    assert probe.truncated is False


@pytest.mark.asyncio
async def test_truncated_probe_never_prunes(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """The author's older PR can sit outside page 1 of a busy repo."""
    clock = ManualClock()
    monkeypatch.setattr(
        github_api, "list_open_prs", lambda cwd=None: [_raw_pr(7), _raw_pr(8)]
    )
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(
                200, [_probe_pr(8)], etag='"v2"', truncated=True
            )
        ),
    )

    orch = _orchestrator(clock, budget=0)
    await orch.refresh_prs(force=True)
    assert _tracked_numbers(orch) == {7, 8}

    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    assert _tracked_numbers(orch) == {7, 8}, (
        "a truncated first page was treated as the authoritative open set and "
        "pruned a PR that simply fell outside it"
    )


@pytest.mark.asyncio
async def test_an_untruncated_probe_still_only_schedules_the_relist(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """Even a complete page does not remove a PR by itself.

    Review round 3 withdrew the probe's authority over removal entirely rather
    than keep guarding individual ways it can be wrong, so ``truncated`` no
    longer changes what may be pruned — it only remains as an accurate signal.
    What the probe still does, and what makes the merged PR clear in about a
    minute, is get the authoritative relist scheduled promptly.
    """
    clock = ManualClock()
    open_prs = [_raw_pr(7), _raw_pr(8)]
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: list(open_prs))
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(
                200, [_probe_pr(8)], etag='"v2"', truncated=False
            )
        ),
    )

    orch = _orchestrator(clock, budget=500)
    await orch.refresh_prs(force=True)
    assert _tracked_numbers(orch) == {7, 8}

    open_prs[:] = [_raw_pr(8)]

    # The probe alone changes nothing...
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    # ...the relist it scheduled is what prunes.
    clock.advance(timedelta(seconds=orchestrator_module.POLL_INTERVAL_SECONDS))
    await orch.refresh_prs()
    assert _tracked_numbers(orch) == {8}


# ---------------------------------------------------------------------------
# 2 (P1) — the rich tick must not discard the probe's activity timestamps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rich_refresh_carries_probe_updated_at(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """The rich tick is exactly when the probe detected the change.

    Production ``PR_SNAPSHOT_FIELDS`` omits ``updatedAt``, so replacing the
    probe body with the rich list drops the only activity signal — and the
    validator has just been advanced, so the following probes return 304 and
    the review slice is never invalidated.
    """
    clock = ManualClock()
    review_scans: list[int] = []
    probe_updated = {"value": "2026-08-22T00:00:00Z"}

    # Production shape: the rich list carries no updatedAt at all.
    monkeypatch.setattr(
        github_api, "list_open_prs", lambda cwd=None: [_raw_pr(7, updated_at=None)]
    )
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(
                200, [_probe_pr(7, updated_at=probe_updated["value"])], etag='"v2"'
            )
        ),
    )
    monkeypatch.setattr(
        github_api,
        "scan_review_threads_observation",
        lambda number, latest, cwd=None: (
            review_scans.append(number)
            or github_api.ObservationReadResult.observed(([], []))
        ),
    )

    orch = _orchestrator(clock, budget=500)
    await orch.refresh_prs(force=True)
    scans_after_seed = len(review_scans)
    assert scans_after_seed > 0

    # A comment lands; the next tick due is the 15-minute RICH one.
    probe_updated["value"] = "2026-08-22T04:45:00Z"
    clock.advance(timedelta(minutes=16))
    await orch.refresh_prs()
    clock.advance(timedelta(seconds=orchestrator_module.POLL_INTERVAL_SECONDS))
    await orch.refresh_prs()

    assert len(review_scans) > scans_after_seed, (
        "the rich refresh replaced the probe body and dropped its updatedAt, so "
        "the review slice was never invalidated"
    )


# ---------------------------------------------------------------------------
# 3 (P2) — the runner tab's own poll must refresh the freshness indicator
# ---------------------------------------------------------------------------


def test_runner_issues_partial_emits_the_observation_slot() -> None:
    from fastapi.testclient import TestClient

    client = TestClient(app.app)
    partial = client.get("/partials/runner-issues").text

    assert partial.count('id="observation-age-slot"') == 1, (
        "the runner tab polls every five seconds but never replaces the header "
        "indicator, so an initially fresh label stays fresh through an outage"
    )
    assert 'hx-swap-oob="true"' in partial


def test_runner_full_page_renders_the_slot_once() -> None:
    from fastapi.testclient import TestClient

    client = TestClient(app.app)
    client.get("/partials/runner-issues")
    page = client.get("/?tab=runner_issues").text

    assert page.count('id="observation-age-slot"') == 1
    assert "hx-swap-oob" not in page
