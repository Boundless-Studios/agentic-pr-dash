"""Cover for the co-located Docker runner probe.

Two defects motivated this file, both of which made a healthy fleet render as
"Self-hosted CI is offline":

1. Docker >= 29.2 validates the `ps` arguments given to `docker top` and rejects
   any selection without a PID column (`Couldn't find PID field in ps output`,
   exit 1). The probe asked for `-eo args`, so on an up-to-date host every
   busy-check failed and the whole probe returned None.
2. The probe read only the AMBIENT Docker daemon. A fleet split across two boxes
   could therefore only ever be half-seen, and whichever half `DOCKER_HOST` did
   not name was reported as absent rather than unknown.

Falling back to the GitHub runners endpoint does not rescue either case: for
JIT-ephemeral runners that deregister after each job, that endpoint lists only
stale offline rows.
"""

from __future__ import annotations

import subprocess

from agentic_pr_dash import runner_monitor
from agentic_pr_dash.runner_monitor import LocalRunnerHost

LABEL = "gaia-ci-desktop"
LISTENING = "PID COMMAND\n1 /home/runner/actions-runner/run.sh\n"
WORKING = "PID COMMAND\n1 /home/runner/actions-runner/bin/Runner.Worker spawnclient\n"


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _container(name: str, state: str = "running", cid: str = "0123456789ab") -> str:
    return '{"Names": "%s", "State": "%s", "ID": "%s"}' % (name, state, cid)


def _host_of(cmd: list[str]) -> str | None:
    return cmd[cmd.index("--host") + 1] if "--host" in cmd else None


def _verb(cmd: list[str]) -> str:
    return cmd[cmd.index("--host") + 2] if "--host" in cmd else cmd[1]


def test_busy_probe_requests_a_pid_column() -> None:
    """`docker top` must be given a ps selection that includes PID."""
    seen: list[list[str]] = []

    def run(cmd: list[str], cwd: str | None, timeout_s: int) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return _completed(_container("gha-runner-1") if _verb(cmd) == "ps" else LISTENING)

    runner_monitor._local_docker_runner_load("gha-runner-", LABEL, None, run)

    top = [cmd for cmd in seen if _verb(cmd) == "top"]
    assert top, "expected a docker top call for a running container"
    selection = top[0][top[0].index("-eo") + 1]
    assert "pid" in selection.split(","), (
        f"docker >= 29.2 rejects a ps selection without PID; got -eo {selection}"
    )


def test_running_container_without_worker_is_idle_not_offline() -> None:
    def run(cmd: list[str], cwd: str | None, timeout_s: int) -> subprocess.CompletedProcess[str]:
        if _verb(cmd) == "ps":
            return _completed("\n".join(_container(f"gha-runner-{n}") for n in (1, 2)))
        return _completed(LISTENING)

    load = runner_monitor._local_docker_runner_load("gha-runner-", LABEL, None, run)

    assert load is not None
    assert (load.online, load.idle, load.busy) == (2, 2, 0)


def test_running_container_with_worker_is_busy() -> None:
    def run(cmd: list[str], cwd: str | None, timeout_s: int) -> subprocess.CompletedProcess[str]:
        if _verb(cmd) == "ps":
            return _completed(_container("gha-runner-1"))
        return _completed(WORKING)

    load = runner_monitor._local_docker_runner_load("gha-runner-", LABEL, None, run)

    assert load is not None
    assert (load.busy, load.idle) == (1, 0)


# --- multi-host ---------------------------------------------------------


CI = LocalRunnerHost(prefix="gha-runner-", docker_host="ssh://ci", name="ci-box")
RESERVE = LocalRunnerHost(prefix="gha-runner-", docker_host="ssh://wsl", name="reserve")


def _two_box_run(ci_count: int = 9, reserve_count: int = 2, reserve_state: str = "running"):
    def run(cmd: list[str], cwd: str | None, timeout_s: int) -> subprocess.CompletedProcess[str]:
        host = _host_of(cmd)
        if _verb(cmd) == "ps":
            count, state = (
                (ci_count, "running") if host == "ssh://ci" else (reserve_count, reserve_state)
            )
            return _completed(
                "\n".join(
                    _container(f"gha-runner-{n}", state, f"{host == 'ssh://ci':x}00000000{n:03x}")
                    for n in range(1, count + 1)
                )
            )
        return _completed(LISTENING)

    return run


def test_every_configured_host_is_probed_and_summed() -> None:
    """A fleet split across two boxes reports the combined total."""
    load = runner_monitor._local_docker_runner_load(
        [CI, RESERVE], LABEL, None, _two_box_run()
    )

    assert load is not None
    assert load.total == 11
    assert load.online == 11
    assert load.error is None


def test_each_host_is_addressed_explicitly() -> None:
    """Each probe pins its daemon, so neither box depends on ambient DOCKER_HOST."""
    seen: list[list[str]] = []
    base = _two_box_run()

    def run(cmd: list[str], cwd: str | None, timeout_s: int) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return base(cmd, cwd, timeout_s)

    runner_monitor._local_docker_runner_load([CI, RESERVE], LABEL, None, run)

    probed = {_host_of(cmd) for cmd in seen if _verb(cmd) == "ps"}
    assert probed == {"ssh://ci", "ssh://wsl"}


def test_names_are_qualified_across_hosts() -> None:
    """Both boxes name containers gha-runner-N; the merge must disambiguate."""
    load = runner_monitor._local_docker_runner_load(
        [CI, RESERVE], LABEL, None, _two_box_run(ci_count=1, reserve_count=1)
    )

    assert load is not None
    assert sorted(r.name for r in load.idle_runners) == [
        "ci-box/gha-runner-1",
        "reserve/gha-runner-1",
    ]


def test_single_host_names_stay_unqualified() -> None:
    """One configured host keeps the historical bare container name."""
    load = runner_monitor._local_docker_runner_load(
        [CI], LABEL, None, _two_box_run(ci_count=1)
    )

    assert load is not None
    assert [r.name for r in load.idle_runners] == ["gha-runner-1"]


def test_one_unreachable_host_degrades_but_still_reports() -> None:
    """A dead box must not blank the fleet, nor pass its counts off as complete."""

    def run(cmd: list[str], cwd: str | None, timeout_s: int) -> subprocess.CompletedProcess[str]:
        if _host_of(cmd) == "ssh://wsl":
            return _completed("cannot connect", returncode=1)
        if _verb(cmd) == "ps":
            return _completed("\n".join(_container(f"gha-runner-{n}") for n in range(1, 10)))
        return _completed(LISTENING)

    load = runner_monitor._local_docker_runner_load([CI, RESERVE], LABEL, None, run)

    assert load is not None
    assert load.online == 9
    assert load.is_degraded
    assert "reserve" in (load.error or "")
    assert "reserve" in load.recommendation


def test_all_hosts_unreachable_falls_back_to_github() -> None:
    """Total loss of docker is unknown, not an authoritative zero."""

    def run(cmd: list[str], cwd: str | None, timeout_s: int) -> subprocess.CompletedProcess[str]:
        return _completed("cannot connect", returncode=1)

    assert runner_monitor._local_docker_runner_load([CI, RESERVE], LABEL, None, run) is None


def test_configured_hosts_prefers_multi_host_key(monkeypatch) -> None:
    """local_runner_hosts wins over the legacy single-prefix key."""

    class _Cfg:
        extra = {
            "local_runner_container_prefix": "gha-runner-",
            "local_runner_hosts": [
                {"prefix": "gha-runner-", "docker_host": "ssh://ci", "name": "ci-box"},
                {"prefix": "gha-runner-", "docker_host": "ssh://wsl", "name": "reserve"},
            ],
        }

    monkeypatch.setattr(runner_monitor, "load_config", lambda *_a, **_k: _Cfg())
    hosts = runner_monitor._configured_local_runner_hosts(None)

    assert [h.docker_host for h in hosts] == ["ssh://ci", "ssh://wsl"]


def test_configured_hosts_prefers_environment_prefix_over_multi_host_key(monkeypatch) -> None:
    """The environment override selects the ambient daemon before TOML hosts."""

    class _Cfg:
        extra = {
            "local_runner_hosts": [
                {"prefix": "gha-runner-", "docker_host": "ssh://ci", "name": "ci-box"},
            ],
        }

    monkeypatch.setenv("AGENTIC_PR_DASH_LOCAL_RUNNER_CONTAINER_PREFIX", "temporary-")
    monkeypatch.setattr(runner_monitor, "load_config", lambda *_a, **_k: _Cfg())

    assert runner_monitor._configured_local_runner_hosts(None) == [
        LocalRunnerHost(prefix="temporary-")
    ]


def test_malformed_configured_host_degrades_valid_fleet(monkeypatch) -> None:
    """A malformed entry remains visible instead of making partial counts complete."""

    class _Cfg:
        extra = {
            "local_runner_hosts": [
                {"prefix": "gha-runner-", "docker_host": "ssh://ci", "name": "ci-box"},
                {"docker_host": "ssh://wsl", "name": "reserve"},
            ],
        }

    monkeypatch.delenv("AGENTIC_PR_DASH_LOCAL_RUNNER_CONTAINER_PREFIX", raising=False)
    monkeypatch.setattr(runner_monitor, "load_config", lambda *_a, **_k: _Cfg())
    hosts = runner_monitor._configured_local_runner_hosts(None)

    def run(cmd: list[str], cwd: str | None, timeout_s: int) -> subprocess.CompletedProcess[str]:
        if _verb(cmd) == "ps":
            return _completed(_container("gha-runner-1"))
        return _completed(LISTENING)

    load = runner_monitor._local_docker_runner_load(hosts, LABEL, None, run)

    assert load is not None
    assert load.online == 1
    assert load.is_degraded
    assert "reserve" in (load.error or "")
    assert "prefix must be a non-empty string" in (load.error or "")


def test_all_malformed_configured_hosts_return_configuration_failure(monkeypatch) -> None:
    """Invalid local host configuration must not silently fall back to GitHub."""

    class _Cfg:
        extra = {"local_runner_hosts": [{"name": "reserve"}]}

    monkeypatch.delenv("AGENTIC_PR_DASH_LOCAL_RUNNER_CONTAINER_PREFIX", raising=False)
    monkeypatch.setattr(runner_monitor, "load_config", lambda *_a, **_k: _Cfg())

    load = runner_monitor._local_docker_runner_load(
        runner_monitor._configured_local_runner_hosts(None), LABEL, None, lambda *_a: None
    )

    assert load is not None
    assert load.is_degraded
    assert "reserve" in (load.error or "")
    assert "prefix must be a non-empty string" in (load.error or "")


def test_non_list_configured_hosts_return_configuration_failure(monkeypatch) -> None:
    """A table-shaped host setting must not silently fall back to GitHub."""

    class _Cfg:
        extra = {"local_runner_hosts": {"prefix": "gha-runner-"}}

    monkeypatch.delenv("AGENTIC_PR_DASH_LOCAL_RUNNER_CONTAINER_PREFIX", raising=False)
    monkeypatch.setattr(runner_monitor, "load_config", lambda *_a, **_k: _Cfg())

    hosts = runner_monitor._configured_local_runner_hosts(None)
    assert hosts[0].configuration_error == "must be a list of tables"
    load = runner_monitor._local_docker_runner_load(hosts, LABEL, None, lambda *_a: None)

    assert load is not None
    assert load.is_degraded
    assert "local_runner_hosts" in (load.error or "")


def test_non_string_configured_host_prefix_returns_configuration_failure(monkeypatch) -> None:
    """A typed-but-invalid prefix must remain visible as configuration error."""

    class _Cfg:
        extra = {
            "local_runner_hosts": [
                {"prefix": ["gha-runner-"], "name": "list-prefix"},
                {"prefix": 42, "name": "integer-prefix"},
            ]
        }

    monkeypatch.delenv("AGENTIC_PR_DASH_LOCAL_RUNNER_CONTAINER_PREFIX", raising=False)
    monkeypatch.setattr(runner_monitor, "load_config", lambda *_a, **_k: _Cfg())

    hosts = runner_monitor._configured_local_runner_hosts(None)
    assert all(
        host.configuration_error == "prefix must be a non-empty string" for host in hosts
    )
    load = runner_monitor._local_docker_runner_load(hosts, LABEL, None, lambda *_a: None)

    assert load is not None
    assert load.is_degraded
    assert "list-prefix" in (load.error or "")
    assert "integer-prefix" in (load.error or "")


def test_non_string_configured_docker_host_returns_configuration_failure(monkeypatch) -> None:
    """A present non-string endpoint must not probe the ambient Docker daemon."""

    class _Cfg:
        extra = {
            "local_runner_hosts": [
                {"prefix": "gha-runner-", "docker_host": False, "name": "reserve"},
            ]
        }

    monkeypatch.delenv("AGENTIC_PR_DASH_LOCAL_RUNNER_CONTAINER_PREFIX", raising=False)
    monkeypatch.setattr(runner_monitor, "load_config", lambda *_a, **_k: _Cfg())
    hosts = runner_monitor._configured_local_runner_hosts(None)

    assert hosts[0].configuration_error == "docker_host must be a string"
    load = runner_monitor._local_docker_runner_load(hosts, LABEL, None, lambda *_a: None)

    assert load is not None
    assert load.is_degraded
    assert "reserve" in (load.error or "")
    assert "docker_host must be a string" in (load.error or "")


def test_configured_hosts_falls_back_to_legacy_prefix(monkeypatch) -> None:
    """Without the new key the ambient-daemon behaviour is unchanged."""

    class _Cfg:
        extra = {"local_runner_container_prefix": "gha-runner-"}

    monkeypatch.delenv("AGENTIC_PR_DASH_LOCAL_RUNNER_CONTAINER_PREFIX", raising=False)
    monkeypatch.setattr(runner_monitor, "load_config", lambda *_a, **_k: _Cfg())
    hosts = runner_monitor._configured_local_runner_hosts(None)

    assert len(hosts) == 1
    assert hosts[0].docker_host is None
    assert hosts[0].prefix == "gha-runner-"
