"""GitHub Actions runner load aggregation for the dashboard.

The dashboard can show whether self-hosted runner capacity is the bottleneck for
pending CI. This module gathers workflow-job and runner-pool state through the
GitHub API helpers and returns compact summaries for cards and queue warnings.
It is optional: when no runner label is configured, the runner panel disappears.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import subprocess
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from .config import load as load_config


def _runner_label() -> str | None:
    """Return the configured self-hosted runner label, or None if the runner panel is disabled."""
    return load_config().runner_label


@dataclass(frozen=True)
class RunnerInfo:
    id: int
    name: str
    status: str
    busy: bool
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunnerFleetLoad:
    total: int = 0
    online: int = 0
    busy: int = 0
    idle: int = 0
    offline: int = 0
    utilization_percent: int = 0
    recommendation: str = "Self-hosted CI runner load is unavailable."
    busy_runners: list[RunnerInfo] = field(default_factory=list)
    idle_runners: list[RunnerInfo] = field(default_factory=list)
    offline_runners: list[RunnerInfo] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: str | None = None

    @property
    def is_degraded(self) -> bool:
        return self.error is not None


RunCommand = Callable[[list[str], Optional[str], int], subprocess.CompletedProcess[str]]

_INTEGRATION_PERMISSION_ERRORS = (
    "resource not accessible by integration",
    "http 403",
)


def _runner_probe_failure(detail: str) -> RunnerFleetLoad:
    normalized = detail.casefold()
    if all(fragment in normalized for fragment in _INTEGRATION_PERMISSION_ERRORS):
        return _degraded_load(
            "Runner probe unauthorized: the GitHub App installation needs "
            "Repository Administration: Read to list self-hosted runners; "
            "the runners may still be online.",
            recommendation=(
                "Runner inventory probe is unauthorized; runner health is unknown."
            ),
        )
    return _degraded_load(f"Runner probe failed: {detail}")


def _configured_local_container_prefix(cwd: str | None) -> str:
    raw = os.environ.get("AGENTIC_PR_DASH_LOCAL_RUNNER_CONTAINER_PREFIX")
    if raw is None:
        raw = load_config(cwd).extra.get("local_runner_container_prefix")
    return str(raw or "").strip()


def _local_docker_runner_load(
    prefix: str,
    label: str,
    cwd: str | None,
    run: RunCommand,
) -> RunnerFleetLoad | None:
    """Read a co-located Docker runner fleet without GitHub credentials."""
    try:
        listed = run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"name={prefix}",
                "--format",
                "{{json .}}",
            ],
            cwd,
            10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listed.returncode != 0:
        return None

    containers: list[dict[str, Any]] = []
    try:
        for line in listed.stdout.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict) and str(item.get("Names", "")).startswith(prefix):
                containers.append(item)
    except json.JSONDecodeError:
        return None
    if not containers:
        return None

    runners: list[dict[str, Any]] = []
    for index, container in enumerate(containers, start=1):
        name = str(container.get("Names") or "")
        state = str(container.get("State") or "").casefold()
        online = state == "running"
        busy = False
        if online:
            try:
                processes = run(
                    ["docker", "top", name, "-eo", "args"], cwd, 5
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if processes.returncode != 0:
                return None
            busy = "Runner.Worker" in processes.stdout
        raw_id = str(container.get("ID") or "")
        try:
            runner_id = int(raw_id[:12], 16)
        except ValueError:
            runner_id = index
        runners.append(
            {
                "id": runner_id,
                "name": name,
                "status": "online" if online else "offline",
                "busy": busy,
                "labels": [{"name": label}],
            }
        )
    return parse_runner_inventory({"runners": runners}, label=label)


def _run(cmd: list[str], cwd: str | None, timeout_s: int) -> subprocess.CompletedProcess[str]:
    from agentic_pr_dash import github_api  # noqa: PLC0415
    return subprocess.run(
        cmd,
        cwd=cwd,
        timeout=timeout_s,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=github_api.automation_subprocess_env(cmd),
    )


def parse_runner_inventory(
    payload: dict[str, Any],
    *,
    label: str | None = None,
) -> RunnerFleetLoad:
    label = label or _runner_label()
    if label is None:
        return RunnerFleetLoad()
    runners = [
        _runner_from_payload(item)
        for item in payload.get("runners", [])
        if isinstance(item, dict) and label in _label_names(item)
    ]
    runners = [runner for runner in runners if runner is not None]

    total = len(runners)
    online_runners = [runner for runner in runners if runner.status == "online"]
    busy_runners = [runner for runner in online_runners if runner.busy]
    idle_runners = [runner for runner in online_runners if not runner.busy]
    offline_runners = [runner for runner in runners if runner.status != "online"]
    utilization = round((len(busy_runners) / len(online_runners)) * 100) if online_runners else 0

    return RunnerFleetLoad(
        total=total,
        online=len(online_runners),
        busy=len(busy_runners),
        idle=len(idle_runners),
        offline=len(offline_runners),
        utilization_percent=utilization,
        recommendation=_recommendation(
            total=total,
            online=len(online_runners),
            busy=len(busy_runners),
            idle=len(idle_runners),
            offline=len(offline_runners),
            label=label,
        ),
        busy_runners=sorted(busy_runners, key=lambda runner: runner.name),
        idle_runners=sorted(idle_runners, key=lambda runner: runner.name),
        offline_runners=sorted(offline_runners, key=lambda runner: runner.name),
    )


def get_runner_fleet_load(
    *,
    repo: str | None = None,
    label: str | None = None,
    cwd: str | None = None,
    local_container_prefix: str | None = None,
    run: RunCommand = _run,
) -> RunnerFleetLoad:
    label = label or _runner_label()
    if label is None:
        return RunnerFleetLoad()
    prefix = (
        _configured_local_container_prefix(cwd)
        if local_container_prefix is None
        else local_container_prefix.strip()
    )
    if prefix:
        local_load = _local_docker_runner_load(prefix, label, cwd, run)
        if local_load is not None:
            return local_load
    repo_name = repo or _get_repo_full_name(cwd=cwd, run=run)
    if not repo_name:
        return _degraded_load("Runner probe failed: could not determine active GitHub repository.")

    cmd = ["gh", "api", f"repos/{repo_name}/actions/runners", "--paginate", "--slurp"]
    try:
        result = run(cmd, cwd, 20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _degraded_load(f"Runner probe failed: {exc}")

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        return _runner_probe_failure(detail)

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return _degraded_load(f"Runner probe returned invalid JSON: {exc}")

    try:
        payload = _merge_runner_pages(payload)
    except TypeError:
        return _degraded_load("Runner probe returned an unexpected response shape.")

    return parse_runner_inventory(payload, label=label)


def _get_repo_full_name(*, cwd: str | None, run: RunCommand) -> str | None:
    try:
        result = run(["git", "remote", "get-url", "origin"], cwd, 10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return _repo_full_name_from_remote(result.stdout.strip())


def _repo_full_name_from_remote(remote_url: str) -> str | None:
    if remote_url.startswith("git@"):
        path = remote_url.split(":", 1)[-1]
    else:
        parsed = urlparse(remote_url)
        path = parsed.path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return None
    return f"{parts[-2]}/{parts[-1]}"


def _merge_runner_pages(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if not isinstance(payload, list):
        raise TypeError("runner payload must be a dict or list of dict pages")
    runners: list[Any] = []
    for page in payload:
        if not isinstance(page, dict):
            raise TypeError("runner page must be a dict")
        page_runners = page.get("runners", [])
        if not isinstance(page_runners, list):
            raise TypeError("runner page runners must be a list")
        runners.extend(page_runners)
    return {"runners": runners}


def _runner_from_payload(item: dict[str, Any]) -> RunnerInfo | None:
    name = item.get("name")
    if not isinstance(name, str) or not name:
        return None
    return RunnerInfo(
        id=int(item.get("id") or 0),
        name=name,
        status=str(item.get("status") or "unknown"),
        busy=bool(item.get("busy")),
        labels=tuple(_label_names(item)),
    )


def _label_names(item: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for raw_label in item.get("labels", []):
        if isinstance(raw_label, dict) and isinstance(raw_label.get("name"), str):
            names.append(raw_label["name"])
    return names


def _recommendation(
    *,
    total: int,
    online: int,
    busy: int,
    idle: int,
    offline: int,
    label: str,
) -> str:
    if total == 0:
        return f"No {label} runners were found. Check runner labels or registration."
    if online == 0:
        return "Self-hosted CI is offline; heavy jobs will fall back to ubuntu-latest."
    if idle > 0:
        suffix = "s" if idle != 1 else ""
        return f"Self-hosted CI has spare capacity for {idle} more concurrent job{suffix}."
    if busy >= online and offline > 0:
        return "Self-hosted CI is saturated, and offline runners could restore more parallel capacity."
    if busy >= online:
        return "Self-hosted CI is saturated. Add runners or raise concurrency if queues persist."
    return "Self-hosted CI load is available."


def _degraded_load(
    error: str,
    *,
    recommendation: str = "Self-hosted CI runner load is unavailable.",
) -> RunnerFleetLoad:
    return RunnerFleetLoad(
        recommendation=recommendation,
        error=error,
    )
