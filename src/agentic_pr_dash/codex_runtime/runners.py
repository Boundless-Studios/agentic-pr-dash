"""Generic Codex hook dispatch loops (BOU-1672).

Three repo-agnostic runners the host repo parametrizes with its own config:

* :func:`run_shared_hook` — normalize a Codex payload and forward Bash
  PreToolUse-style events to a single target hook script.
* :class:`StopChecksRunner` — fan a normalized Stop payload across an ORDERED
  list of (name, path) hooks, running ALL enabled ones (never break on first
  non-zero), forwarding at most one authoritative JSON object to stdout and
  condensing the rest to stderr. The hook LIST and the enable predicate are
  supplied by the host repo (its Claude↔Codex parity invariant lives there).
* :func:`run_session_commands` — best-effort run an ordered list of
  (name, argv) SessionStart commands, gated by an enable predicate.

None of these embed a hook list or a path layout — that is host config.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MAX_CONDENSED_HOOK_OUTPUT_LINES = 20
HOOK_FAILURE_OUTPUT_LIMIT = 4096
STOP_HOOK_TIMEOUT_SECONDS = 30
HookFailureClass = Literal[
    "timeout",
    "spawn_failure",
    "invalid_json",
    "unexpected_exit",
]


@dataclass(frozen=True, slots=True)
class HookFailure:
    """Bounded, replayable details for a Stop hook that needs repair."""

    hook_name: str
    hook_path: str
    failure_class: HookFailureClass
    retry_argv: tuple[str, ...]
    stderr: str


@dataclass(frozen=True, slots=True)
class StopChecksReport:
    """Structured result retained without changing ``StopChecksRunner.run``."""

    final_rc: int
    failures: tuple[HookFailure, ...]


def _bounded_failure_output(
    stderr: str | bytes | None,
    *,
    payload_text: str,
    hook_env: dict[str, str],
) -> str:
    """Redact runner inputs from child diagnostics, then enforce the output bound."""
    if isinstance(stderr, bytes):
        text = stderr.decode(errors="replace")
    else:
        text = stderr or ""
    sensitive_values = {payload_text, *(value for value in hook_env.values() if len(value) >= 4)}
    for value in sorted(sensitive_values, key=len, reverse=True):
        if value:
            text = text.replace(value, "[REDACTED]")
    return text.strip()[:HOOK_FAILURE_OUTPUT_LIMIT]


def run_shared_hook(
    payload: dict,
    *,
    normalize: Callable[[dict], dict],
    apply_env: Callable[[dict], None],
    target: str | None,
    python: str | None = None,
) -> int:
    """Forward a normalized Bash hook payload to a single target script.

    Returns 0 for non-Bash events (nothing to forward), 2 when ``target`` is
    missing, else the target's exit code. ``normalize`` and ``apply_env`` are
    the host's payload/env helpers.
    """
    normalized = normalize(payload)
    apply_env(payload)

    if normalized.get("tool_name") != "Bash":
        return 0

    if not target:
        print("CODEX_SHARED_HOOK_TARGET is required", file=sys.stderr)
        return 2

    result = subprocess.run(
        [python or sys.executable, target],
        input=json.dumps(normalized),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def run_session_commands(
    commands: Iterable[tuple[str, list[str]]],
    *,
    is_enabled: Callable[[str], bool],
) -> None:
    """Best-effort run each ``(name, argv)`` whose ``name`` is enabled.

    Never raises and never short-circuits — a SessionStart command failing must
    not abort the session. Stdout/stderr of each command are forwarded through.
    """
    for name, command in commands:
        if not is_enabled(name):
            continue
        try:
            result = subprocess.run(command, text=True, capture_output=True, check=False)
        except OSError as exc:
            print(str(exc), file=sys.stderr)
            continue
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)


def _stdout_is_json(text: str) -> bool:
    """Return True iff *text* is a single valid JSON object."""
    stripped = text.strip()
    if not stripped:
        return False
    try:
        parsed = json.loads(stripped)
        return isinstance(parsed, dict)
    except json.JSONDecodeError:
        return False


class HumanOutputBuffer:
    """De-duplicates and condenses human-readable child hook output.

    Long chunks keep BOTH ends (blocking hooks put remediation/opt-out near the
    end); duplicates are dropped and counted.
    """

    def __init__(self) -> None:
        self._chunks: list[str] = []
        self._seen: set[str] = set()
        self._duplicate_count = 0

    def add(self, text: str) -> None:
        if not text:
            return
        if text in self._seen:
            self._duplicate_count += 1
            return
        self._seen.add(text)
        self._chunks.append(text)

    def emit(self) -> None:
        should_condense = self._duplicate_count > 0 or any(
            len(chunk.splitlines()) > MAX_CONDENSED_HOOK_OUTPUT_LINES for chunk in self._chunks
        )
        if should_condense:
            print("Codex stop hook output condensed:", file=sys.stderr)

        for chunk in self._chunks:
            lines = chunk.splitlines(keepends=True)
            if len(lines) <= MAX_CONDENSED_HOOK_OUTPUT_LINES:
                print(chunk, end="", file=sys.stderr)
                continue
            head_count = (MAX_CONDENSED_HOOK_OUTPUT_LINES + 1) // 2
            tail_count = MAX_CONDENSED_HOOK_OUTPUT_LINES - head_count
            omitted = len(lines) - head_count - tail_count
            print("".join(lines[:head_count]), end="", file=sys.stderr)
            print(f"... {omitted} hook output line(s) omitted ...", file=sys.stderr)
            if tail_count:
                print("".join(lines[-tail_count:]), end="", file=sys.stderr)

        if self._duplicate_count:
            print(
                f"... {self._duplicate_count} duplicate hook output omitted ...",
                file=sys.stderr,
            )


class StopChecksRunner:
    """Fan a normalized Stop payload across an ordered (name, path) hook list.

    Runs EVERY enabled hook (mirroring Claude, where each Stop hook is
    registered independently and the harness runs them all, blocking if ANY
    exits 2). Forwards at most one authoritative JSON object to stdout; every
    other child stdout (human text or a second JSON) plus all child stderr is
    routed to adapter stderr (condensed). ``final_rc`` blocks (2) if any hook
    blocked, else carries the first non-zero error code, else 0.

    The host supplies: the ordered ``hooks`` list, an ``is_enabled`` predicate,
    a ``resolve_path`` (relative hook path → absolute), and a ``hook_env`` dict.
    """

    def __init__(
        self,
        *,
        resolve_path: Callable[[str], str],
        is_enabled: Callable[[str], bool],
        python: str | None = None,
        timeout_seconds: int = STOP_HOOK_TIMEOUT_SECONDS,
    ) -> None:
        self._resolve_path = resolve_path
        self._is_enabled = is_enabled
        self._python = python or sys.executable
        self._timeout_seconds = timeout_seconds
        self.last_report = StopChecksReport(final_rc=0, failures=())

    @staticmethod
    def emit_error_json(message: str) -> None:
        """Emit a structured JSON error to stdout so the harness always gets parseable output."""
        print(json.dumps({"hook_event_name": "Stop", "error": message}))

    def run(
        self,
        hooks: Sequence[tuple[str, str]],
        *,
        payload_text: str,
        hook_env: dict[str, str],
        bypass_enable_check: Callable[[str], bool] | None = None,
    ) -> int:
        """Run *hooks* against *payload_text*; return the aggregate exit code.

        *bypass_enable_check* — when given and returning True for a hook name,
        that hook runs regardless of ``is_enabled`` (used for explicit override
        lists where every entry is intended to run).
        """
        final_rc = 0
        failures: list[HookFailure] = []
        self.last_report = StopChecksReport(final_rc=0, failures=())
        human_output = HumanOutputBuffer()
        # Defer JSON emission until every hook has run so a later BLOCKING
        # (exit-2) JSON can win over an earlier success/approval JSON. With
        # multiple Stop hooks, the harness must see the block reason, not the
        # first hook's stdout. We hold the best JSON candidate (and which hook
        # produced it) and route every other JSON/human chunk to stderr.
        chosen_json: str | None = None
        chosen_json_blocking = False

        for hook_name, hook_path in hooks:
            forced = bypass_enable_check is not None and bypass_enable_check(hook_name)
            if not forced and not self._is_enabled(hook_name):
                continue
            resolved_path = self._resolve_path(hook_path)
            retry_argv = (self._python, resolved_path)
            try:
                result = subprocess.run(
                    list(retry_argv),
                    input=payload_text,
                    text=True,
                    capture_output=True,
                    check=False,
                    env=hook_env,
                    timeout=self._timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                failures.append(
                    HookFailure(
                        hook_name=hook_name,
                        hook_path=resolved_path,
                        failure_class="timeout",
                        retry_argv=retry_argv,
                        stderr=_bounded_failure_output(
                            exc.stderr,
                            payload_text=payload_text,
                            hook_env=hook_env,
                        ),
                    )
                )
                if final_rc == 0:
                    final_rc = 1
                continue
            except OSError as exc:
                failures.append(
                    HookFailure(
                        hook_name=hook_name,
                        hook_path=resolved_path,
                        failure_class="spawn_failure",
                        retry_argv=retry_argv,
                        stderr=_bounded_failure_output(
                            str(exc),
                            payload_text=payload_text,
                            hook_env=hook_env,
                        ),
                    )
                )
                if final_rc == 0:
                    final_rc = 1
                continue
            child_stdout_is_json = _stdout_is_json(result.stdout)
            child_blocking = result.returncode == 2
            failure_class: HookFailureClass | None = None
            if child_blocking and not child_stdout_is_json:
                failure_class = "invalid_json"
            elif result.returncode not in (0, 2):
                failure_class = "unexpected_exit"
            if failure_class is not None:
                failures.append(
                    HookFailure(
                        hook_name=hook_name,
                        hook_path=resolved_path,
                        failure_class=failure_class,
                        retry_argv=retry_argv,
                        stderr=_bounded_failure_output(
                            result.stderr,
                            payload_text=payload_text,
                            hook_env=hook_env,
                        ),
                    )
                )

            if result.returncode != 0 and not child_stdout_is_json:
                if result.stdout:
                    human_output.add(result.stdout)
                error_detail = (
                    result.stderr.strip().splitlines()[-1]
                    if result.stderr.strip()
                    else "unknown error"
                )
                error_json = json.dumps(
                    {
                        "hook_event_name": "Stop",
                        "error": (
                            f"run_stop_checks: stop hook '{hook_name}' exited with code "
                            f"{result.returncode}: {error_detail}"
                        ),
                    }
                )
                # A blocking exit-2 error supersedes any earlier non-blocking
                # JSON; a non-blocking error only fills the slot if empty.
                if chosen_json is None or (child_blocking and not chosen_json_blocking):
                    if chosen_json is not None:
                        human_output.add(chosen_json)
                    chosen_json = error_json
                    chosen_json_blocking = child_blocking
                if result.stderr:
                    human_output.add(result.stderr)
            else:
                if result.stdout:
                    if child_stdout_is_json and (
                        chosen_json is None or (child_blocking and not chosen_json_blocking)
                    ):
                        # Promote this JSON to the authoritative output; demote
                        # any previously chosen (non-blocking) JSON to stderr.
                        if chosen_json is not None:
                            human_output.add(chosen_json)
                        chosen_json = result.stdout
                        chosen_json_blocking = child_blocking
                    else:
                        human_output.add(result.stdout)
                if result.stderr:
                    human_output.add(result.stderr)

            if result.returncode == 2:
                final_rc = 2
            elif result.returncode != 0 and final_rc == 0:
                final_rc = result.returncode

        if chosen_json is not None:
            print(chosen_json, end="")
        human_output.emit()
        self.last_report = StopChecksReport(final_rc=final_rc, failures=tuple(failures))
        return final_rc
