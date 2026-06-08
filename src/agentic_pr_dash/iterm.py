"""iTerm helpers for focusing or opening worktree sessions."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path


def focus_or_open_worktree(path: str) -> bool:
    """Focus an iTerm tab for a worktree if found, otherwise open a new tab."""
    worktree_path = Path(path).expanduser()
    if not worktree_path.exists():
        return False

    shell_command = f"cd {shlex.quote(str(worktree_path))} && clear"
    worktree_name = worktree_path.name

    script = """
on run argv
    set targetPath to item 1 of argv
    set targetName to item 2 of argv
    set shellCommand to item 3 of argv

    tell application "iTerm2"
        activate
        if (count of windows) > 0 then
            repeat with aWindow in windows
                repeat with aTab in tabs of aWindow
                    repeat with aSession in sessions of aTab
                        set sessionName to ""
                        set tabName to ""
                        try
                            set sessionName to name of aSession
                        end try
                        try
                            set tabName to name of aTab
                        end try
                        if sessionName contains targetName or tabName contains targetName then
                            tell aWindow
                                select
                                set current tab to aTab
                            end tell
                            return "focused"
                        end if
                    end repeat
                end repeat
            end repeat
        end if

        if (count of windows) is 0 then
            create window with default profile
        else
            tell current window to create tab with default profile
        end if

        tell current session of current window
            write text shellCommand
        end tell
        return "opened"
    end tell
end run
""".strip()

    return _run_osascript("iTerm2", script, [str(worktree_path), worktree_name, shell_command])


def _run_osascript(app_name: str, script: str, args: list[str]) -> bool:
    if app_name != "iTerm2":
        return False

    try:
        proc = subprocess.run(
            ["osascript", "-", *args],
            input=script,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    return proc.returncode == 0
