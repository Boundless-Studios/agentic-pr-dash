"""Local agent process discovery for the dashboard.

Attribution rules:
  - Each claude/codex process is assigned to a worktree by its ACTUAL cwd
    (via `lsof -d cwd`), not by substring-matching worktree paths against
    the command line. Command-line matching caused false positives because
    e.g. `bash <your worktree launcher>`
    mentions the main repo path and would drag any claude it spawned into
    the main worktree's card regardless of where that claude's own cwd is.
  - If a process's own cwd doesn't fall inside any known worktree, we walk
    up the parent chain and use the nearest ancestor's cwd instead. Lets
    us still attribute a short-lived agent whose cwd is a subdirectory
    outside the worktree but whose parent shell lives inside one.

Filters:
  - Known background daemons / services (babysit-prs, claude-usage,
    daemon-runner.sh, MCP servers, statusline scripts) are skipped so they
    don't masquerade as "someone is working on this branch".
  - Claude invoked with `-p`/`--print` is a one-shot non-interactive call;
    those are always automation, never a live session.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import load as load_config
from .models import AgentProcess


@dataclass(slots=True)
class ProcessRow:
    pid: int
    ppid: int
    cpu_pct: float
    command: str
    cwd: str | None = None


# CPU% floor for a process to count as "actively working". macOS %cpu is a
# recent decaying average (not lifetime), so idle REPLs sitting at a prompt
# sample at 0.0% while an agent that's processing a turn typically sits
# between 1-60% depending on load. 1% is comfortably above the noise floor.
_ACTIVE_CPU_THRESHOLD = 1.0


def discover_active_agents(worktree_paths: list[str]) -> dict[str, list[AgentProcess]]:
    """Return active Claude/Codex sessions grouped by worktree path."""
    sorted_paths = sorted({path for path in worktree_paths if path}, key=len, reverse=True)
    if not sorted_paths:
        return {}

    rows = _parse_process_rows(_run_process_table())
    if not rows:
        return {}

    cwds = _collect_cwds()
    for row in rows:
        row.cwd = cwds.get(row.pid)

    by_pid = {row.pid: row for row in rows}
    children_by_pid: dict[int, list[ProcessRow]] = {}
    for row in rows:
        children_by_pid.setdefault(row.ppid, []).append(row)

    result: dict[str, list[AgentProcess]] = {}
    seen_keys: set[tuple[str, str, int]] = set()

    for row in rows:
        cli_name = _agent_cli_name(row.command)
        if not cli_name:
            continue

        # When `node /path/to/codex` spawns the real codex binary as a child,
        # skip the child — we only want one entry per logical agent.
        parent = by_pid.get(row.ppid)
        if parent and _agent_cli_name(parent.command) == cli_name:
            continue

        if _is_noninteractive(row, by_pid):
            continue

        # Gate on recent activity. macOS %cpu is a decaying recent average,
        # so an idle REPL at a prompt will sample near zero while a session
        # that's actually processing a turn won't. For the node→codex
        # wrapper case the wrapper itself is always near 0%, so roll in the
        # same-cli descendants' CPU too.
        effective_cpu = _effective_cpu(row, cli_name, children_by_pid)
        if effective_cpu < _ACTIVE_CPU_THRESHOLD:
            continue

        worktree_path = _resolve_worktree(row, by_pid, sorted_paths)
        if not worktree_path:
            continue

        key = (worktree_path, cli_name, row.pid)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        result.setdefault(worktree_path, []).append(
            AgentProcess(
                pid=row.pid,
                cli_name=cli_name,
                label=cli_name.capitalize(),
                command=row.command[:240],
            )
        )

    for agents in result.values():
        agents.sort(key=lambda agent: (agent.label, agent.pid))

    return result


def discover_primary_feature_pipeline_agents(
    worktree_paths: list[str], *, min_cpu: float = _ACTIVE_CPU_THRESHOLD,
    discovery_names: set[str] | None = None,
) -> dict[str, list[AgentProcess]]:
    """Return interactive feature-pipeline sessions by worktree.

    ``min_cpu`` is the CPU% floor for counting a process as actively working.
    The default keeps the dashboard's "who's busy now" semantics. Pass
    ``min_cpu=0.0`` for an OWNERSHIP/liveness check, where an idle session
    sitting at a prompt still owns its worktree (its `%cpu` decays to ~0 but it
    is very much alive) — the dashboard's activity gate would otherwise miss it
    and let another session adopt and service the worktree (BOU-1540).

    ``discovery_names`` is the recognized-CLI allow-list. Pass the TARGET repo's
    list (or, for a mixed candidate set, the UNION across candidates) — resolving
    it from the process cwd would miss a custom-CLI owner a target repo
    recognizes, or count one it excludes (PR #7 review, P2). Each returned
    ``AgentProcess`` carries ``cli_name``, so a caller with per-candidate
    allow-lists can filter precisely. Defaults to the process cwd config.
    """
    sorted_paths = sorted({path for path in worktree_paths if path}, key=len, reverse=True)
    if not sorted_paths:
        return {}
    allowed_clis = discovery_names if discovery_names is not None else set(load_config().discovery_names)

    rows = _parse_process_rows(_run_process_table())
    if not rows:
        return {}

    cwds = _collect_cwds()
    for row in rows:
        row.cwd = cwds.get(row.pid)

    by_pid = {row.pid: row for row in rows}
    children_by_pid: dict[int, list[ProcessRow]] = {}
    for row in rows:
        children_by_pid.setdefault(row.ppid, []).append(row)

    result: dict[str, list[AgentProcess]] = {}
    for row in rows:
        if not _is_feature_pipeline_invocation(row.command):
            continue
        cli_name = _command_cli_name(row.command, allowed_clis)
        if cli_name not in allowed_clis:
            continue
        if _is_noninteractive(row, by_pid):
            continue
        if _effective_cpu_for_cli(row, cli_name, children_by_pid) < min_cpu:
            continue

        worktree_path = _resolve_worktree(row, by_pid, sorted_paths)
        if not worktree_path:
            continue
        result.setdefault(worktree_path, []).append(
            AgentProcess(
                pid=row.pid,
                cli_name=cli_name,
                label=cli_name.capitalize(),
                command=row.command[:240],
            )
        )

    for agents in result.values():
        agents.sort(key=lambda agent: agent.pid)
    return result


def _effective_cpu(
    row: ProcessRow,
    cli_name: str,
    children_by_pid: dict[int, list[ProcessRow]],
) -> float:
    """Max %cpu across a process and its same-CLI descendants.

    Bounded DFS so we don't walk the entire tree for a non-agent process.
    """
    best = row.cpu_pct
    stack = list(children_by_pid.get(row.pid, []))
    while stack:
        child = stack.pop()
        if _agent_cli_name(child.command) != cli_name:
            continue
        if child.cpu_pct > best:
            best = child.cpu_pct
        stack.extend(children_by_pid.get(child.pid, []))
    return best


def _effective_cpu_for_cli(
    row: ProcessRow,
    cli_name: str,
    children_by_pid: dict[int, list[ProcessRow]],
) -> float:
    best = row.cpu_pct
    stack = list(children_by_pid.get(row.pid, []))
    while stack:
        child = stack.pop()
        if _command_cli_name(child.command) != cli_name:
            continue
        if child.cpu_pct > best:
            best = child.cpu_pct
        stack.extend(children_by_pid.get(child.pid, []))
    return best


def _run_process_table() -> str:
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,%cpu=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""

    if proc.returncode != 0:
        return ""
    return proc.stdout


def _parse_process_rows(output: str) -> list[ProcessRow]:
    rows: list[ProcessRow] = []
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        parts = raw_line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_text, ppid_text, cpu_text, command = parts
        if not pid_text.isdigit() or not ppid_text.isdigit():
            continue
        try:
            cpu_pct = float(cpu_text)
        except ValueError:
            cpu_pct = 0.0
        rows.append(ProcessRow(
            pid=int(pid_text),
            ppid=int(ppid_text),
            cpu_pct=cpu_pct,
            command=command,
        ))
    return rows


def _collect_cwds() -> dict[int, str]:
    """Get cwd for every process in one lsof call.

    Output format (one field per line):
        p<pid>
        n<cwd>
    """
    try:
        proc = subprocess.run(
            ["lsof", "-d", "cwd", "-Fpn"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}

    cwds: dict[int, str] = {}
    current_pid: int | None = None
    for line in proc.stdout.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            try:
                current_pid = int(value)
            except ValueError:
                current_pid = None
        elif tag == "n" and current_pid is not None:
            cwds[current_pid] = value
    return cwds


def _resolve_worktree(
    row: ProcessRow,
    by_pid: dict[int, ProcessRow],
    worktree_paths: list[str],
) -> str | None:
    """Attribute a process to a worktree by its own cwd (no parent fallback).

    Parent-chain fallback sounds appealing but backfires for orphans: a codex
    whose worktree was reaped still reports its old cwd via lsof (the kernel
    holds the inode open), and that path no longer matches any live worktree.
    If we then walked the parent chain we'd pick up whichever shell spawned
    it and plop the orphan onto an unrelated card. Unattributed is better.
    """
    if not row.cwd:
        return None
    return _match_path(row.cwd, worktree_paths)


def _match_path(cwd: str, worktree_paths: list[str]) -> str | None:
    for path in worktree_paths:
        if cwd == path or cwd.startswith(path + "/"):
            return path
    return None


# Patterns that identify background/service processes that happen to be
# claude/codex but aren't "someone working on this branch right now".
_SERVICE_SNIPPETS = (
    "serena start-mcp-server",
    "claude-in-mobile",
    "daemon-runner.sh",
    "statusline.sh",
    "ShipIt",
    "codex app-server",
    "feature-pipeline",
    "/babysit-prs",
    "/claude-usage",
)


def _is_feature_pipeline_invocation(command: str) -> bool:
    """True only for an actual feature-pipeline *invocation*, not any command
    that merely mentions the phrase.

    The dashboard's own maintenance prompt contains the prose "Use the project
    feature-pipeline conventions for PR maintenance...", and unrelated
    automation (e.g. a usage analyzer summarizing dashboard logs, or a
    `codex exec` run whose prompt quotes a maintenance handoff) can echo that
    text verbatim on its command line. Matching the bare substring made every
    such process look like a live interactive session and caused the dashboard
    to defer PR maintenance to a ghost. A genuine invocation carries the
    slash-command token (`/feature-pipeline`) or the plugin skill namespace
    (`feature-pipeline:`).
    """
    return "/feature-pipeline" in command or "feature-pipeline:" in command


def _agent_cli_name(command: str) -> str | None:
    if any(snippet in command for snippet in _SERVICE_SNIPPETS):
        return None

    return _command_cli_name(command)


def _command_cli_name(command: str, discovery_names: set[str] | None = None) -> str | None:

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    if not tokens:
        return None

    # Caller may supply the TARGET repo's allow-list; otherwise fall back to the
    # process cwd config. Resolving it here (not only after this returns) is what
    # lets a custom-CLI owner from another repo be recognized (PR #7 review, P2).
    if discovery_names is None:
        discovery_names = set(load_config().discovery_names)
    executable = Path(tokens[0]).name
    if executable in discovery_names:
        return executable

    if executable == "node" and len(tokens) > 1:
        script_name = Path(tokens[1]).name
        if script_name in discovery_names:
            return script_name

    return None


def _is_noninteractive(row: ProcessRow, by_pid: dict[int, ProcessRow]) -> bool:
    """Skip one-shot print-mode claudes and anything spawned by a daemon runner."""
    try:
        tokens = shlex.split(row.command)
    except ValueError:
        tokens = row.command.split()

    executable = Path(tokens[0]).name if tokens else ""
    if executable == "claude" or (executable == "node" and len(tokens) > 1 and Path(tokens[1]).name == "claude"):
        for tok in tokens[1:]:
            if tok in {"-p", "--print"}:
                return True

    # Walk the parent chain; if any ancestor's command matches a known
    # daemon/service pattern, treat this as a service invocation.
    seen: set[int] = set()
    current = by_pid.get(row.ppid)
    while current is not None and current.pid not in seen:
        seen.add(current.pid)
        if any(snippet in current.command for snippet in _SERVICE_SNIPPETS):
            return True
        current = by_pid.get(current.ppid)

    return False
