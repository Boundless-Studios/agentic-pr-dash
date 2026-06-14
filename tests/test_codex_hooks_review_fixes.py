"""Regression tests for the BOU-1671 Codex review findings on PR #28.

Finding 1: resolve_warden_hook resolves repo-local fallbacks against payload cwd.
Finding 2: chained git commit (cd x && git commit) still runs commit hooks.
Finding 3: a stdout {"decision":"block"} (exit 0) short-circuits later hooks.
Finding 4: deeper parent AGENTS.md searches (find ../.. ...) are blocked.
Finding 5: session_id (and other passthrough metadata) survives normalization.
"""
from __future__ import annotations

import json
from pathlib import Path

from agentic_pr_dash.codex_hooks import _payload, run_bash_policy, run_warden


# ---------------------------------------------------------------------------
# Finding 1: resolve_warden_hook uses payload cwd for repo-local fallbacks
# ---------------------------------------------------------------------------


def test_resolve_warden_hook_finds_repo_local_venv_via_payload_cwd(monkeypatch, tmp_path):
    """A warden-hook under <payload-cwd>/.venv/bin is found when process cwd differs."""
    repo = tmp_path / "worktree"
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    hook = venv_bin / "warden-hook"
    hook.write_text("#!/bin/sh\n")
    hook.chmod(0o755)

    # No env candidate, nothing on PATH, process cwd is elsewhere.
    monkeypatch.delenv("WARDEN_HOOK_BIN", raising=False)
    monkeypatch.delenv("WARDEN_HOOK_PATH", raising=False)
    monkeypatch.setattr(run_warden.shutil, "which", lambda _name: None)
    monkeypatch.chdir(tmp_path)  # process cwd != repo

    # Without payload → resolves against process cwd, misses the worktree hook.
    assert run_warden.resolve_warden_hook() != str(hook)
    # With payload cwd → finds the repo-local hook.
    assert run_warden.resolve_warden_hook({"cwd": str(repo)}) == str(hook)


# ---------------------------------------------------------------------------
# Finding 2: chained git commit still runs commit hooks
# ---------------------------------------------------------------------------


def test_chained_git_commit_runs_commit_hooks(tmp_path):
    ran: list[str] = []
    original = run_bash_policy.run_script
    run_bash_policy.run_script = lambda path, text, *, base: ran.append(path) or 0
    try:
        run_bash_policy.run(
            [], [("ch", "commit-hook.py")], base_dir=tmp_path,
            payload_text="{}", command="cd subdir && git commit -m wip",
        )
    finally:
        run_bash_policy.run_script = original
    assert ran == ["commit-hook.py"]


def test_space_free_chained_git_commit_detected():
    assert run_bash_policy.is_git_commit_command("cd x&&git commit -m y")


def test_quoted_separator_in_commit_message_does_not_oversplit():
    # The && is inside the quoted message; still a single commit invocation.
    assert run_bash_policy.is_git_commit_command("git commit -m 'a && b'")


def test_non_commit_chain_not_detected():
    assert not run_bash_policy.is_git_commit_command("git push && make")


# ---------------------------------------------------------------------------
# Finding 3: stdout block decision short-circuits later hooks
# ---------------------------------------------------------------------------


def test_stdout_block_short_circuits(tmp_path):
    """A hook emitting {"decision":"block"} with exit 0 stops later hooks."""
    blocker = tmp_path / "blocker.py"
    blocker.write_text(
        "import json\n"
        "print(json.dumps({'decision': 'block', 'reason': 'no'}))\n"
        # exit 0 — the block is signalled via stdout only
    )
    later = tmp_path / "later.py"
    later.write_text("print('SHOULD-NOT-RUN')\n")

    rc = run_bash_policy.run(
        [("blk", "blocker.py"), ("later", "later.py")],
        [], base_dir=tmp_path, payload_text="{}", command="ls",
    )
    assert rc != 0  # block surfaced as non-zero so run() short-circuits


def test_run_script_returns_block_rc_on_stdout_block(tmp_path, capsys):
    blocker = tmp_path / "blk.py"
    blocker.write_text(
        "import json\nprint(json.dumps({'decision': 'block', 'reason': 'x'}))\n"
    )
    rc = run_bash_policy.run_script("blk.py", "{}", base=tmp_path)
    assert rc == run_bash_policy._STDOUT_BLOCK_RC


def test_run_script_allows_plain_stdout(tmp_path):
    ok = tmp_path / "ok.py"
    ok.write_text("print('all good')\n")
    rc = run_bash_policy.run_script("ok.py", "{}", base=tmp_path)
    assert rc == 0


# ---------------------------------------------------------------------------
# Finding 4: deeper parent AGENTS.md searches are blocked
# ---------------------------------------------------------------------------


def test_deeper_parent_agents_search_blocked():
    assert run_bash_policy._block_reason_for_command("find ../.. -name AGENTS.md") is not None


def test_deeper_parent_path_agents_search_blocked():
    reason = run_bash_policy._block_reason_for_command("find ../../foo -name AGENTS.md")
    assert reason is not None


def test_current_dir_agents_search_still_allowed():
    assert run_bash_policy._block_reason_for_command("find . -name AGENTS.md") is None


# ---------------------------------------------------------------------------
# Finding 5: session metadata survives normalization
# ---------------------------------------------------------------------------


def test_session_id_preserved_in_normalized_payload():
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "cwd": "/repo",
        "session_id": "sess-123",
    }
    out = _payload.normalized_payload(payload)
    assert out["session_id"] == "sess-123"


def test_normalized_payload_omits_absent_metadata():
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/repo"}
    out = _payload.normalized_payload(payload)
    assert "session_id" not in out
