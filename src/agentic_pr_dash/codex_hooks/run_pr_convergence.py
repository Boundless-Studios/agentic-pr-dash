"""Thin, runtime-neutral adapter for durable PR convergence intents."""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime
from os import PathLike
from pathlib import Path

from agentic_pr_dash.codex_hooks._payload import load_payload, normalized_payload
from agentic_pr_dash.codex_hooks.command_parser import (
    cd_target,
    effective_git_cwd,
    is_git_push,
    parse_gh_pr_arm_target,
    split_command_segments,
)

WORKFLOW_TYPE = "pr-maintenance"
_PR_URL = re.compile(
    r"https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)"
)
_DIRECT_GITHUB_REMOTE = re.compile(
    r"^(?:https?://github\.com/|ssh://git@github\.com/|git@github\.com:)",
    re.IGNORECASE,
)
_PUSH_UPDATE = re.compile(
    r"^(?:[0-9a-f]{4,}\.{2,3}[0-9a-f]{4,}|"
    r"\[(?:new branch|new tag|deleted|up to date)\])\s+.+\s+->\s+.+$",
    re.IGNORECASE,
)
_IndexedTarget = tuple[int, str, str, str | None, str | None]


class LocalGitIdentity:
    __slots__ = ("head_sha", "pushed_ref", "repository", "worktree_path")

    def __init__(
        self,
        repository: str,
        pushed_ref: str,
        head_sha: str,
        worktree_path: str,
    ) -> None:
        self.repository = repository
        self.pushed_ref = pushed_ref
        self.head_sha = head_sha
        self.worktree_path = worktree_path


def _git(cwd: str, *args: str) -> tuple[str, ...]:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return ()
    if result.returncode != 0:
        return ()
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _worktree_root(cwd: str) -> Path | None:
    try:
        candidate = Path(cwd).expanduser().resolve()
    except OSError:
        return None
    if candidate.is_file():
        candidate = candidate.parent
    for directory in (candidate, *candidate.parents):
        if (directory / ".git").exists():
            return directory
    return None


def _git_directories(worktree: Path) -> tuple[Path, Path] | None:
    marker = worktree / ".git"
    if marker.is_dir():
        return marker, marker
    try:
        prefix, separator, value = (
            marker.read_text(encoding="utf-8").strip().partition(":")
        )
    except OSError:
        return None
    if prefix.casefold() != "gitdir" or not separator or not value.strip():
        return None
    git_dir = Path(value.strip()).expanduser()
    if not git_dir.is_absolute():
        git_dir = (worktree / git_dir).resolve()
    common_dir = git_dir
    try:
        common_value = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    else:
        common_dir = (git_dir / common_value).resolve()
    return git_dir, common_dir


def _packed_ref(common_dir: Path, ref_name: str) -> str:
    try:
        lines = (common_dir / "packed-refs").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    suffix = f" {ref_name}"
    for line in lines:
        if not line.startswith(("#", "^")) and line.endswith(suffix):
            return line.split(" ", 1)[0].strip()
    return ""


def _read_ref(git_dir: Path, common_dir: Path, ref_name: str) -> str:
    for base in (git_dir, common_dir):
        try:
            head_sha = (base / ref_name).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if head_sha:
            return head_sha
    return _packed_ref(common_dir, ref_name)


def _head_identity(git_dir: Path, common_dir: Path) -> tuple[str, str] | None:
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return (head, "HEAD") if head else None
    pushed_ref = head.removeprefix("ref:").strip()
    if not pushed_ref:
        return None
    head_sha = _read_ref(git_dir, common_dir, pushed_ref)
    return (head_sha, pushed_ref) if head_sha else None


def _local_branch_ref(branch: str) -> str | None:
    candidate = branch.strip()
    if candidate.startswith("refs/heads/"):
        candidate = candidate.removeprefix("refs/heads/")
    elif candidate.startswith("refs/"):
        return None
    invalid = {" ", "~", "^", ":", "?", "*", "[", "\\"}
    components = candidate.split("/")
    if (
        not candidate
        or ".." in candidate
        or "@{" in candidate
        or any(character in candidate for character in invalid)
        or any(
            not component
            or component.startswith(".")
            or component.endswith((".", ".lock"))
            for component in components
        )
    ):
        return None
    return f"refs/heads/{candidate}"


def _origin_url(common_dir: Path) -> str:
    try:
        lines = (common_dir / "config").read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    section = ""
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().casefold()
            continue
        if section != 'remote "origin"' or not line or line.startswith(("#", ";")):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            key, separator, value = line.partition(" ")
        if separator and key.strip().casefold() == "url":
            return value.strip().strip('"')
    return ""


def _filesystem_git_identity(
    cwd: str,
    *,
    branch: str | None = None,
) -> LocalGitIdentity | None:
    worktree = _worktree_root(cwd)
    if worktree is None:
        return None
    directories = _git_directories(worktree)
    if directories is None:
        return None
    git_dir, common_dir = directories
    if branch is None:
        head = _head_identity(git_dir, common_dir)
    else:
        pushed_ref = _local_branch_ref(branch)
        if pushed_ref is None:
            return None
        head_sha = _read_ref(git_dir, common_dir, pushed_ref)
        head = (head_sha, pushed_ref) if head_sha else None
    if head is None:
        return None
    head_sha, pushed_ref = head
    raw_remote = _origin_url(common_dir)
    if _DIRECT_GITHUB_REMOTE.match(raw_remote):
        remote = raw_remote
    else:
        resolved_remote = _git(str(worktree), "remote", "get-url", "origin")
        if len(resolved_remote) != 1:
            return None
        remote = resolved_remote[0]
    try:
        repository = _canonical_repository(remote)
    except ValueError:
        return None
    return LocalGitIdentity(
        repository=repository,
        pushed_ref=pushed_ref,
        head_sha=head_sha,
        worktree_path=str(worktree),
    )


def _subprocess_git_identity(
    cwd: str,
    *,
    branch: str | None = None,
) -> LocalGitIdentity | None:
    if branch is None:
        checkout = _git(
            cwd,
            "rev-parse",
            "--show-toplevel",
            "HEAD",
            "--symbolic-full-name",
            "HEAD",
        )
        if len(checkout) != 3:
            return None
        worktree_path, head_sha, pushed_ref = checkout
    else:
        pushed_ref = _local_branch_ref(branch)
        if pushed_ref is None:
            return None
        checkout = _git(cwd, "rev-parse", "--show-toplevel", pushed_ref)
        if len(checkout) != 2:
            return None
        worktree_path, head_sha = checkout
    remote = _git(worktree_path, "remote", "get-url", "origin")
    if len(remote) != 1:
        return None
    try:
        repository = _canonical_repository(remote[0])
    except ValueError:
        return None
    return LocalGitIdentity(
        repository=repository,
        pushed_ref=pushed_ref,
        head_sha=head_sha,
        worktree_path=worktree_path,
    )


def local_git_identity(
    cwd: str,
    *,
    branch: str | None = None,
) -> LocalGitIdentity | None:
    return _filesystem_git_identity(cwd, branch=branch) or _subprocess_git_identity(
        cwd,
        branch=branch,
    )


def _canonical_repository(value: str) -> str:
    from agentic_pr_dash.lifecycle_models import (
        canonical_repository,
    )

    return canonical_repository(value)


def _exit_code(payload: dict) -> int | None:
    response = payload.get("tool_response")
    mappings = (response, payload) if isinstance(response, dict) else (payload,)
    for mapping in mappings:
        for key in ("exit_code", "exitCode", "returncode", "code"):
            value = mapping.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.lstrip("-").isdigit():
                return int(value)
    return None


def _output_pr_identity(payload: dict) -> tuple[str, int] | None:
    response = payload.get("tool_response")
    if not isinstance(response, dict):
        return None
    for key in ("stdout", "output", "text"):
        value = response.get(key)
        if not isinstance(value, str):
            continue
        for match in _PR_URL.finditer(value):
            return (
                f"{match.group('owner')}/{match.group('repo')}",
                int(match.group("number")),
            )
    return None


def _tool_output(payload: dict) -> str:
    response = payload.get("tool_response")
    if not isinstance(response, dict):
        return ""
    return "\n".join(
        value
        for key in ("stdout", "stderr", "output", "text")
        if isinstance((value := response.get(key)), str)
    )


def _output_proves_push_succeeded(payload: dict) -> bool:
    output = _tool_output(payload)
    lowered = output.casefold()
    if (
        "error: failed to push some refs" in lowered
        or "fatal:" in lowered
        or "[rejected]" in lowered
        or "[remote rejected]" in lowered
    ):
        return False
    for line in output.splitlines():
        candidate = line.strip()
        if candidate == "Everything up-to-date":
            return True
        if candidate[:1] in {"*", "+", "-", "="}:
            candidate = candidate[1:].lstrip()
        if _PUSH_UPDATE.fullmatch(candidate):
            return True
    return False


def _merge_shell_state(
    states: dict[
        tuple[bool, str | None],
        frozenset[_IndexedTarget],
    ],
    key: tuple[bool, str | None],
    targets: frozenset[_IndexedTarget],
) -> None:
    if key in states:
        states[key] &= targets
    else:
        states[key] = targets


def _relocated_cwd(cwd: str | None, destination: str) -> str | None:
    expanded = os.path.expanduser(destination)
    if os.path.isabs(expanded):
        return expanded
    if cwd is None:
        return None
    return str((Path(cwd) / expanded).resolve())


def _successful_targets(
    command: str,
    base_cwd: str,
    *,
    exit_code: int,
    payload: dict,
) -> tuple[tuple[str, str, str | None, str | None], ...]:
    segments = split_command_segments(command)
    ambiguous = {
        index
        for index, (operator, _segment) in enumerate(segments)
        if operator in {"|", "&"}
        for index in (index - 1, index)
        if index >= 0
    }
    push_segments = sum(
        index not in ambiguous and is_git_push(segment)
        for index, (_operator, segment) in enumerate(segments)
    )
    output_proves_single_push = push_segments == 1 and _output_proves_push_succeeded(
        payload
    )
    states: dict[tuple[bool, str | None], frozenset[_IndexedTarget]] = {
        (True, base_cwd): frozenset()
    }
    for index, (leading_op, segment) in enumerate(segments):
        destination = cd_target(segment)
        push = index not in ambiguous and is_git_push(segment)
        pr_target = parse_gh_pr_arm_target(segment) if index not in ambiguous else None
        next_states: dict[tuple[bool, str | None], frozenset[_IndexedTarget]] = {}
        for (previous_succeeded, cwd), proven_targets in states.items():
            executes = (
                leading_op in {"", ";"}
                or (leading_op == "&&" and previous_succeeded)
                or (leading_op == "||" and not previous_succeeded)
            )
            if not executes:
                if push and output_proves_single_push:
                    continue
                _merge_shell_state(
                    next_states,
                    (previous_succeeded, cwd),
                    proven_targets,
                )
                continue
            if destination is not None:
                _merge_shell_state(
                    next_states,
                    (True, _relocated_cwd(cwd, destination)),
                    proven_targets,
                )
                _merge_shell_state(
                    next_states,
                    (False, cwd),
                    proven_targets,
                )
                continue
            outcomes = (True,) if push and output_proves_single_push else (True, False)
            for succeeded in outcomes:
                next_targets = proven_targets
                if succeeded and cwd is not None:
                    if push:
                        next_targets |= {
                            (
                                index,
                                "push",
                                effective_git_cwd(segment, cwd),
                                None,
                                None,
                            )
                        }
                    elif pr_target is not None:
                        pr_number, branch = pr_target
                        next_targets |= {(index, "pr", cwd, pr_number, branch)}
                _merge_shell_state(
                    next_states,
                    (succeeded, cwd),
                    next_targets,
                )
        states = next_states

    expected_success = exit_code == 0
    proven: frozenset[_IndexedTarget] | None = None
    for (succeeded, _cwd), targets in states.items():
        if succeeded != expected_success:
            continue
        proven = targets if proven is None else proven & targets
    if not proven:
        return ()
    return tuple(
        (kind, cwd, pr_number, branch)
        for _index, kind, cwd, pr_number, branch in sorted(proven)
    )


def _enqueue_target(
    *,
    kind: str,
    cwd: str,
    explicit_pr_number: str | None,
    target_branch: str | None,
    payload: dict,
    state_root: str | PathLike[str] | None,
    now: datetime | None,
) -> None:
    pr_number = int(explicit_pr_number) if explicit_pr_number is not None else None
    identity = local_git_identity(cwd, branch=target_branch)
    if identity is None:
        return
    if kind == "push":
        canonical_repository = _prior_pr_repository(identity, state_root)
        if canonical_repository is not None:
            identity = LocalGitIdentity(
                canonical_repository,
                identity.pushed_ref,
                identity.head_sha,
                identity.worktree_path,
            )
    if kind == "pr" and pr_number is None:
        output_identity = _output_pr_identity(payload)
        if output_identity is not None:
            repository, pr_number = output_identity
            identity = LocalGitIdentity(
                repository,
                identity.pushed_ref,
                identity.head_sha,
                identity.worktree_path,
            )
    enqueue_maintenance(
        build_maintenance_intent(
            identity,
            session_id=str(payload.get("session_id") or "unattributed"),
            reason=(
                "post-push maintenance"
                if kind == "push"
                else "post-pr-create maintenance"
            ),
            now=now,
            pr_number=pr_number,
        ),
        root=state_root,
    )


def _prior_pr_repository(
    identity: LocalGitIdentity,
    state_root: str | PathLike[str] | None,
) -> str | None:
    """Reuse a PR-created upstream identity for later pushes from a fork."""

    from agentic_pr_dash.lifecycle_store import LifecycleStore

    candidates = [
        record.intent
        for record in LifecycleStore(state_root).list_intents()
        if record.intent.pr_number is not None
        and record.intent.pushed_ref == identity.pushed_ref
        and record.intent.worktree_path == identity.worktree_path
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda intent: intent.requested_at).repository


def enqueue_maintenance(
    intent: object, *, root: str | PathLike[str] | None = None
) -> object:
    """Lazy, injectable boundary over the typed durable enqueue operation."""

    from agentic_pr_dash.lifecycle_store import (
        enqueue_maintenance as durable_enqueue,
    )

    return durable_enqueue(intent, root=root)


def build_maintenance_intent(
    identity: LocalGitIdentity,
    *,
    session_id: str,
    reason: str,
    now: datetime | None = None,
    pr_number: int | None = None,
) -> object:
    from agentic_pr_dash.lifecycle_models import MaintenanceIntentV1

    return MaintenanceIntentV1(
        repository=identity.repository,
        pushed_ref=identity.pushed_ref,
        head_sha=identity.head_sha,
        workflow_type=WORKFLOW_TYPE,
        reason=reason,
        worktree_path=identity.worktree_path,
        session_id=session_id or "unattributed",
        requested_at=now or datetime.now(UTC),
        pr_number=pr_number,
    )


def _run_payload(
    payload: dict,
    *,
    event: str | None = None,
    state_root: str | PathLike[str] | None = None,
    now: datetime | None = None,
) -> int:
    """Handle one normalized lifecycle payload without remote observation."""

    phase = event or str(payload.get("hook_event_name") or "")
    if phase == "SessionEnd":
        from agentic_pr_dash import session_registry

        session_id = str(payload.get("session_id") or "")
        if session_id:
            normalized = normalized_payload(payload)
            runtime = str(
                payload.get("runtime")
                or payload.get("cli")
                or payload.get("tool_name")
                or "unknown"
            )
            session_registry.record_event(
                event="completed",
                session_id=session_id,
                cli=runtime,
                launch_source="lifecycle-hook",
                worktree_path=str(normalized["cwd"]),
                exit_code=0,
            )
        return 0
    if phase == "Stop":
        from agentic_pr_dash.stop_hook import (
            StopHookRequest,
            run_stop_hook,
        )

        normalized = normalized_payload(payload)
        return run_stop_hook(
            StopHookRequest(
                cwd=str(normalized["cwd"]),
                session_id=str(payload.get("session_id") or ""),
                state_root=state_root,
            ),
            now=now,
        )
    if phase != "PostToolUse":
        return 0
    exit_code = _exit_code(payload)
    if exit_code is None:
        exit_code = 0
    normalized = normalized_payload(payload)
    if normalized.get("tool_name") != "Bash":
        return 0
    tool_input = normalized.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return 0
    for kind, cwd, pr_number, branch in _successful_targets(
        command,
        str(normalized["cwd"]),
        exit_code=exit_code,
        payload=payload,
    ):
        _enqueue_target(
            kind=kind,
            cwd=cwd,
            explicit_pr_number=pr_number,
            target_branch=branch,
            payload=payload,
            state_root=state_root,
            now=now,
        )
    return 0


def run_payload(
    payload: dict,
    *,
    event: str | None = None,
    state_root: str | PathLike[str] | None = None,
    now: datetime | None = None,
) -> int:
    """Best-effort hook boundary: local failures never block the host runtime."""

    try:
        return _run_payload(
            payload,
            event=event,
            state_root=state_root,
            now=now,
        )
    except Exception:  # noqa: BLE001 - lifecycle hooks are advisory boundaries
        return 0


def main(argv: list[str] | None = None) -> int:
    """Read one hook payload from stdin and run the requested lifecycle event."""

    args = list(sys.argv[1:] if argv is None else argv)
    payload = load_payload()
    event = args[0] if args else str(payload.get("hook_event_name") or "")
    return run_payload(payload, event=event)


if __name__ == "__main__":
    raise SystemExit(main())
