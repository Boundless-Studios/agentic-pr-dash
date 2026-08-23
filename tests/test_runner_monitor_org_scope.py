from __future__ import annotations

import json
import subprocess

from agentic_pr_dash.runner_monitor import get_runner_fleet_load

LABEL = "gaia-ci-desktop"
REPO = "Boundless-Studios/gaia-free"
GROUP_ID = 4


def _runners(*names: str, status: str = "online", group_id: int = GROUP_ID) -> str:
    return json.dumps(
        {
            "runners": [
                {
                    "id": index,
                    "name": name,
                    "status": status,
                    "busy": False,
                    "runner_group_id": group_id,
                    "labels": [{"name": LABEL}],
                }
                for index, name in enumerate(names, 1)
            ]
        }
    )


def _groups(visibility: str = "selected") -> str:
    return json.dumps(
        {"runner_groups": [{"id": GROUP_ID, "name": "gaia-ci", "visibility": visibility}]}
    )


def _group_repos(*full_names: str) -> str:
    return json.dumps({"repositories": [{"full_name": name} for name in full_names]})


def _ok(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _err(stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)


def _load(run):
    return get_runner_fleet_load(
        repo=REPO, label=LABEL, local_container_prefix="", run=run
    )


def test_org_registered_fleet_is_not_reported_offline() -> None:
    """A fleet registered at ORG level must still be found.

    `repos/<owner>/<repo>/actions/runners` lists only runners registered to that
    repo; it does NOT include org runners reached through a runner group. Before
    this fallback the dashboard showed the fleet offline while nine runners were
    up and taking jobs.
    """

    def run(cmd, cwd, timeout_s):
        path = cmd[2]
        if path.startswith("repos/"):
            return _ok(_runners())
        if path.endswith("/repositories"):
            return _ok(_group_repos(REPO))
        if path.endswith("/runner-groups"):
            return _ok(_groups())
        return _ok(_runners("gha-ubuntu-1", "gha-ubuntu-2"))

    load = _load(run)
    assert load.online == 2
    assert load.is_degraded is False


def test_stale_offline_repo_registrations_do_not_mask_the_org_fleet() -> None:
    """The fallback keys on ONLINE runners, not on total.

    A repo that used to host the fleet keeps its stale `offline` registrations,
    so `total` stays non-zero long after the runners moved. Measured on
    gaia-free right after the flip: total=27, online=0, nine live at org scope.
    """

    def run(cmd, cwd, timeout_s):
        path = cmd[2]
        if path.startswith("repos/"):
            return _ok(_runners(*[f"stale-{i}" for i in range(27)], status="offline"))
        if path.endswith("/repositories"):
            return _ok(_group_repos(REPO))
        if path.endswith("/runner-groups"):
            return _ok(_groups())
        return _ok(_runners("gha-ubuntu-1", "gha-ubuntu-2", "gha-ubuntu-3"))

    load = _load(run)
    assert load.online == 3
    assert load.total == 3


def test_runners_in_groups_this_repo_cannot_use_are_excluded() -> None:
    """Org-wide listing includes groups that do not grant this repository.

    Counting those would report capacity the repo cannot claim, and a job routed
    on that basis queues against a label nothing answers.
    """

    def run(cmd, cwd, timeout_s):
        path = cmd[2]
        if path.startswith("repos/"):
            return _ok(_runners())
        if path.endswith("/repositories"):
            return _ok(_group_repos("Boundless-Studios/some-other-repo"))
        if path.endswith("/runner-groups"):
            return _ok(_groups())
        return _ok(_runners("gha-other-1", "gha-other-2"))

    load = _load(run)
    assert load.online == 0
    assert load.total == 0


def test_offline_org_fleet_is_reported_as_offline_not_absent() -> None:
    """An org fleet that is entirely offline is an outage, not an empty fleet.

    Keying the preference on `online` would fall back to the repo's empty view
    and report zero registered runners instead of N offline.
    """

    def run(cmd, cwd, timeout_s):
        path = cmd[2]
        if path.startswith("repos/"):
            return _ok(_runners())
        if path.endswith("/repositories"):
            return _ok(_group_repos(REPO))
        if path.endswith("/runner-groups"):
            return _ok(_groups())
        return _ok(_runners("gha-ubuntu-1", "gha-ubuntu-2", status="offline"))

    load = _load(run)
    assert load.total == 2
    assert load.offline == 2


def test_transient_org_failure_surfaces_instead_of_looking_healthy() -> None:
    """Only a PERMISSION error may be swallowed.

    A timeout, invalid JSON, or transient 5xx on the org probe means the fleet's
    state is unknown. Returning the repo-only load would present that as a
    healthy empty fleet.
    """

    def run(cmd, cwd, timeout_s):
        path = cmd[2]
        if path.startswith("repos/"):
            return _ok(_runners())
        if path.endswith("/repositories"):
            return _ok(_group_repos(REPO))
        if path.endswith("/runner-groups"):
            return _ok(_groups())
        return _err("gh: Internal Server Error (HTTP 500)")

    load = _load(run)
    assert load.is_degraded is True


def test_repo_registered_fleet_does_not_query_the_org_scope() -> None:
    """The org path is a fallback, not an extra request on the happy path."""
    paths: list[str] = []

    def run(cmd, cwd, timeout_s):
        paths.append(cmd[2])
        return _ok(_runners("gha-runner-1"))

    load = _load(run)
    assert load.online == 1
    assert not any(path.startswith("orgs/") for path in paths)


def test_both_scopes_paginate() -> None:
    """--paginate is required everywhere, per BOU-2834.

    Ephemeral runners JIT-register a fresh name per job, so stale `offline` rows
    accumulate and GitHub returns them oldest-first -- the live runners sit past
    the first page, and an un-paginated call sees only dead entries.
    """
    commands: list[list[str]] = []

    def run(cmd, cwd, timeout_s):
        commands.append(cmd)
        path = cmd[2]
        if path.startswith("repos/"):
            return _ok(_runners())
        if path.endswith("/repositories"):
            return _ok(_group_repos(REPO))
        if path.endswith("/runner-groups"):
            return _ok(_groups())
        return _ok(_runners("gha-ubuntu-1"))

    _load(run)
    assert len(commands) >= 2
    for cmd in commands:
        assert "--paginate" in cmd
