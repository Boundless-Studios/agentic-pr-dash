"""Tests for the Codex runner framework (agentic_pr_dash.codex_runtime, BOU-1672)."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from agentic_pr_dash import codex_runtime
from agentic_pr_dash.codex_runtime import payload as payload_mod
from agentic_pr_dash.codex_runtime import runners as runners_mod
from agentic_pr_dash.codex_runtime import settings as settings_mod


# --------------------------------------------------------------------------- #
# payload normalization
# --------------------------------------------------------------------------- #
def _root(p: Path):
    return lambda: p


def test_exec_command_maps_to_bash_and_cmd_to_command(tmp_path):
    payload = {
        "tool_name": "exec_command",
        "tool_input": {"cmd": "git push", "workdir": "/wt"},
    }
    norm = payload_mod.normalized_payload(payload, repo_root=_root(tmp_path))
    assert norm["tool_name"] == "Bash"
    assert norm["tool_input"] == {"command": "git push"}
    assert norm["cwd"] == "/wt"


def test_namespaced_exec_command_maps_to_bash(tmp_path):
    payload = {"tool_name": "functions.exec_command", "tool_input": {"cmd": "ls"}}
    norm = payload_mod.normalized_payload(payload, repo_root=_root(tmp_path))
    assert norm["tool_name"] == "Bash"
    assert norm["tool_input"] == {"command": "ls"}


def test_non_dict_tool_input_collapses_to_empty_dict(tmp_path):
    payload = {"tool_name": "Bash", "tool_input": "not-a-dict"}
    assert payload_mod.normalized_tool_input(payload) == {}


def test_normalized_cwd_prefers_workdir_then_cwd_then_repo_root(tmp_path):
    assert (
        payload_mod.normalized_cwd(
            {"tool_input": {"workdir": "/a"}, "cwd": "/b"}, repo_root=_root(tmp_path)
        )
        == "/a"
    )
    assert payload_mod.normalized_cwd({"cwd": "/b"}, repo_root=_root(tmp_path)) == "/b"
    assert payload_mod.normalized_cwd({}, repo_root=_root(tmp_path)) == str(tmp_path)


def test_apply_shared_env_seeds_project_dirs(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("GAIA_PROJECT_DIR", raising=False)
    payload_mod.apply_shared_env({}, repo_root=_root(tmp_path))
    assert os.environ["CLAUDE_PROJECT_DIR"] == str(tmp_path)
    assert os.environ["GAIA_PROJECT_DIR"] == str(tmp_path)


def test_load_payload_rejects_non_dict(monkeypatch):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps([1, 2])))
    with pytest.raises(SystemExit):
        payload_mod.load_payload()


# --------------------------------------------------------------------------- #
# upstream-hook loader
# --------------------------------------------------------------------------- #
def test_load_upstream_hook_main_absent_returns_none(monkeypatch):
    def fake_import(name):
        raise ModuleNotFoundError(name="agentic_pr_dash.codex_hooks", path=None)

    monkeypatch.setattr(payload_mod.importlib, "import_module", fake_import)
    assert payload_mod.load_upstream_hook_main("nope") is None


def test_load_upstream_hook_main_propagates_broken_sibling_import(monkeypatch):
    def fake_import(name):
        raise ModuleNotFoundError("No module named 'some_sibling'", name="some_sibling")

    monkeypatch.setattr(payload_mod.importlib, "import_module", fake_import)
    with pytest.raises(ModuleNotFoundError):
        payload_mod.load_upstream_hook_main("run_arm_pr_watch")


def test_load_upstream_hook_main_missing_main_raises(monkeypatch):
    incomplete = types.ModuleType("agentic_pr_dash.codex_hooks.x")
    monkeypatch.setattr(payload_mod.importlib, "import_module", lambda _n: incomplete)
    with pytest.raises(AttributeError):
        payload_mod.load_upstream_hook_main("x")


def test_load_upstream_hook_main_returns_callable(monkeypatch):
    mod = types.ModuleType("agentic_pr_dash.codex_hooks.x")
    mod.main = lambda: 0  # type: ignore[attr-defined]
    monkeypatch.setattr(payload_mod.importlib, "import_module", lambda _n: mod)
    assert payload_mod.load_upstream_hook_main("x") is mod.main


# --------------------------------------------------------------------------- #
# settings resolution
# --------------------------------------------------------------------------- #
def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_local_overrides_default_then_profile_then_env(tmp_path, monkeypatch):
    defaults = tmp_path / "defaults.json"
    local = tmp_path / "local.json"
    _write(
        defaults,
        {
            "hooks": {"a": True, "b": True, "c": True},
            "profiles": {"active": "strict", "available": {"strict": {"b": False}}},
        },
    )
    _write(local, {"hooks": {"a": False}})

    resolver = settings_mod.SettingsResolver(defaults_path=defaults, local_path=local)
    # local overrides default
    assert resolver.hook_enabled("a") is False
    # active profile overrides
    assert resolver.hook_enabled("b") is False
    # untouched stays default
    assert resolver.hook_enabled("c") is True
    # env override wins last
    monkeypatch.setenv("CODEX_HOOK_C", "off")
    assert resolver.hook_enabled("c") is False


def test_hook_enabled_unknown_returns_default(tmp_path):
    defaults = tmp_path / "d.json"
    local = tmp_path / "l.json"
    _write(defaults, {"hooks": {}})
    assert settings_mod.hook_enabled(
        "missing", default=True, defaults_path=defaults, local_path=local
    )
    assert not settings_mod.hook_enabled(
        "missing", default=False, defaults_path=defaults, local_path=local
    )


def test_settings_cli_enable_disable_roundtrip(tmp_path, capsys):
    defaults = tmp_path / "d.json"
    local = tmp_path / "l.json"
    _write(defaults, {"hooks": {"x": True}})

    rc = settings_mod.main(
        ["disable", "x"], default_defaults_path=defaults, default_local_path=local
    )
    assert rc == 0
    assert json.loads(local.read_text())["hooks"]["x"] is False

    rc = settings_mod.main(
        ["enable", "x"], default_defaults_path=defaults, default_local_path=local
    )
    assert rc == 0
    assert json.loads(local.read_text())["hooks"]["x"] is True


def test_settings_cli_unknown_profile_errors(tmp_path):
    defaults = tmp_path / "d.json"
    local = tmp_path / "l.json"
    _write(defaults, {"profiles": {"available": {"standard": {}}}})
    rc = settings_mod.main(
        ["profile", "nope"], default_defaults_path=defaults, default_local_path=local
    )
    assert rc == 2


# --------------------------------------------------------------------------- #
# runners: shared hook
# --------------------------------------------------------------------------- #
def test_run_shared_hook_skips_non_bash(tmp_path):
    rc = runners_mod.run_shared_hook(
        {"tool_name": "request_user_input"},
        normalize=lambda p: {"tool_name": "request_user_input"},
        apply_env=lambda p: None,
        target=str(tmp_path / "target.py"),
    )
    assert rc == 0


def test_run_shared_hook_missing_target_blocks(tmp_path):
    rc = runners_mod.run_shared_hook(
        {"tool_name": "Bash"},
        normalize=lambda p: {"tool_name": "Bash"},
        apply_env=lambda p: None,
        target=None,
    )
    assert rc == 2


def test_run_shared_hook_forwards_to_target(tmp_path):
    target = tmp_path / "target.py"
    target.write_text(
        "import sys, json\n"
        "data = json.load(sys.stdin)\n"
        "print('TN=' + data['tool_name'])\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    rc = runners_mod.run_shared_hook(
        {"tool_name": "exec_command", "tool_input": {"cmd": "ls"}},
        normalize=lambda p: payload_mod.normalized_payload(p, repo_root=_root(tmp_path)),
        apply_env=lambda p: None,
        target=str(target),
    )
    assert rc == 7


# --------------------------------------------------------------------------- #
# runners: session commands
# --------------------------------------------------------------------------- #
def test_run_session_commands_skips_disabled_and_runs_enabled(tmp_path):
    log = tmp_path / "log"
    cmd = [sys.executable, "-c", f"open({str(log)!r}, 'a').write('ran\\n')"]
    runners_mod.run_session_commands(
        [("on", cmd), ("off", cmd)],
        is_enabled=lambda name: name == "on",
    )
    assert log.read_text() == "ran\n"


def test_run_session_commands_best_effort_on_bad_command():
    # Non-existent binary must not raise.
    runners_mod.run_session_commands(
        [("x", ["/nonexistent/binary/zzz"])],
        is_enabled=lambda _n: True,
    )


# --------------------------------------------------------------------------- #
# runners: StopChecksRunner
# --------------------------------------------------------------------------- #
def _hook_script(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_stop_runner_runs_all_and_forwards_single_json(tmp_path, capsys):
    a = _hook_script(tmp_path, "a.py", "import json\nprint(json.dumps({'decision':'approve'}))\n")
    b = _hook_script(tmp_path, "b.py", "print('human text from b')\n")
    runner = runners_mod.StopChecksRunner(
        resolve_path=lambda x: x,
        is_enabled=lambda _n: True,
    )
    rc = runner.run(
        [("a", a), ("b", b)], payload_text="{}", hook_env=dict(os.environ)
    )
    out = capsys.readouterr()
    assert rc == 0
    # exactly one JSON on stdout
    assert out.out.strip() == json.dumps({"decision": "approve"})
    # human text routed to stderr
    assert "human text from b" in out.err


def test_stop_runner_blocks_when_any_hook_exits_2(tmp_path, capsys):
    a = _hook_script(tmp_path, "a.py", "raise SystemExit(0)\n")
    b = _hook_script(tmp_path, "b.py", "import sys\nprint('blocked', file=sys.stderr)\nraise SystemExit(2)\n")
    c = _hook_script(tmp_path, "c.py", "raise SystemExit(0)\n")
    ran = tmp_path / "ran"
    c2 = _hook_script(
        tmp_path, "c2.py", f"open({str(ran)!r},'a').write('c2')\nraise SystemExit(0)\n"
    )
    runner = runners_mod.StopChecksRunner(resolve_path=lambda x: x, is_enabled=lambda _n: True)
    rc = runner.run(
        [("a", a), ("b", b), ("c", c2)], payload_text="{}", hook_env=dict(os.environ)
    )
    assert rc == 2
    # later hook still ran (no break on first non-zero)
    assert ran.read_text() == "c2"


def test_stop_runner_skips_disabled_hooks(tmp_path):
    ran = tmp_path / "ran"
    h = _hook_script(tmp_path, "h.py", f"open({str(ran)!r},'a').write('x')\n")
    runner = runners_mod.StopChecksRunner(resolve_path=lambda x: x, is_enabled=lambda n: False)
    runner.run([("h", h)], payload_text="{}", hook_env=dict(os.environ))
    assert not ran.exists()


def test_stop_runner_first_nonzero_error_code_when_no_block(tmp_path):
    a = _hook_script(tmp_path, "a.py", "raise SystemExit(3)\n")
    runner = runners_mod.StopChecksRunner(resolve_path=lambda x: x, is_enabled=lambda _n: True)
    rc = runner.run([("a", a)], payload_text="{}", hook_env=dict(os.environ))
    assert rc == 3


def test_stop_runner_failure_report_captures_timeout_and_redacts_sensitive_values(
    monkeypatch,
):
    payload_text = json.dumps(
        {
            "session_id": "raw-stop-payload-secret",
            "nested": {"scalar": "scalar-only-leak"},
        }
    )
    hook_env = {
        "HOOK_SECRET": "environment-secret-value",
        "SHORT_SECRET": "xy",
    }
    timeout = subprocess.TimeoutExpired(
        cmd=["python3", "/repo/.claude/hooks/stop-example.py"],
        timeout=30,
        stderr=(
            "failed environment-secret-value xy scalar-only-leak "
            + payload_text
            + " "
            + "x" * (runners_mod.HOOK_FAILURE_OUTPUT_LIMIT * 2)
        ),
    )
    monkeypatch.setattr(
        runners_mod.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(timeout),
    )
    runner = runners_mod.StopChecksRunner(
        resolve_path=lambda path: f"/repo/{path}",
        is_enabled=lambda _name: True,
        python="python3",
    )

    rc = runner.run(
        [("stop_example", ".claude/hooks/stop-example.py")],
        payload_text=payload_text,
        hook_env=hook_env,
    )

    assert rc == 1
    failure = runner.last_report.failures[0]
    assert failure.hook_name == "stop_example"
    assert failure.hook_path == "/repo/.claude/hooks/stop-example.py"
    assert failure.failure_class == "timeout"
    assert failure.exit_code is None
    assert failure.timeout_seconds == 30
    assert failure.retry_argv == (
        "python3",
        "/repo/.claude/hooks/stop-example.py",
    )
    assert len(failure.stderr) <= runners_mod.HOOK_FAILURE_OUTPUT_LIMIT
    assert "environment-secret-value" not in failure.stderr
    assert "xy" not in failure.stderr
    assert "scalar-only-leak" not in failure.stderr
    assert payload_text not in failure.stderr


def test_stop_runner_failure_report_classifies_invalid_json_and_unexpected_exit(
    tmp_path,
):
    invalid = _hook_script(
        tmp_path,
        "invalid.py",
        "print('not-json')\nraise SystemExit(2)\n",
    )
    unexpected = _hook_script(
        tmp_path,
        "unexpected.py",
        "import sys\nprint('boom', file=sys.stderr)\nraise SystemExit(7)\n",
    )
    runner = runners_mod.StopChecksRunner(
        resolve_path=lambda path: path,
        is_enabled=lambda _name: True,
    )

    rc = runner.run(
        [("invalid", invalid), ("unexpected", unexpected)],
        payload_text="{}",
        hook_env=dict(os.environ),
    )

    assert rc == 2
    assert [failure.failure_class for failure in runner.last_report.failures] == [
        "invalid_json",
        "unexpected_exit",
    ]
    assert runner.last_report.failures[0].exit_code == 2
    assert runner.last_report.failures[1].exit_code == 7
    assert all(
        failure.timeout_seconds is None for failure in runner.last_report.failures
    )
    assert runner.last_report.final_rc == 2


def test_stop_runner_failure_report_captures_spawn_failure(monkeypatch):
    monkeypatch.setattr(
        runners_mod.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("cannot spawn")),
    )
    runner = runners_mod.StopChecksRunner(
        resolve_path=lambda path: f"/repo/{path}",
        is_enabled=lambda _name: True,
        python="python3",
    )

    assert (
        runner.run(
            [("stop_example", ".claude/hooks/stop-example.py")],
            payload_text="{}",
            hook_env={},
        )
        == 1
    )
    failure = runner.last_report.failures[0]
    assert failure.failure_class == "spawn_failure"
    assert failure.exit_code is None
    assert failure.timeout_seconds is None
    assert failure.stderr == "cannot spawn"


def test_human_output_buffer_dedups_and_condenses(capsys):
    buf = runners_mod.HumanOutputBuffer()
    buf.add("same")
    buf.add("same")  # duplicate
    buf.add("x\n" * 50)  # long chunk
    buf.emit()
    err = capsys.readouterr().err
    assert "condensed" in err
    assert "duplicate hook output omitted" in err
    assert "hook output line(s) omitted" in err


# --------------------------------------------------------------------------- #
# public surface
# --------------------------------------------------------------------------- #
def test_default_settings_fragment_ships_as_package_data():
    fragment = settings_mod.default_settings_fragment()
    assert isinstance(fragment, dict)
    assert "hooks" in fragment and isinstance(fragment["hooks"], dict)
    assert "profiles" in fragment
    profiles = fragment["profiles"]["available"]
    assert {"minimal", "standard", "strict"} <= set(profiles)


def test_default_settings_fragment_is_usable_as_defaults(tmp_path):
    defaults = tmp_path / "default-settings.json"
    defaults.write_text(json.dumps(settings_mod.default_settings_fragment()), encoding="utf-8")
    local = tmp_path / "local.json"
    # a hook present in the fragment resolves to its shipped default
    assert settings_mod.hook_enabled(
        "warden", default=False, defaults_path=defaults, local_path=local
    )


# --------------------------------------------------------------------------- #
# BOU-1672 Codex P2 review findings
# --------------------------------------------------------------------------- #
def test_normalized_payload_preserves_session_and_response_fields(tmp_path):
    """Finding 1: normalization must not drop session_id / tool_response.

    The arm hook reads session_id (ownership) and tool_response (skip after a
    failed command); rebuilding a 3-key dict would silently strip them.
    """
    payload = {
        "tool_name": "exec_command",
        "tool_input": {"cmd": "git push", "workdir": "/wt"},
        "session_id": "sess-abc",
        "tool_response": {"exit_code": 1, "stderr": "boom"},
        "hook_event_name": "PreToolUse",
    }
    norm = payload_mod.normalized_payload(payload, repo_root=_root(tmp_path))
    # Translated fields still normalized
    assert norm["tool_name"] == "Bash"
    assert norm["tool_input"] == {"command": "git push"}
    assert norm["cwd"] == "/wt"
    # Untranslated fields survive
    assert norm["session_id"] == "sess-abc"
    assert norm["tool_response"] == {"exit_code": 1, "stderr": "boom"}
    assert norm["hook_event_name"] == "PreToolUse"


def test_normalized_payload_does_not_mutate_input(tmp_path):
    payload = {"tool_name": "exec_command", "tool_input": {"cmd": "ls"}, "session_id": "s"}
    payload_mod.normalized_payload(payload, repo_root=_root(tmp_path))
    # original untouched
    assert payload["tool_name"] == "exec_command"
    assert payload["tool_input"] == {"cmd": "ls"}


def test_env_override_disables_default_enabled_unlisted_hook(tmp_path, monkeypatch):
    """Finding 2: CODEX_HOOK_<NAME>=off must disable an item absent from settings."""
    defaults = tmp_path / "default-settings.json"
    defaults.write_text("{}", encoding="utf-8")
    local = tmp_path / "local.json"
    monkeypatch.setenv("CODEX_HOOK_FOO", "off")
    assert (
        settings_mod.hook_enabled(
            "foo", default=True, defaults_path=defaults, local_path=local
        )
        is False
    )


def test_env_override_enables_default_disabled_unlisted_skill(tmp_path, monkeypatch):
    defaults = tmp_path / "default-settings.json"
    defaults.write_text("{}", encoding="utf-8")
    local = tmp_path / "local.json"
    monkeypatch.setenv("CODEX_SKILL_BAR", "on")
    assert (
        settings_mod.skill_enabled(
            "bar", default=False, defaults_path=defaults, local_path=local
        )
        is True
    )


def test_stop_runner_blocking_json_wins_over_earlier_success_json(tmp_path, capsys):
    """Finding 3: a later exit-2 JSON block must reach stdout, not the earlier OK JSON."""
    ok = _hook_script(
        tmp_path, "ok.py", "import json\nprint(json.dumps({'decision':'approve'}))\n"
    )
    block = _hook_script(
        tmp_path,
        "block.py",
        "import json, sys\nprint(json.dumps({'decision':'block','reason':'nope'}))\n"
        "raise SystemExit(2)\n",
    )
    runner = runners_mod.StopChecksRunner(resolve_path=lambda x: x, is_enabled=lambda _n: True)
    rc = runner.run(
        [("ok", ok), ("block", block)], payload_text="{}", hook_env=dict(os.environ)
    )
    out = capsys.readouterr()
    assert rc == 2
    # Authoritative stdout JSON is the block decision, not the earlier approval.
    assert json.loads(out.out.strip()) == {"decision": "block", "reason": "nope"}
    # The demoted approval JSON is routed to stderr.
    assert "approve" in out.err


def test_stop_runner_keeps_block_json_even_when_block_runs_first(tmp_path, capsys):
    """If the blocking hook runs first, a later success JSON must not displace it."""
    block = _hook_script(
        tmp_path,
        "block.py",
        "import json\nprint(json.dumps({'decision':'block'}))\nraise SystemExit(2)\n",
    )
    ok = _hook_script(
        tmp_path, "ok.py", "import json\nprint(json.dumps({'decision':'approve'}))\n"
    )
    runner = runners_mod.StopChecksRunner(resolve_path=lambda x: x, is_enabled=lambda _n: True)
    rc = runner.run(
        [("block", block), ("ok", ok)], payload_text="{}", hook_env=dict(os.environ)
    )
    out = capsys.readouterr()
    assert rc == 2
    assert json.loads(out.out.strip()) == {"decision": "block"}
    assert "approve" in out.err


def test_package_reexports_public_symbols():
    for name in (
        "normalized_payload",
        "apply_shared_env",
        "load_payload",
        "load_upstream_hook_main",
        "hook_enabled",
        "skill_enabled",
        "load_settings",
        "SettingsResolver",
        "StopChecksRunner",
        "HOOK_FAILURE_OUTPUT_LIMIT",
        "HookFailure",
        "StopChecksReport",
        "run_shared_hook",
        "run_session_commands",
        "HumanOutputBuffer",
    ):
        assert hasattr(codex_runtime, name), name
