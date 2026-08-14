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

import re
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
_REDACTED = "<redacted>"
_SECRET_MARKERS = (
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "SECRET",
    "API_KEY",
    "PRIVATE_KEY",
    "AUTH_TOKEN",
    "ACCESS_KEY",
    "CREDENTIAL",
)
_SECRET_ASSIGNMENT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_-]*?)=([^\s]+)")
_SECRET_LONG_OPTION_RE = re.compile(
    r"(--[A-Za-z0-9][A-Za-z0-9_-]*(?:token|password|passwd|secret|api-key|private-key|auth|credential)[A-Za-z0-9_-]*=)([^\s]+)",
    re.IGNORECASE,
)
_SECRET_LONG_OPTION_VALUE_RE = re.compile(
    r"(--[A-Za-z0-9][A-Za-z0-9_-]*(?:token|password|passwd|secret|api-key|private-key|auth|credential)[A-Za-z0-9_-]*)(\s+)([^\s]+)",
    re.IGNORECASE,
)
# A token that plausibly starts a NEW option (`-v`, `--model`), as opposed to a
# fragment of a quote-stripped multi-word secret value such as `-----BEGIN`.
_OPTION_BOUNDARY_RE = re.compile(r"^--?[A-Za-z0-9]")


class ProcessScanUnavailable(RuntimeError):
    """The local process scan could not be trusted to answer "who is home?".

    Raised only by :func:`worktree_occupants`. The display queries return an
    empty mapping on a failed scan, which is right for rendering and fatal for
    a destructive gate — see that function's docstring (BOU-2933).
    """


def discover_active_agents(
    worktree_paths: list[str], *, min_cpu: float = _ACTIVE_CPU_THRESHOLD
) -> dict[str, list[AgentProcess]]:
    """Return active Claude/Codex sessions grouped by worktree path.

    This is the dashboard's "who is busy right now" query. ``min_cpu`` is the
    CPU% floor for counting a process as actively working; the default keeps
    those display semantics, and ``min_cpu=0.0`` turns it into a liveness read
    (the BOU-1540 precedent already applied to
    :func:`discover_primary_feature_pipeline_agents`).

    **Do not use this to gate a destructive action.** It fails open twice over:
    idle occupants fall under the CPU floor, and a failed scan is reported as
    "nobody is home". Use :func:`worktree_occupants` (BOU-2933).
    """
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
        if effective_cpu < min_cpu:
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
                command=_redact_command_for_display(row.command),
            )
        )

    for agents in result.values():
        agents.sort(key=lambda agent: (agent.label, agent.pid))

    return result


def worktree_occupants(worktree_paths: list[str]) -> dict[str, list[AgentProcess]]:
    """Return EVERY live process whose cwd is inside each worktree.

    This is the safety gate for destructive actions (``git worktree remove
    --force``). It deliberately differs from :func:`discover_active_agents` on
    all three axes that made that function unsafe to reuse here (BOU-2933):

    * **No CPU floor.** An idle REPL sitting at a prompt, or a SIGSTOP'd agent
      on a detached tty, samples ~0% CPU and is exactly the occupant we most
      need to see — the incident that motivated this destroyed a checkout out
      from under a stopped codex, which then held codex's machine-wide log DB
      and took the CLI down in every worktree on the box.
    * **No CLI allow-list.** A stray pytest running from a ``.venv`` in the
      tree, or a user's shell sitting in it, is also a reason not to delete.
      Being over-conservative here costs disk; being wrong the other way
      destroys work.
    * **Fails closed.** If ``ps`` or ``lsof`` cannot be read we raise
      :class:`ProcessScanUnavailable` instead of reporting an empty mapping.
      Failing to *look* is not evidence that nobody is there.

    Attribution is by the process's own cwd only, never the parent chain — see
    :func:`_resolve_worktree` for why that matters for orphans specifically.
    """
    sorted_paths = sorted({path for path in worktree_paths if path}, key=len, reverse=True)
    if not sorted_paths:
        return {}

    rows = _parse_process_rows(_run_process_table())
    if not rows:
        raise ProcessScanUnavailable("process table unavailable (ps returned nothing)")

    cwds = _collect_cwds()
    if not cwds:
        raise ProcessScanUnavailable("cwd scan unavailable (lsof returned nothing)")

    result: dict[str, list[AgentProcess]] = {}
    for row in rows:
        cwd = cwds.get(row.pid)
        if not cwd:
            continue
        worktree_path = _match_path(cwd, sorted_paths)
        if not worktree_path:
            continue
        cli_name = _agent_cli_name(row.command) or ""
        result.setdefault(worktree_path, []).append(
            AgentProcess(
                pid=row.pid,
                cli_name=cli_name,
                label=(cli_name or _process_label(row.command)).capitalize(),
                command=_redact_command_for_display(row.command),
            )
        )

    for occupants in result.values():
        occupants.sort(key=lambda occupant: occupant.pid)
    return result


def _process_label(command: str) -> str:
    """Short human label for a non-agent occupant (``python``, ``zsh``, ...)."""
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    return Path(argv[0]).name if argv else "process"


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
                command=_redact_command_for_display(row.command),
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


def _redact_command_for_display(command: str, *, limit: int = 240) -> str:
    """Return a dashboard-safe command string without likely secret values."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return _redact_unparsed_command(command)[:limit]

    redacted: list[str] = []
    redact_next = False
    swallowing_value = False
    for token in tokens:
        if redact_next:
            redacted.append(_REDACTED)
            redact_next = False
            # `ps` output loses shell quoting, so a quoted multi-word secret
            # value arrives as several tokens; keep swallowing them until the
            # next option-like boundary rather than leaking the value's tail.
            swallowing_value = True
            continue

        if swallowing_value:
            if not _OPTION_BOUNDARY_RE.match(token):
                continue
            swallowing_value = False

        if _looks_like_secret_assignment(token):
            name, _, _value = token.partition("=")
            redacted.append(f"{name}={_REDACTED}")
            # An option's inline value (--private-key=...) can be multi-word
            # once `ps` strips shell quoting; swallow the tail like the
            # space-separated form. Bare env assignments (GH_TOKEN=... cmd)
            # must not swallow: the next token is the command itself.
            swallowing_value = token.startswith("-")
            continue

        if token.startswith("--") and "=" in token:
            name, _, value = token.partition("=")
            if _looks_like_secret_name(name.lstrip("-")):
                redacted.append(f"{name}={_REDACTED}")
                # An inline value can also be multi-word once `ps` strips the
                # shell quoting; swallow the tail like the space-separated form.
                swallowing_value = True
                continue
            # Wrapper forms hide the secret assignment in the option VALUE
            # (--env=GH_TOKEN=..., --build-arg=API_KEY=...).
            inner_name, inner_sep, inner_value = value.partition("=")
            if inner_sep and inner_value and _looks_like_secret_name(inner_name):
                redacted.append(f"{name}={inner_name}={_REDACTED}")
                swallowing_value = True
                continue

        if token.startswith("--") and _looks_like_secret_name(token.lstrip("-")):
            redacted.append(token)
            redact_next = True
            continue

        redacted.append(token)

    return " ".join(redacted)[:limit]


def _redact_unparsed_command(command: str) -> str:
    def redact_assignment(match: re.Match[str]) -> str:
        name = match.group(1)
        if _looks_like_secret_name(name):
            return f"{name}={_REDACTED}"
        # The greedy value may itself be a secret assignment hidden in an
        # option value (--env=GH_TOKEN=...).
        inner_name, inner_sep, inner_value = match.group(2).partition("=")
        if inner_sep and inner_value and _looks_like_secret_name(inner_name):
            return f"{name}={inner_name}={_REDACTED}"
        return match.group(0)

    command = _SECRET_ASSIGNMENT_RE.sub(redact_assignment, command)
    command = _SECRET_LONG_OPTION_RE.sub(rf"\1{_REDACTED}", command)
    return _SECRET_LONG_OPTION_VALUE_RE.sub(rf"\1\2{_REDACTED}", command)


def _looks_like_secret_assignment(token: str) -> bool:
    name, separator, value = token.partition("=")
    return bool(separator and value and _looks_like_secret_name(name))


def _looks_like_secret_name(name: str) -> bool:
    normalized = name.strip("-").upper().replace("-", "_")
    if any(marker in normalized for marker in _SECRET_MARKERS):
        return True
    # Align with _SECRET_LONG_OPTION_RE: any auth-ish name (--authorization,
    # --auth-header, OAUTH_*) carries a credential value. Over-redacting a
    # non-secret value here is a cosmetic loss; leaking one is not.
    return normalized == "PASS" or "AUTH" in normalized


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
