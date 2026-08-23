"""BOU-3095 PR #169 review follow-ups (codex).

Seven defects the freshness change introduced or left open. The first is the
serious one: making the probe body authoritative for pruning turned an existing
author-matching gap into a board-wipe.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import subprocess

import pytest

from agentic_pr_dash import app, github_api, orchestrator as orchestrator_module
from agentic_pr_dash.models import RunnerExecutionSummary
from agentic_pr_dash.observation import ObservationController, ObservationSlice
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


def _orchestrator(
    clock: ManualClock, *, budget: int = 0
) -> orchestrator_module.Orchestrator:
    return orchestrator_module.Orchestrator(
        repo_cwd="/repos/widgets",
        observation_controller=ObservationController(clock=clock),
        quota_ledger=QuotaLedger(clock=clock, background_hourly_budget=budget),
    )


def _tracked_numbers(orch) -> set[int]:
    return {number for _repo, number in orch.prs}


# ---------------------------------------------------------------------------
# 1 (P1) — App-identity authors must not read as "no open PRs"
# ---------------------------------------------------------------------------


def test_probe_matches_app_author_against_rest_bot_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``app/<name>`` and ``<name>[bot]`` are the same identity.

    Before the open set was derived from the probe, this mismatch only cost a
    wasted relist. Now an unmatched author means an empty authoritative open
    set — i.e. prune every open PR off the board.
    """
    payload = json.dumps(
        [
            {
                "number": 7,
                "user": {"login": "gaia-bot[bot]"},
                "head": {"sha": "h", "ref": "feature/7", "repo": {"owner": {}}},
                "base": {"ref": "main"},
                "html_url": "https://github.com/org/widgets/pull/7",
            }
        ]
    )

    monkeypatch.setattr(
        github_api,
        "_run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0] if a else [], 0, stdout=f"HTTP/2 200 OK\r\nETag: \"v2\"\r\n\r\n{payload}", stderr=""
        ),
    )

    probe = github_api.probe_open_prs_rest(
        "org", "widgets", author="app/gaia-bot"
    )

    assert probe.changed is True
    assert [pr["number"] for pr in probe.prs] == [7], (
        "the App-spelled author did not match the REST bot login, so the "
        "authoritative open set came back empty"
    )


@pytest.mark.asyncio
async def test_a_cheap_probe_alone_never_empties_the_whole_board(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """Defence in depth for the class of bug above.

    "Every PR I own closed at once" is rare; "my author filter stopped
    matching" is not. Fixing the author comparison removes the known instance,
    but the blast radius of the next one is the entire board, so an empty open
    set from the cheap path must be confirmed by a real relist before it is
    allowed to prune everything. A partial shrink still prunes immediately —
    only the all-gone case is held back.
    """
    clock = ManualClock()
    probe_prs: list[dict] = [_probe_pr(7), _probe_pr(8)]
    calls = {"list": 0}

    def list_open_prs(cwd=None):
        calls["list"] += 1
        return [_raw_pr(7), _raw_pr(8)]

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)
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

    # The probe suddenly reports nothing at all, with the rich relist denied.
    probe_prs.clear()
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    assert _tracked_numbers(orch) == {7, 8}, (
        "a cheap probe returning an empty body wiped the entire board without "
        "any authoritative confirmation"
    )


# ---------------------------------------------------------------------------
# 2 (P2) — a PR the fast probe reveals must not wait out the rich interval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_revealing_an_unknown_pr_schedules_a_rich_relist(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    clock = ManualClock()
    known = [_raw_pr(7)]
    probe_numbers = [7]
    calls = {"list": 0}

    def list_open_prs(cwd=None):
        calls["list"] += 1
        return [_raw_pr(n) for n in probe_numbers]

    monkeypatch.setattr(github_api, "list_open_prs", list_open_prs)
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(
                200, [_probe_pr(n) for n in probe_numbers], etag='"v2"'
            )
        ),
    )

    orch = _orchestrator(clock, budget=500)
    await orch.refresh_prs(force=True)
    assert _tracked_numbers(orch) == {7}
    assert known  # keep the fixture honest about what was seeded

    # PR 9 opens; no webhook was delivered, so only the probe knows.
    probe_numbers.append(9)
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    # The next tick must pick it up rather than waiting out the 15-minute clock.
    clock.advance(timedelta(seconds=orchestrator_module.POLL_INTERVAL_SECONDS))
    await orch.refresh_prs()

    assert 9 in _tracked_numbers(orch), (
        "a PR only the fast probe saw stayed invisible until the rich "
        "reconciliation interval"
    )


# ---------------------------------------------------------------------------
# 3 (P2) — the runner tab must not claim the board is still loading
# ---------------------------------------------------------------------------


def test_runner_tab_context_carries_observation_freshness() -> None:
    context = app.runner_dashboard_context()

    assert "observation" in context, (
        "the runner tab renders the shared header include, so without this the "
        "indicator falls back to 'PR data loading' forever"
    )
    assert "board_loaded" in context


# ---------------------------------------------------------------------------
# 4 (P2) — a failing rebuild must not evict a newer task's entry
# ---------------------------------------------------------------------------


def test_failed_rebuild_does_not_pop_a_replacement_task(monkeypatch) -> None:
    import asyncio

    async def scenario() -> None:
        app._dashboard_context_cache.clear()
        app._dashboard_context_tasks.clear()
        key = (False, "board")
        monkeypatch.setattr(app.orchestrator, "log", lambda *a, **k: None)

        failing_started = asyncio.Event()
        release = asyncio.Event()

        async def fake_to_thread(func, **kwargs):
            failing_started.set()
            await release.wait()
            raise RuntimeError("scan exploded")

        monkeypatch.setattr(app.asyncio, "to_thread", fake_to_thread)

        await app._dashboard_context_async()
        await failing_started.wait()
        old_task = app._dashboard_context_tasks[key]

        # A refresh clears the map; a later poll installs a replacement.
        app._dashboard_context_tasks.clear()
        sentinel = asyncio.create_task(asyncio.sleep(0.5))
        app._dashboard_context_tasks[key] = sentinel

        release.set()
        with pytest.raises(RuntimeError):
            await old_task

        assert app._dashboard_context_tasks.get(key) is sentinel, (
            "the failing task evicted a newer task's entry, so later polls start "
            "duplicate scans and the replacement cannot publish its result"
        )
        sentinel.cancel()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 5 (P2) — a cold start that never observed anything must read as degraded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cold_quota_denied_start_reports_a_degraded_reason(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """The indicator exists to expose exactly this outage."""
    clock = ManualClock()
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [_raw_pr(7)])

    orch = _orchestrator(clock, budget=0)
    await orch.refresh_prs()

    freshness = orch.open_set_freshness()
    assert freshness.observed_at is None
    assert freshness.degraded_reason, (
        "a cold start with rich discovery denied recorded no reason, so the "
        "header shows a non-stale 'PR data loading' indefinitely"
    )


def test_unknown_observation_with_a_failure_renders_stale(monkeypatch) -> None:
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

    context = app._observation_context()

    assert context["known"] is False
    assert context["stale"] is True
    assert "quota" in context["detail"]


# ---------------------------------------------------------------------------
# 6 (P1) — the first REST timestamp for an already-tracked PR is a change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_rest_timestamp_for_a_known_pr_invalidates_review(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """PR_SNAPSHOT_FIELDS omits updatedAt, so the baseline starts empty.

    If the first REST 200 is itself caused by a comment, swallowing it as
    "no previous value" loses the only signal that PR ever changed.
    """
    clock = ManualClock()
    review_scans: list[int] = []
    rich_pr = _raw_pr(7)
    rich_pr.pop("updatedAt")  # production shape: the rich list has no updatedAt

    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [dict(rich_pr)])
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(
                200,
                [_probe_pr(7, updated_at="2026-08-22T04:45:00Z")],
                etag='"v2"',
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

    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()
    clock.advance(timedelta(seconds=orchestrator_module.POLL_INTERVAL_SECONDS))
    await orch.refresh_prs()

    assert len(review_scans) > scans_after_seed, (
        "the first REST updatedAt for an already-tracked PR was swallowed, so a "
        "comment that arrived before the first probe stays invisible"
    )


# ---------------------------------------------------------------------------
# 7 (P2) — an inferred review change must revoke blocker authority
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inferred_review_change_revokes_blocker_authority(
    monkeypatch: pytest.MonkeyPatch, boundaries
) -> None:
    """Match the webhook path: an invalidated slice is no longer authoritative.

    Otherwise a quota-deferred review read leaves a resolved thread reporting an
    actionable HAS_COMMENTS, or a new comment leaving the card CLEAN.
    """
    clock = ManualClock()
    updated_at = {"value": "2026-08-22T00:00:00Z"}
    monkeypatch.setattr(
        github_api,
        "list_open_prs",
        lambda cwd=None: [_raw_pr(7, updated_at=updated_at["value"])],
    )
    monkeypatch.setattr(
        github_api,
        "probe_open_prs_rest",
        lambda owner, repo, *, etag=None, last_modified=None, author=None, cwd=None: (
            github_api.ConditionalPRListProbe(
                200, [_probe_pr(7, updated_at=updated_at["value"])], etag='"v2"'
            )
        ),
    )
    monkeypatch.setattr(
        github_api,
        "scan_review_threads_observation",
        lambda number, latest, cwd=None: github_api.ObservationReadResult.observed(
            ([], [])
        ),
    )

    orch = _orchestrator(clock, budget=500)
    await orch.refresh_prs(force=True)

    key = next(iter(orch._observation_keys.values()))
    orch._observed_blocker_slices[key] = {ObservationSlice.REVIEW}
    orch._observed_blocker_keys.add(key)

    updated_at["value"] = "2026-08-22T04:45:00Z"
    clock.advance(timedelta(seconds=90))
    await orch.refresh_prs()

    assert ObservationSlice.REVIEW not in orch._observed_blocker_slices.get(key, set()), (
        "the synthetic review event did not revoke blocker authority, unlike the "
        "real webhook path"
    )
