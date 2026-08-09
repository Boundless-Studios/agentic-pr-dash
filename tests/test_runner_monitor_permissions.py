from __future__ import annotations

import subprocess

from agentic_pr_dash.runner_monitor import get_runner_fleet_load


def test_github_app_runner_permission_failure_is_actionable() -> None:
    def forbidden(
        cmd: list[str], cwd: str | None, timeout_s: int
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr="gh: Resource not accessible by integration (HTTP 403)",
        )

    load = get_runner_fleet_load(
        repo="Boundless-Studios/gaia-free",
        label="desktop-ci",
        local_container_prefix="",
        run=forbidden,
    )

    assert load.is_degraded is True
    assert load.total == 0
    assert "health is unknown" in load.recommendation
    assert load.error is not None
    assert "Administration: Read" in load.error
    assert "may still be online" in load.error


def test_local_docker_runner_probe_does_not_require_github() -> None:
    calls: list[list[str]] = []

    def local_runner_state(
        cmd: list[str], cwd: str | None, timeout_s: int
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:3] == ["docker", "ps", "-a"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=(
                    '{"ID":"a1","Names":"gha-runner-1","State":"running"}\n'
                    '{"ID":"a2","Names":"gha-runner-2","State":"running"}\n'
                    '{"ID":"a3","Names":"gha-runner-3","State":"exited"}\n'
                ),
                stderr="",
            )
        if cmd[:3] == ["docker", "top", "gha-runner-1"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="COMMAND\nRunner.Listener run\n", stderr=""
            )
        if cmd[:3] == ["docker", "top", "gha-runner-2"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="COMMAND\nRunner.Listener run\nRunner.Worker spawnclient\n",
                stderr="",
            )
        raise AssertionError(f"unexpected command: {cmd}")

    load = get_runner_fleet_load(
        repo="Boundless-Studios/gaia-free",
        label="desktop-ci",
        local_container_prefix="gha-runner-",
        run=local_runner_state,
    )

    assert load.is_degraded is False
    assert (load.total, load.online, load.busy, load.idle, load.offline) == (
        3,
        2,
        1,
        1,
        1,
    )
    assert [runner.name for runner in load.busy_runners] == ["gha-runner-2"]
    assert [runner.name for runner in load.idle_runners] == ["gha-runner-1"]
    assert [runner.name for runner in load.offline_runners] == ["gha-runner-3"]
    assert all(cmd[0] == "docker" for cmd in calls)


def test_scaled_down_local_fleet_reports_zero_without_github() -> None:
    """Docker answering "no such containers" is an authoritative zero.

    Configuring a container prefix declares that the fleet is local, so a
    successful probe that matches nothing means the fleet is scaled to zero —
    not that the probe was unavailable. Falling through to the GitHub runner
    endpoint here would demand Administration: Read and could report unrelated
    registered runners in place of the authoritative local total.
    """
    calls: list[list[str]] = []

    def empty_fleet(
        cmd: list[str], cwd: str | None, timeout_s: int
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:3] == ["docker", "ps", "-a"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    load = get_runner_fleet_load(
        repo="Boundless-Studios/gaia-free",
        label="desktop-ci",
        local_container_prefix="gha-runner-",
        run=empty_fleet,
    )

    assert load.is_degraded is False
    assert (load.total, load.online, load.busy, load.idle, load.offline) == (
        0,
        0,
        0,
        0,
        0,
    )
    assert [cmd[:3] for cmd in calls] == [["docker", "ps", "-a"]]


def test_unreadable_local_probe_still_falls_back_to_github() -> None:
    """A docker failure is genuinely unavailable, so the fallback must remain.

    This pins the boundary against the scaled-down case above: only a
    *successful* empty listing is an authoritative zero.
    """

    def docker_unavailable(
        cmd: list[str], cwd: str | None, timeout_s: int
    ) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["docker", "ps", "-a"]:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="docker: command not found"
            )
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr="gh: Resource not accessible by integration (HTTP 403)",
        )

    load = get_runner_fleet_load(
        repo="Boundless-Studios/gaia-free",
        label="desktop-ci",
        local_container_prefix="gha-runner-",
        run=docker_unavailable,
    )

    assert load.is_degraded is True
    assert "Administration: Read" in (load.error or "")
