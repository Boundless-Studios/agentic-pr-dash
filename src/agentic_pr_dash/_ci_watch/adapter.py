"""Adapter rendering and invocation (project-specific surfaces, best-effort)."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _render(template: str, fields: dict[str, str]) -> str:
    out = template
    for key, value in fields.items():
        out = out.replace("{" + key + "}", value)
    return out


def run_adapter(template: str | None, fields: dict[str, str], cwd: Path) -> None:
    """Render ``template`` with ``fields`` and run it via the shell, best-effort.

    A missing template is a no-op. Any failure (non-zero, timeout, OSError) is
    swallowed — adapters are advisory progress mirrors and must never break the
    watcher or block the turn.
    """
    if not template:
        return
    command = _render(template, fields)
    try:
        subprocess.run(
            ["bash", "-lc", command],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
