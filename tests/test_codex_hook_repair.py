from __future__ import annotations

from agentic_pr_dash import codex_hook_repair


def test_stale_direct_peon_ping_adapter_needs_repair() -> None:
    command = "bash ~/.claude/hooks/peon-ping/adapters/codex.sh"
    assert codex_hook_repair._needs_repair(command, "/repo")


def test_primary_wrapper_is_stable() -> None:
    command = "python3 /repo/scripts/codex-hooks/run_peon_ping.py"
    assert not codex_hook_repair._needs_repair(command, command)
