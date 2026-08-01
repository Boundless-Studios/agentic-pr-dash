"""Runtime-neutral Stop-hook adapter over the canonical maintenance gate."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import maintenance_check
from .config import load as load_config


@dataclass(frozen=True)
class StopHookRequest:
    """Host policy/configuration supplied to the portable stop-gate adapter."""

    cwd: str
    session_id: str = ""
    no_waiter: bool = False
    policy_path: str | None = None
    ledger_path: str | None = None


def _state_dir(request: StopHookRequest) -> Path:
    return load_config(request.cwd).state_dir_for(request.cwd)


def _streak_path(request: StopHookRequest) -> Path:
    return _state_dir(request) / "pr-watch.stopgate-failstreak.json"


def _read_streak(request: StopHookRequest) -> int:
    try:
        payload = json.loads(_streak_path(request).read_text(encoding="utf-8"))
        if payload.get("session_id") == request.session_id:
            return max(0, int(payload.get("streak", 0)))
    except (OSError, ValueError, TypeError):
        pass
    return 0


def _write_streak(request: StopHookRequest, streak: int) -> None:
    path = _streak_path(request)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"session_id": request.session_id, "streak": streak}),
            encoding="utf-8",
        )
    except OSError:
        pass


def _release_marker(request: StopHookRequest, streak: int, error_type: str) -> Path:
    path = _state_dir(request) / "pr-watch.stopgate-release.jsonl"
    record = {
        "event": "stop_gate_escape_release",
        "at": datetime.now(UTC).isoformat(),
        "session_id": request.session_id,
        "failure_count": streak,
        "reason": "internal_error",
        "error_type": error_type,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass
    return path


def _failure_limit() -> int:
    raw = os.environ.get("AGENTIC_PR_DASH_STOP_HOOK_FAILURE_LIMIT", "3")
    return int(raw) if raw.isdigit() and int(raw) > 0 else 3


def _argv(request: StopHookRequest) -> list[str]:
    argv = ["stop-gate", "--cwd", request.cwd]
    if request.session_id:
        argv.extend(("--session-id", request.session_id))
    if request.policy_path:
        argv.extend(("--policy", request.policy_path))
    if request.ledger_path:
        argv.extend(("--ledger", request.ledger_path))
    if request.no_waiter:
        argv.append("--no-waiter")
    return argv


def run_stop_hook(request: StopHookRequest) -> int:
    """Run the canonical gate and bound repeated internal-error blocking.

    Ordinary clean/pending/partial results are entirely owned and rendered by
    ``maintenance_check``. This boundary handles only an unexpected exception:
    fail closed twice, then persist a redacted release record and allow the host
    session to stop so broken infrastructure cannot wedge it forever.
    """
    try:
        result = int(maintenance_check.main(_argv(request)))
    except Exception as exc:  # noqa: BLE001 - hook boundary must never traceback
        error_type = type(exc).__name__
    else:
        if result in (0, 2):
            _write_streak(request, 0)
            return result
        error_type = f"UnexpectedExit{result}"

    streak = _read_streak(request) + 1
    limit = _failure_limit()
    if streak >= limit:
        _write_streak(request, 0)
        marker = _release_marker(request, streak, error_type)
        print(
            f"[pr-watch] RELEASING: stop-gate internal error {streak}x "
            f"consecutively; marker: {marker}",
            file=sys.stderr,
        )
        return 0
    _write_streak(request, streak)
    print(
        f"[pr-watch] BLOCKING: stop-gate internal error "
        f"({streak}/{limit}); retry after checking the installed runtime.",
        file=sys.stderr,
    )
    return 2
