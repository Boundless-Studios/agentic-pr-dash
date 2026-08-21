from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agentic_pr_dash import codex_hook_repair


def test_stale_direct_peon_ping_adapter_needs_repair() -> None:
    command = "bash ~/.claude/hooks/peon-ping/adapters/codex.sh"
    assert codex_hook_repair._needs_repair(command, "/repo")


def test_primary_wrapper_is_stable() -> None:
    command = "python3 /repo/scripts/codex-hooks/run_peon_ping.py"
    assert not codex_hook_repair._needs_repair(command, command)


def test_repository_root_anchors_wrapper_outside_installed_package(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository_root = tmp_path / "gaia"
    repository_root.mkdir()
    target = tmp_path / "hooks.json"
    target.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "command": (
                                        "bash ~/.claude/hooks/peon-ping/"
                                        "adapters/codex.sh"
                                    )
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    def fake_run(*_args, **kwargs):
        assert kwargs["cwd"] == str(repository_root)
        return subprocess.CompletedProcess([], 0, ".git\n", "")

    monkeypatch.setattr(codex_hook_repair.subprocess, "run", fake_run)

    assert codex_hook_repair.main(
        [
            "--target",
            str(target),
            "--repository-root",
            str(repository_root),
        ]
    ) == 0
    repaired = json.loads(target.read_text(encoding="utf-8"))
    assert repaired["hooks"]["SessionStart"][0]["hooks"][0]["command"] == (
        f'python3 "{repository_root / "scripts/codex-hooks/run_peon_ping.py"}"'
    )
