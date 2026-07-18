"""Tests for agentic_pr_dash.codex_hooks.run_bash_policy."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

import pytest

from agentic_pr_dash.codex_hooks import run_bash_policy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bash_payload(command: str, cwd: str = "/repo") -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def _run_main(monkeypatch, payload: dict, *, env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run run_bash_policy.main() with a fake stdin.  Returns (rc, captured_stdout)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.delenv("AGENTIC_PR_DASH_BASH_POLICY_SHARED_HOOKS", raising=False)
    monkeypatch.delenv("AGENTIC_PR_DASH_BASH_POLICY_COMMIT_HOOKS", raising=False)
    monkeypatch.delenv("AGENTIC_PR_DASH_BASH_POLICY_HOOK_BASE_DIR", raising=False)
    monkeypatch.delenv("CODEX_BASH_POLICY_HOOK_BASE_DIR", raising=False)
    if env:
        for k, v in env.items():
            monkeypatch.setenv(k, v)

    captured: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: captured.append(
        " ".join(str(a) for a in args)
    ))

    rc = run_bash_policy.main()
    return rc, "\n".join(captured)


# ---------------------------------------------------------------------------
# Generic AGENTS.md guard (always active, repo-agnostic)
# ---------------------------------------------------------------------------


def test_find_parent_agents_md_is_blocked(monkeypatch):
    """``find .. -name AGENTS.md`` is blocked regardless of whether a roster is injected."""
    rc, out = _run_main(monkeypatch, _make_bash_payload("find .. -name AGENTS.md"))
    assert rc == 0
    decoded = json.loads(out)
    assert decoded["decision"] == "block"
    assert "AGENTS.md" in decoded["reason"]


def test_find_parent_with_options_is_blocked(monkeypatch):
    """Extra find options don't bypass the AGENTS.md sibling-search guard."""
    rc, out = _run_main(monkeypatch, _make_bash_payload("find ../ -maxdepth 2 -name AGENTS.md"))
    decoded = json.loads(out)
    assert decoded["decision"] == "block"


def test_find_in_current_dir_is_allowed(monkeypatch):
    """``find . -name AGENTS.md`` (current dir) must NOT be blocked."""
    rc, out = _run_main(monkeypatch, _make_bash_payload("find . -name AGENTS.md"))
    assert rc == 0
    assert out == ""


# ---------------------------------------------------------------------------
# BOU-1575 regression: non-gaia checkout with no roster injected
# ---------------------------------------------------------------------------


def test_no_roster_pytest_command_is_allowed(monkeypatch):
    """BOU-1575: with no hook roster injected, a pytest command is allowed.

    This is the critical regression guard: in a non-gaia checkout the engine
    carries no gaia-specific policy scripts and must not block normal commands.
    """
    rc, out = _run_main(monkeypatch, _make_bash_payload("pytest tests/ -q"))
    assert rc == 0
    assert out == ""


def test_no_roster_git_commit_is_allowed(monkeypatch):
    """BOU-1575: git commit with no roster injected → allowed (no commit hooks either)."""
    rc, out = _run_main(monkeypatch, _make_bash_payload("git commit -m 'fix'"))
    assert rc == 0
    assert out == ""


def test_no_roster_arbitrary_command_is_allowed(monkeypatch):
    """No roster → only generic guard runs; arbitrary commands pass through."""
    rc, out = _run_main(monkeypatch, _make_bash_payload("echo hello world"))
    assert rc == 0
    assert out == ""


# ---------------------------------------------------------------------------
# Non-Bash tool passes through
# ---------------------------------------------------------------------------


def test_non_bash_tool_passes_through(monkeypatch):
    payload = {"tool_name": "Read", "tool_input": {"path": "/etc/hosts"}, "cwd": "/repo"}
    rc, out = _run_main(monkeypatch, payload)
    assert rc == 0
    assert out == ""


# ---------------------------------------------------------------------------
# Roster injection via env vars
# ---------------------------------------------------------------------------


def test_shared_hook_runs_for_all_commands(monkeypatch, tmp_path):
    """A shared hook injected via env runs for every Bash command."""
    # Write a fake hook that blocks with a sentinel reason
    hook = tmp_path / "my-hook.py"
    hook.write_text(
        "import json, sys\n"
        "print(json.dumps({'decision': 'block', 'reason': 'sentinel-block'}))\n"
        "sys.exit(1)\n"
    )
    roster = json.dumps([["my_hook", "my-hook.py"]])
    env = {
        "AGENTIC_PR_DASH_BASH_POLICY_SHARED_HOOKS": roster,
        "AGENTIC_PR_DASH_BASH_POLICY_HOOK_BASE_DIR": str(tmp_path),
    }
    rc, out = _run_main(monkeypatch, _make_bash_payload("ls -la"), env=env)
    assert rc != 0 or "sentinel-block" in out


def test_shared_hook_short_circuits_on_first_nonzero(monkeypatch, tmp_path):
    """When the first hook blocks, the second hook must NOT run."""
    hook1 = tmp_path / "hook1.py"
    hook1.write_text("import sys\nprint('hook1-ran')\nsys.exit(1)\n")
    hook2 = tmp_path / "hook2.py"
    hook2.write_text("import sys\nprint('hook2-ran')\nsys.exit(0)\n")
    roster = json.dumps([["h1", "hook1.py"], ["h2", "hook2.py"]])
    env = {
        "AGENTIC_PR_DASH_BASH_POLICY_SHARED_HOOKS": roster,
        "AGENTIC_PR_DASH_BASH_POLICY_HOOK_BASE_DIR": str(tmp_path),
    }
    # We can't easily monkeypatch print AND subprocess.run simultaneously, so
    # use the run() API directly for this test.
    payload_text = json.dumps(_make_bash_payload("ls"))
    ran: list[str] = []

    def fake_run_script(path: str, text: str, *, base: Path) -> int:
        ran.append(path)
        return 1 if path == "hook1.py" else 0

    import agentic_pr_dash.codex_hooks.run_bash_policy as mod
    original = mod.run_script
    mod.run_script = fake_run_script
    try:
        rc = run_bash_policy.run(
            [("h1", "hook1.py"), ("h2", "hook2.py")],
            [],
            base_dir=tmp_path,
            payload_text=payload_text,
            command="ls",
        )
    finally:
        mod.run_script = original

    assert rc == 1
    assert ran == ["hook1.py"]  # hook2 never ran


def test_commit_hook_runs_only_for_git_commit(monkeypatch, tmp_path):
    """Commit-only hooks run for git commit but not for other Bash commands."""
    hook = tmp_path / "commit-hook.py"
    hook.write_text("import sys\nprint('commit-hook-ran')\nsys.exit(0)\n")
    commit_roster = json.dumps([["ch", "commit-hook.py"]])
    env = {
        "AGENTIC_PR_DASH_BASH_POLICY_COMMIT_HOOKS": commit_roster,
        "AGENTIC_PR_DASH_BASH_POLICY_HOOK_BASE_DIR": str(tmp_path),
    }

    ran: list[str] = []

    import agentic_pr_dash.codex_hooks.run_bash_policy as mod
    original = mod.run_script
    mod.run_script = lambda path, text, *, base: ran.append(path) or 0
    try:
        # Non-commit command → commit hook must NOT run
        payload_text = json.dumps(_make_bash_payload("pytest tests/"))
        run_bash_policy.run([], [("ch", "commit-hook.py")], base_dir=tmp_path,
                            payload_text=payload_text, command="pytest tests/")
        assert ran == []

        # git commit command → commit hook MUST run
        payload_text2 = json.dumps(_make_bash_payload("git commit -m wip"))
        run_bash_policy.run([], [("ch", "commit-hook.py")], base_dir=tmp_path,
                            payload_text=payload_text2, command="git commit -m wip")
        assert ran == ["commit-hook.py"]
    finally:
        mod.run_script = original


def test_commit_hook_runs_for_absolute_path_git_commit(tmp_path):
    """`/usr/bin/git commit` dispatches commit-only hooks (BOU-2147).

    The detector matches the git token by basename, so a path-qualified git
    cannot evade COMMIT_ONLY_HOOKS dispatch — while a path-qualified non-commit
    git command still skips them.
    """
    ran: list[str] = []

    import agentic_pr_dash.codex_hooks.run_bash_policy as mod
    original = mod.run_script
    mod.run_script = lambda path, text, *, base: ran.append(path) or 0
    try:
        # Absolute-path non-commit git → commit hook must NOT run
        cmd_status = "/usr/bin/git status"
        run_bash_policy.run([], [("ch", "commit-hook.py")], base_dir=tmp_path,
                            payload_text=json.dumps(_make_bash_payload(cmd_status)),
                            command=cmd_status)
        assert ran == []

        # Absolute-path git commit → commit hook MUST run
        cmd_commit = "/usr/bin/git commit -m x"
        run_bash_policy.run([], [("ch", "commit-hook.py")], base_dir=tmp_path,
                            payload_text=json.dumps(_make_bash_payload(cmd_commit)),
                            command=cmd_commit)
        assert ran == ["commit-hook.py"]

        # Homebrew-path git commit → commit hook MUST run too
        ran.clear()
        cmd_brew = "/opt/homebrew/bin/git commit -m x"
        run_bash_policy.run([], [("ch", "commit-hook.py")], base_dir=tmp_path,
                            payload_text=json.dumps(_make_bash_payload(cmd_brew)),
                            command=cmd_brew)
        assert ran == ["commit-hook.py"]
    finally:
        mod.run_script = original


# ---------------------------------------------------------------------------
# Legacy env alias for hook base dir
# ---------------------------------------------------------------------------


def test_legacy_codex_base_dir_env_is_honoured(monkeypatch, tmp_path):
    """CODEX_BASH_POLICY_HOOK_BASE_DIR is accepted as an alias."""
    monkeypatch.setenv("CODEX_BASH_POLICY_HOOK_BASE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENTIC_PR_DASH_BASH_POLICY_HOOK_BASE_DIR", raising=False)
    assert run_bash_policy.hook_base_dir() == tmp_path


def test_preferred_env_beats_legacy_env(monkeypatch, tmp_path):
    """AGENTIC_PR_DASH_BASH_POLICY_HOOK_BASE_DIR wins over the legacy alias."""
    preferred = tmp_path / "preferred"
    legacy = tmp_path / "legacy"
    monkeypatch.setenv("AGENTIC_PR_DASH_BASH_POLICY_HOOK_BASE_DIR", str(preferred))
    monkeypatch.setenv("CODEX_BASH_POLICY_HOOK_BASE_DIR", str(legacy))
    assert run_bash_policy.hook_base_dir() == preferred


# ---------------------------------------------------------------------------
# is_git_commit_command — env-prefix / flag tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cmd,expected", [
    ("git commit -m 'fix'", True),
    ("git commit --amend", True),
    ("GIT_DIR=.git git commit -m x", True),
    ("env GIT_DIR=.git git commit -m x", True),
    ("git -C subdir commit -m x", True),
    ("git push", False),
    ("pytest tests/", False),
    ("", False),
    ("git", False),
    ("git status", False),
    # BOU-2147: path-qualified git matches by basename — an absolute-path git
    # must not evade commit detection …
    ("/usr/bin/git commit -m x", True),
    ("/opt/homebrew/bin/git commit -m x", True),
    ("../bin/git commit -m x", True),
    ("/usr/bin/git -C subdir commit -m x", True),
    ("env GIT_DIR=.git /usr/bin/git commit -m x", True),
    # … while non-commit git and non-git lookalikes stay untouched.
    ("/usr/bin/git status", False),
    ("/usr/bin/git push", False),
    ("/usr/bin/gitx commit -m x", False),
    ("mygit commit -m x", False),
])
def test_is_git_commit_command(cmd, expected):
    assert run_bash_policy.is_git_commit_command(cmd) == expected


def test_is_git_commit_env_prefix_key_equals_val(monkeypatch):
    """``KEY=val git commit`` (no ``env``) is detected as a commit."""
    assert run_bash_policy.is_git_commit_command("GIT_AUTHOR_NAME=foo git commit -m x")


# ---------------------------------------------------------------------------
# run() Python API — behavior_enabled injection
# ---------------------------------------------------------------------------


def test_run_behavior_enabled_skips_disabled_hooks(tmp_path):
    """A hook whose name returns False from behavior_enabled is skipped."""
    ran: list[str] = []

    import agentic_pr_dash.codex_hooks.run_bash_policy as mod
    original = mod.run_script
    mod.run_script = lambda path, text, *, base: ran.append(path) or 0
    try:
        run_bash_policy.run(
            [("disabled_hook", "hook.py"), ("enabled_hook", "hook2.py")],
            [],
            base_dir=tmp_path,
            behavior_enabled=lambda name: name == "enabled_hook",
            payload_text="{}",
            command="ls",
        )
    finally:
        mod.run_script = original

    assert ran == ["hook2.py"]
    assert "hook.py" not in ran


# ---------------------------------------------------------------------------
# _block_reason_for_command direct unit tests
# ---------------------------------------------------------------------------


def test_block_reason_for_non_string_is_none():
    assert run_bash_policy._block_reason_for_command(None) is None
    assert run_bash_policy._block_reason_for_command(42) is None


def test_block_reason_for_agents_md_search():
    reason = run_bash_policy._block_reason_for_command("find ../ -name AGENTS.md")
    assert reason is not None
    assert "AGENTS.md" in reason


def test_block_reason_for_normal_command_is_none():
    assert run_bash_policy._block_reason_for_command("pytest tests/ -q") is None
    assert run_bash_policy._block_reason_for_command("ls -la") is None
