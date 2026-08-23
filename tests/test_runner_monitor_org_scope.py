from __future__ import annotations

import json
import subprocess

from agentic_pr_dash.runner_monitor import get_runner_fleet_load

LABEL = "gaia-ci-desktop"


def _runners(*names: str) -> str:
    return json.dumps(
        {
            "runners": [
                {
                    "id": index,
                    "name": name,
                    "status": "online",
                    "busy": False,
                    "labels": [{"name": LABEL}],
                }
                for index, name in enumerate(names, 1)
            ]
        }
    )


def _ok(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def test_org_registered_fleet_is_not_reported_offline() -> None:
    """A fleet registered at ORG level must still be found.

    `repos/<owner>/<repo>/actions/runners` lists only runners registered to that
    repo; it does NOT include org runners the repo reaches through a runner
    group. Before this fallback the dashboard showed the fleet offline while
    nine runners were up and taking jobs.
    """
    scopes: list[str] = []

    def run(cmd: list[str], cwd: str | None, timeout_s: int):
        scopes.append(cmd[2])
        if cmd[2].startswith("repos/"):
            return _ok(_runners())
        return _ok(_runners("gha-ubuntu-1", "gha-ubuntu-2"))

    load = get_runner_fleet_load(
        repo="Boundless-Studios/gaia-free",
        label=LABEL,
        local_container_prefix="",
        run=run,
    )

    assert load.online == 2
    assert load.is_degraded is False
    assert any(scope.startswith("orgs/Boundless-Studios") for scope in scopes)


def test_stale_offline_repo_registrations_do_not_mask_the_org_fleet() -> None:
    """The fallback must key on ONLINE runners, not on total.

    A repo that used to host the fleet keeps its stale `offline` registrations,
    so `total` stays non-zero long after the runners moved to org scope.
    Measured on gaia-free right after the flip: total=27, online=0, while nine
    runners were live at org scope.
    """
    offline_rows = json.dumps(
        {
            "runners": [
                {
                    "id": index,
                    "name": f"gha-runner-{index}-1787400000-1",
                    "status": "offline",
                    "busy": False,
                    "labels": [{"name": LABEL}],
                }
                for index in range(1, 28)
            ]
        }
    )

    def run(cmd: list[str], cwd: str | None, timeout_s: int):
        if cmd[2].startswith("repos/"):
            return _ok(offline_rows)
        return _ok(_runners("gha-ubuntu-1", "gha-ubuntu-2", "gha-ubuntu-3"))

    load = get_runner_fleet_load(
        repo="Boundless-Studios/gaia-free",
        label=LABEL,
        local_container_prefix="",
        run=run,
    )

    assert load.online == 3
    assert load.total == 3


def test_repo_registered_fleet_does_not_query_the_org_scope() -> None:
    """The org call is a fallback, not an extra request on the happy path."""
    scopes: list[str] = []

    def run(cmd: list[str], cwd: str | None, timeout_s: int):
        scopes.append(cmd[2])
        return _ok(_runners("gha-runner-1"))

    load = get_runner_fleet_load(
        repo="Boundless-Studios/gaia-free",
        label=LABEL,
        local_container_prefix="",
        run=run,
    )

    assert load.online == 1
    assert not any(scope.startswith("orgs/") for scope in scopes)


def test_missing_org_permission_keeps_the_repo_answer() -> None:
    """A 403 on the org scope means "cannot see org runners", not "fleet down".

    The probe token only needs org `Self-hosted runners: read` when the fleet is
    org-registered. Without it the org call fails, and that must not turn an
    honest empty repo-scope answer into a probe error.
    """

    def run(cmd: list[str], cwd: str | None, timeout_s: int):
        if cmd[2].startswith("repos/"):
            return _ok(_runners())
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="gh: Resource not accessible (HTTP 403)"
        )

    load = get_runner_fleet_load(
        repo="Boundless-Studios/gaia-free",
        label=LABEL,
        local_container_prefix="",
        run=run,
    )

    assert load.total == 0
    assert load.is_degraded is False


def test_both_scopes_paginate() -> None:
    """--paginate is required on both, per BOU-2834.

    Ephemeral runners JIT-register a fresh name per job, so stale `offline` rows
    accumulate and GitHub returns them oldest-first -- the live runners sit past
    the first page, and an un-paginated call sees only dead entries.
    """
    commands: list[list[str]] = []

    def run(cmd: list[str], cwd: str | None, timeout_s: int):
        commands.append(cmd)
        if cmd[2].startswith("repos/"):
            return _ok(_runners())
        return _ok(_runners("gha-ubuntu-1"))

    get_runner_fleet_load(
        repo="Boundless-Studios/gaia-free",
        label=LABEL,
        local_container_prefix="",
        run=run,
    )

    assert len(commands) == 2
    for cmd in commands:
        assert "--paginate" in cmd
