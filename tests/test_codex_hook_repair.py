from __future__ import annotations

import json
from pathlib import Path

from agentic_pr_dash import codex_hook_repair


WRAPPER = "/host checkout/scripts/codex-hooks/run_peon_ping.py"
COMMAND = f'python3 "{WRAPPER}"'


def _config(command: str) -> dict:
    return {"hooks": {"SessionStart": [{"hooks": [{"command": command}]}]}}


def test_stale_direct_peon_ping_adapter_needs_repair() -> None:
    command = "bash ~/.claude/hooks/peon-ping/adapters/codex.sh"
    assert codex_hook_repair._needs_repair(command, COMMAND)


def test_quoted_adapter_path_with_spaces_needs_repair() -> None:
    command = "bash '/Users/name/My Repo/.claude/hooks/peon-ping/adapters/codex.sh'"
    assert codex_hook_repair._needs_repair(command, COMMAND)


def test_valid_primary_wrapper_invocations_are_preserved() -> None:
    commands = [
        f'/usr/bin/python3 "{WRAPPER}"',
        f'timeout 5 python3 "{WRAPPER}"',
        f'PEON_MODE=quiet python3 "{WRAPPER}" --best-effort',
    ]
    for command in commands:
        assert not codex_hook_repair._needs_repair(command, COMMAND)


def test_repair_uses_wrapper_supplied_by_host() -> None:
    repaired, count = codex_hook_repair._repair_config(
        _config("bash ~/.claude/hooks/peon-ping/adapters/codex.sh"), COMMAND
    )
    assert count == 1
    assert repaired["hooks"]["SessionStart"][0]["hooks"][0]["command"] == COMMAND


def test_main_fails_open_for_wrong_top_level_json_types(tmp_path: Path) -> None:
    for payload in (None, [], {"hooks": []}):
        target = tmp_path / "hooks.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        assert codex_hook_repair.main(["--target", str(target), "--wrapper", WRAPPER]) == 0


def test_main_requires_host_wrapper_without_inferring_package_checkout(tmp_path: Path, capsys) -> None:
    target = tmp_path / "hooks.json"
    target.write_text(json.dumps(_config("bash ~/.claude/hooks/peon-ping/adapters/codex.sh")))
    assert codex_hook_repair.main(["--target", str(target)]) == 0
    assert "--wrapper" in capsys.readouterr().err
    assert "codex.sh" in target.read_text()


def test_main_replaces_atomically(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "hooks.json"
    target.write_text(json.dumps(_config("bash ~/.claude/hooks/peon-ping/adapters/codex.sh")))
    replaced: list[tuple[Path, Path]] = []
    real_replace = codex_hook_repair.os.replace

    def replace(source, destination):
        replaced.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(codex_hook_repair.os, "replace", replace)
    assert codex_hook_repair.main(["--target", str(target), "--wrapper", WRAPPER]) == 0
    assert replaced and replaced[0][1] == target
    assert json.loads(target.read_text())["hooks"]["SessionStart"][0]["hooks"][0]["command"] == COMMAND
