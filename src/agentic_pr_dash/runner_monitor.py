"""GitHub Actions runner load aggregation for the dashboard.

The dashboard can show whether self-hosted runner capacity is the bottleneck for
pending CI. This module gathers workflow-job and runner-pool state through the
GitHub API helpers and returns compact summaries for cards and queue warnings.
It is optional: when no runner label is configured, the runner panel disappears.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import os
import subprocess
import threading
import time
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

_RUNNER_CACHE_TTL_SECONDS = 60.0
_runner_cache_lock = threading.Lock()
_runner_cache: dict[tuple[str, int, int], tuple[float, RunnerFleetLoad, RunCommand]] = {}

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


@dataclass(frozen=True)
class LocalRunnerHost:
    """One Docker daemon that hosts part of the self-hosted fleet.

    `docker_host` is a Docker endpoint URL (`ssh://user@host`, `tcp://...`), or
    None to use the ambient daemon the dashboard process already points at.
    """

    prefix: str
    docker_host: str | None = None
    name: str | None = None
    configuration_error: str | None = None

    @property
    def display(self) -> str:
        return self.name or self.docker_host or "local"


def _configured_local_container_prefix(cwd: str | None) -> str:
    raw = os.environ.get("AGENTIC_PR_DASH_LOCAL_RUNNER_CONTAINER_PREFIX")
    if raw is None:
        raw = load_config(cwd).extra.get("local_runner_container_prefix")
    return str(raw or "").strip()


def _configured_local_runner_hosts(cwd: str | None) -> list[LocalRunnerHost]:
    """Resolve the fleet's Docker daemons; the multi-host key wins when present.

    A fleet routinely spans more than one box — a large CI fleet plus a small
    reserve elsewhere, say. The single-prefix form probes only the AMBIENT
    daemon, so on a multi-box fleet it can describe only whichever box
    DOCKER_HOST happens to name and reports the rest as simply absent.
    `local_runner_hosts` names each daemon explicitly.
    """
    environment_prefix = os.environ.get(
        "AGENTIC_PR_DASH_LOCAL_RUNNER_CONTAINER_PREFIX"
    )
    if environment_prefix is not None:
        prefix = environment_prefix.strip()
        return [LocalRunnerHost(prefix=prefix)] if prefix else []

    raw_hosts = load_config(cwd).extra.get("local_runner_hosts")
    hosts: list[LocalRunnerHost] = []
    if raw_hosts is not None and not isinstance(raw_hosts, list):
        return [
            LocalRunnerHost(
                prefix="",
                name="local_runner_hosts",
                configuration_error="must be a list of tables",
            )
        ]
    if isinstance(raw_hosts, list):
        for index, entry in enumerate(raw_hosts, start=1):
            if not isinstance(entry, dict):
                hosts.append(
                    LocalRunnerHost(
                        prefix="",
                        name=f"local_runner_hosts[{index}]",
                        configuration_error="entry must be a table",
                    )
                )
                continue
            raw_prefix = entry.get("prefix")
            raw_docker_host = entry.get("docker_host")
            docker_host = (
                raw_docker_host.strip() or None
                if isinstance(raw_docker_host, str)
                else None
            )
            name = str(entry.get("name") or "").strip() or None
            if "docker_host" in entry and not isinstance(raw_docker_host, str):
                hosts.append(
                    LocalRunnerHost(
                        prefix="",
                        name=name or f"local_runner_hosts[{index}]",
                        configuration_error="docker_host must be a string",
                    )
                )
                continue
            if not isinstance(raw_prefix, str) or not raw_prefix.strip():
                hosts.append(
                    LocalRunnerHost(
                        prefix="",
                        docker_host=docker_host,
                        name=name or f"local_runner_hosts[{index}]",
                        configuration_error="prefix must be a non-empty string",
                    )
                )
                continue
            prefix = raw_prefix.strip()
            hosts.append(
                LocalRunnerHost(prefix=prefix, docker_host=docker_host, name=name)
            )
    if hosts:
        return hosts
    prefix = _configured_local_container_prefix(cwd)
    return [LocalRunnerHost(prefix=prefix)] if prefix else []


def _docker(host: LocalRunnerHost, *args: str) -> list[str]:
    """Build a docker argv pinned to one daemon.

    `--host` rather than a DOCKER_HOST environment variable: RunCommand carries
    no environment, and the flag keeps each call's target visible in the argv.
    """
    if host.docker_host:
        return ["docker", "--host", host.docker_host, *args]
    return ["docker", *args]


def _host_runner_rows(
    host: LocalRunnerHost,
    label: str,
    cwd: str | None,
    run: RunCommand,
    *,
    qualify_names: bool,
) -> list[dict[str, Any]] | None:
    """Runner rows for one daemon, or None when that daemon cannot be read."""
    try:
        listed = run(
            _docker(
                host,
                "ps",
                "-a",
                "--filter",
                f"name={host.prefix}",
                "--format",
                "{{json .}}",
            ),
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
            if isinstance(item, dict) and str(item.get("Names", "")).startswith(
                host.prefix
            ):
                containers.append(item)
    except json.JSONDecodeError:
        return None

    rows: list[dict[str, Any]] = []
    for index, container in enumerate(containers, start=1):
        name = str(container.get("Names") or "")
        state = str(container.get("State") or "").casefold()
        online = state == "running"
        busy = False
        if online:
            try:
                # `-eo pid,args`, never `-eo args`: docker >= 29.2 rejects a ps
                # selection with no PID column ("Couldn't find PID field in ps
                # output") and exits non-zero, which would drop this whole host.
                processes = run(_docker(host, "top", name, "-eo", "pid,args"), cwd, 5)
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
        rows.append(
            {
                "id": runner_id,
                # Container names repeat across boxes (both fleets name theirs
                # gha-runner-N), so an unqualified merge renders indistinguishable
                # duplicates.
                "name": f"{host.display}/{name}" if qualify_names else name,
                "status": "online" if online else "offline",
                "busy": busy,
                "labels": [{"name": label}],
            }
        )
    return rows


def _local_docker_runner_load(
    hosts: list[LocalRunnerHost] | str,
    label: str,
    cwd: str | None,
    run: RunCommand,
) -> RunnerFleetLoad | None:
    """Read a co-located Docker runner fleet without GitHub credentials.

    Accepts a bare prefix string for backward compatibility.
    """
    if isinstance(hosts, str):
        hosts = [LocalRunnerHost(prefix=hosts)]
    if not hosts:
        return None

    qualify = len(hosts) > 1
    rows: list[dict[str, Any]] = []
    unreachable: list[str] = []
    configuration_errors: list[str] = []
    for host in hosts:
        if host.configuration_error:
            configuration_errors.append(
                f"{host.display} ({host.configuration_error})"
            )
            continue
        host_rows = _host_runner_rows(host, label, cwd, run, qualify_names=qualify)
        if host_rows is None:
            unreachable.append(host.display)
            continue
        rows.extend(host_rows)

    if len(unreachable) == len(hosts):
        # Declaring local hosts makes them the authoritative inventory source.
        # Falling back to GitHub here couples an owned-infrastructure health
        # probe to API availability and rate limits, and can replace a precise
        # connectivity failure with stale runner registrations.  Preserve the
        # local failure instead; callers must not query GitHub for this fleet.
        detail = ", ".join(sorted(unreachable))
        return _degraded_load(
            f"Runner probe could not reach: {detail}",
            recommendation=(
                "Configured desktop CI host(s) are unreachable; "
                "runner health is unknown."
            ),
        )

    if len(configuration_errors) == len(hosts):
        return _degraded_load(
            "Runner configuration invalid: " + ", ".join(configuration_errors)
        )

    # A successful listing that matches nothing is an authoritative zero, not an
    # unavailable probe. Configuring hosts declares the fleet local; returning
    # None here would fall through to the GitHub runner endpoint, which needs
    # Administration: Read and may report unrelated registered runners in place
    # of the real local total.
    load = parse_runner_inventory({"runners": rows}, label=label)
    unavailable = [*unreachable, *configuration_errors]
    if unavailable:
        # Partial coverage must not read as a complete picture: the counts are
        # real but describe only the daemons that answered.
        detail = ", ".join(sorted(unavailable))
        return replace(
            load,
            error=f"Runner probe could not reach: {detail}",
            recommendation=(
                f"{load.recommendation} Counts exclude unreachable host(s): {detail}."
            ),
        )
    return load


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
    hosts = (
        _configured_local_runner_hosts(cwd)
        if local_container_prefix is None
        else [LocalRunnerHost(prefix=local_container_prefix.strip())]
        if local_container_prefix.strip()
        else []
    )
    if hosts:
        local_load = _local_docker_runner_load(hosts, label, cwd, run)
        if local_load is not None:
            return local_load
    repo_name = repo or _get_repo_full_name(cwd=cwd, run=run)
    if not repo_name:
        return _degraded_load("Runner probe failed: could not determine active GitHub repository.")

    payload, failure = _fetch_runner_scope(f"repos/{repo_name}", cwd, run)
    if failure is not None:
        return failure
    load = parse_runner_inventory(payload or {}, label=label)
    if load.online:
        return load

    # Nothing ONLINE at repo scope: try the ORG scope before declaring the fleet
    # down. The test is `online`, not `total`, and that distinction is the whole
    # bug: a repo that used to host the fleet keeps its stale `offline`
    # registrations, so `total` stays non-zero long after the runners moved.
    # Measured on gaia-free right after the org flip -- total=27, online=0, while
    # nine runners were live at org scope. Gating on `total` would have returned
    # that empty answer and kept reporting the fleet offline.
    #
    # `repos/<owner>/<repo>/actions/runners` lists ONLY runners registered to
    # that repo. It does NOT include org runners the repo can reach through a
    # runner group -- so once a fleet registers at org level this endpoint
    # returns zero and the dashboard reports "offline" while every runner is up
    # and taking jobs. Observed exactly that: repo scope 0, org scope 6, nine
    # healthy containers.
    #
    # Best-effort: a token without org `Self-hosted runners: read` gets a 403
    # here, which means "cannot see org runners", not "the fleet is down". In
    # that case keep the repo-scope answer rather than surfacing a probe error.
    org = repo_name.split("/", 1)[0]
    if not org:
        return load

    accessible, groups_failure = _accessible_group_ids(org, repo_name, cwd, run)
    if groups_failure is not None:
        # Same rule as the runner fetch below: only a PERMISSION error may be
        # swallowed. A timeout, malformed response, or transient 5xx while
        # enumerating groups leaves the fleet's state unknown, and returning the
        # repo-only load would present that as a healthy empty fleet.
        if groups_failure.error and "unauthorized" in groups_failure.error.casefold():
            return load
        return groups_failure
    if not accessible:
        return load

    # Fetch per GROUP rather than filtering the org-wide list: that listing
    # returns `runner_group_id: null`, so there is no way to tell from it which
    # group a runner belongs to. The per-group endpoint answers the question we
    # actually have -- "runners this repo may use".
    merged: list[Any] = []
    for group_id in sorted(accessible):
        payload_g, failure_g = _fetch_runner_scope_raw(
            f"orgs/{org}/actions/runner-groups/{group_id}/runners", cwd, run
        )
        if failure_g is not None:
            # A PERMISSION error means "cannot see org runners" -- benign on a
            # repo-mode deployment, so keep the repo answer. Anything else
            # (timeout, invalid JSON, transient 5xx) leaves the fleet's state
            # unknown and must surface: returning the repo-only load would
            # present that as a healthy empty fleet.
            if failure_g.error and "unauthorized" in failure_g.error.casefold():
                return load
            return failure_g
        if isinstance(payload_g, dict):
            merged.extend(payload_g.get("runners", []))

    org_load = parse_runner_inventory({"runners": merged}, label=label)
    # Prefer the org answer whenever it knows about ANY runners, online or not.
    # Keying on `online` would hide a genuine org-fleet outage: every runner
    # offline would fall back to the repo's empty or stale view and report zero
    # registered runners instead of N offline.
    return org_load if org_load.total else load


def get_cached_runner_fleet_load(
    *,
    cwd: str | None = None,
    run: RunCommand = _run,
    ttl_seconds: float = _RUNNER_CACHE_TTL_SECONDS,
) -> RunnerFleetLoad:
    """Share one owned-host probe across all dashboard HTTP polls."""
    now = time.monotonic()
    owner = getattr(run, "__self__", None)
    function = getattr(run, "__func__", None)
    key = (
        (cwd or "", id(owner), id(function))
        if owner is not None and function is not None
        else (cwd or "", id(run), 0)
    )
    with _runner_cache_lock:
        expired_keys = [
            cached_key
            for cached_key, (expires_at, _load, _run) in _runner_cache.items()
            if now >= expires_at
        ]
        for expired_key in expired_keys:
            del _runner_cache[expired_key]

        cached = _runner_cache.get(key)
        if cached is not None:
            _expires_at, load, _cached_run = cached
            return load
        load = get_runner_fleet_load(cwd=cwd, run=run)
        _runner_cache[key] = (time.monotonic() + ttl_seconds, load, run)
        return load


_GROUP_CACHE_TTL_SECONDS = 60.0
_group_cache: dict[tuple[str, str], tuple[float, set[int]]] = {}


def _accessible_group_ids(
    org: str,
    repo_name: str,
    cwd: str | None,
    run: RunCommand,
) -> tuple[set[int], RunnerFleetLoad | None]:
    """Runner-group ids that grant `repo_name`, or (empty, failure).

    The org-wide runner endpoint returns every runner in the organisation
    regardless of which groups grant which repositories, so counting it directly
    reports capacity this repo cannot actually use.

    Cached briefly: the dashboard polls runner load every 15s, and this walk
    costs one request per group plus one per selected group. Group membership
    changes on human timescales, so a short TTL removes almost all of that
    traffic without going stale in a way anyone would notice.
    """
    cache_key = (org, repo_name.casefold())
    cached = _group_cache.get(cache_key)
    now = datetime.now(timezone.utc).timestamp()
    if cached is not None and now - cached[0] < _GROUP_CACHE_TTL_SECONDS:
        return set(cached[1]), None

    payload, failure = _fetch_runner_scope_raw(f"orgs/{org}/actions/runner-groups", cwd, run)
    if failure is not None:
        return set(), failure
    if not isinstance(payload, dict) or not isinstance(payload.get("runner_groups"), list):
        return set(), _degraded_load("Runner group probe returned an unexpected response shape.")

    is_private = _repo_is_private(repo_name, cwd, run)
    accessible: set[int] = set()
    for group in payload["runner_groups"]:
        if not isinstance(group, dict):
            continue
        gid = group.get("id")
        if not isinstance(gid, int):
            continue

        # A PUBLIC repo additionally needs allows_public_repositories, which is
        # independent of visibility and defaults to false. A `visibility: all`
        # group still refuses public repos when it is unset, so counting those
        # runners would report capacity this repo cannot claim.
        if is_private is False and group.get("allows_public_repositories") is not True:
            continue

        visibility = group.get("visibility")
        if visibility == "all":
            accessible.add(gid)
            continue
        if visibility == "private":
            # Grants every PRIVATE repo in the org, and those are NOT listed by
            # the selected-repositories endpoint.
            if is_private is not False:
                accessible.add(gid)
            continue

        repos, repos_failure = _fetch_runner_scope_raw(
            f"orgs/{org}/actions/runner-groups/{gid}/repositories", cwd, run
        )
        if repos_failure is not None:
            # Silently skipping would drop a group this repo may well be in and
            # report a partial fleet as healthy. Surface it instead.
            return set(), repos_failure
        if not isinstance(repos, dict):
            return set(), _degraded_load(
                "Runner group repository probe returned an unexpected response shape."
            )
        # Repository names are case-insensitive on GitHub, and `repo_name` keeps
        # whatever spelling the remote or caller used while `full_name` is
        # canonical -- so `boundless-studios/gaia-free` must match
        # `Boundless-Studios/gaia-free`.
        names = {
            str(item.get("full_name", "")).casefold()
            for item in repos.get("repositories", [])
            if isinstance(item, dict)
        }
        if repo_name.casefold() in names:
            accessible.add(gid)

    _group_cache[cache_key] = (now, set(accessible))
    return accessible, None


def _repo_is_private(repo_name: str, cwd: str | None, run: RunCommand) -> bool | None:
    """True/False when known, None when the visibility cannot be determined.

    None is deliberately distinct from False: an unknown visibility must not be
    treated as "public" and silently drop every group that disallows public
    repositories.
    """
    payload, failure = _fetch_runner_scope_raw(f"repos/{repo_name}", cwd, run)
    if failure is not None or not isinstance(payload, dict):
        return None
    private = payload.get("private")
    return private if isinstance(private, bool) else None




def _fetch_runner_scope_raw(
    path: str,
    cwd: str | None,
    run: RunCommand,
) -> tuple[Any, RunnerFleetLoad | None]:
    """Paginated `gh api` returning the merged payload, or (None, failure)."""
    cmd = ["gh", "api", path, "--paginate", "--slurp"]
    try:
        result = run(cmd, cwd, 20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, _degraded_load(f"Runner probe failed: {exc}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        return None, _runner_probe_failure(detail)
    try:
        parsed = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return None, _degraded_load(f"Runner probe returned invalid JSON: {exc}")
    try:
        return _merge_pages(parsed), None
    except TypeError:
        return None, _degraded_load("Runner probe returned an unexpected response shape.")


def _merge_pages(payload: Any) -> dict[str, Any]:
    """Merge --slurp pages, concatenating whichever list key they carry."""
    if isinstance(payload, dict):
        return payload
    if not isinstance(payload, list):
        raise TypeError("payload must be a dict or list of dict pages")
    merged: dict[str, Any] = {}
    for page in payload:
        if not isinstance(page, dict):
            raise TypeError("page must be a dict")
        for key, value in page.items():
            if isinstance(value, list):
                merged.setdefault(key, []).extend(value)
            else:
                merged.setdefault(key, value)
    return merged


def _fetch_runner_scope(
    scope: str,
    cwd: str | None,
    run: RunCommand,
) -> tuple[dict[str, Any] | None, RunnerFleetLoad | None]:
    """Fetch one runner scope. Returns (payload, failure) with exactly one set.

    `scope` is an API path prefix: `repos/<owner>/<repo>` or `orgs/<owner>`.

    --paginate is required, not an optimisation: the fleet's runners are
    ephemeral and JIT-register a fresh name per job, so stale `offline` rows
    accumulate and GitHub returns them oldest-first, pushing the live runners
    past the first page (BOU-2834).
    """
    cmd = ["gh", "api", f"{scope}/actions/runners", "--paginate", "--slurp"]
    try:
        result = run(cmd, cwd, 20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, _degraded_load(f"Runner probe failed: {exc}")

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        return None, _runner_probe_failure(detail)

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        return None, _degraded_load(f"Runner probe returned invalid JSON: {exc}")

    try:
        return _merge_runner_pages(payload), None
    except TypeError:
        return None, _degraded_load("Runner probe returned an unexpected response shape.")


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
