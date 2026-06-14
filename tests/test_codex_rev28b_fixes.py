"""Regression tests for the second Codex re-review on PR #28 (commit 1c088b2).

These cover 8 P2 findings on the warden / bash-policy upstream hook engine:

1. probe trust-root resolves from the checkout root, not the payload exec dir.
2. repo-local warden fallbacks search from the actual checkout root.
3. a literal newline is a top-level command separator for commit detection.
4. valid no-argument git globals (``--no-pager``/``-P``/``-p``/``--paginate``).
5. AGENTS.md parent-tree guard scans each top-level shell segment.
6. ``find`` global options before the parent path don't bypass the guard.
7. ``hookSpecificOutput.permissionDecision:"deny"`` counts as a stdout block.
8. the stdout-JSON block path stays exit-0 (distinct from exit-2/stderr).
"""
from __future__ import annotations

import json
from pathlib import Path

from agentic_pr_dash.codex_hooks import run_bash_policy, run_warden


# ---------------------------------------------------------------------------
# Finding 1: probe trust-root resolved from the checkout root
# ---------------------------------------------------------------------------


def _make_git_checkout(root: Path) -> None:
    (root / ".git").mkdir(parents=True)


def _make_probe(dir_path: Path, name: str = "playwright") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    probe = dir_path / name
    probe.write_text("#!/bin/sh\n")
    probe.chmod(0o755)
    return probe


def test_probe_in_subdir_node_modules_is_not_trusted(tmp_path):
    """A planted probe in <repo>/tmp/node_modules/.bin run from /repo/tmp is NOT trusted."""
    repo = tmp_path / "repo"
    _make_git_checkout(repo)
    subdir = repo / "tmp"
    _make_probe(subdir / "node_modules" / ".bin")
    cmd = "node_modules/.bin/playwright --version"
    # cwd is the writable subdir, but the checkout root is <repo>, so the probe
    # resolves to <repo>/tmp/node_modules/.bin/playwright which is NOT the
    # repo-root probe dir.
    assert run_warden._probe_resolves_under_repo(cmd, str(subdir)) is False


def test_probe_at_repo_root_is_trusted_from_subdir(tmp_path):
    """The genuine repo-root probe resolves under the checkout root even from a subdir."""
    repo = tmp_path / "repo"
    _make_git_checkout(repo)
    _make_probe(repo / "node_modules" / ".bin")
    subdir = repo / "tmp"
    subdir.mkdir(parents=True)
    cmd = "node_modules/.bin/playwright --version"
    # From the checkout root the probe exists and stays inside the root.
    assert run_warden._probe_resolves_under_repo(cmd, str(repo)) is True


def test_find_checkout_root_walks_up_to_git_marker(tmp_path):
    repo = tmp_path / "repo"
    _make_git_checkout(repo)
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    assert run_warden._find_checkout_root(nested) == repo.resolve()


# ---------------------------------------------------------------------------
# Finding 2: repo-local warden fallbacks search from the checkout root
# ---------------------------------------------------------------------------


def test_resolve_warden_hook_finds_root_venv_when_cwd_is_subdir(monkeypatch, tmp_path):
    """warden-hook at <checkout-root>/.venv/bin is found when payload cwd is a subdir."""
    repo = tmp_path / "repo"
    _make_git_checkout(repo)
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    hook = venv_bin / "warden-hook"
    hook.write_text("#!/bin/sh\n")
    hook.chmod(0o755)
    subdir = repo / "pkg"
    subdir.mkdir(parents=True)

    monkeypatch.delenv("WARDEN_HOOK_BIN", raising=False)
    monkeypatch.delenv("WARDEN_HOOK_PATH", raising=False)
    monkeypatch.setattr(run_warden.shutil, "which", lambda _name: None)

    payload = {"cwd": str(subdir)}
    resolved = run_warden.resolve_warden_hook(payload)
    assert resolved == str(hook)


# ---------------------------------------------------------------------------
# Finding 3: literal newline is a top-level command separator
# ---------------------------------------------------------------------------


def test_newline_chained_git_commit_is_detected():
    assert run_bash_policy.is_git_commit_command("make\ngit commit -m x") is True


def test_newline_segments_split():
    segs = run_bash_policy._split_top_level_segments("make\ngit commit -m x")
    assert "git commit -m x" in segs


# ---------------------------------------------------------------------------
# Finding 4: valid no-argument git globals before the subcommand
# ---------------------------------------------------------------------------


def test_git_no_pager_commit_is_detected():
    assert run_bash_policy.is_git_commit_command("git --no-pager commit -m x") is True


def test_git_paginate_short_flags_commit_is_detected():
    assert run_bash_policy.is_git_commit_command("git -P commit -m x") is True
    assert run_bash_policy.is_git_commit_command("git -p commit -m x") is True
    assert run_bash_policy.is_git_commit_command("git --paginate commit -m x") is True


def test_git_unknown_flag_before_subcommand_still_rejected():
    # A flag we deliberately do not model (and that takes no recognized form)
    # should not be mistaken for a commit invocation.
    assert run_bash_policy.is_git_commit_command("git --totally-unknown-flag status") is False


# ---------------------------------------------------------------------------
# Finding 5: AGENTS.md guard scans each top-level shell segment
# ---------------------------------------------------------------------------


def test_agents_search_chained_after_prefix_is_blocked():
    assert run_bash_policy._block_reason_for_command("cd . && find ../.. -name AGENTS.md") is not None
    assert run_bash_policy._block_reason_for_command("echo ok; find .. -name AGENTS.md") is not None


def test_agents_search_alone_still_blocked():
    assert run_bash_policy._block_reason_for_command("find .. -name AGENTS.md") is not None


def test_benign_find_not_blocked():
    assert run_bash_policy._block_reason_for_command("find . -name AGENTS.md") is None


# ---------------------------------------------------------------------------
# Finding 6: find global options before the parent path
# ---------------------------------------------------------------------------


def test_find_with_dash_L_option_before_parent_is_blocked():
    assert run_bash_policy._block_reason_for_command("find -L ../.. -name AGENTS.md") is not None


def test_find_with_O_level_option_before_parent_is_blocked():
    assert run_bash_policy._block_reason_for_command("find -O2 .. -name AGENTS.md") is not None


def test_find_with_HLP_combined_options_blocked():
    assert run_bash_policy._block_reason_for_command("find -LP ../.. -name AGENTS.md") is not None


# ---------------------------------------------------------------------------
# Finding 7: hookSpecificOutput.permissionDecision:"deny" counts as a block
# ---------------------------------------------------------------------------


def test_stdout_is_block_recognizes_permission_deny():
    payload = json.dumps(
        {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}}
    )
    assert run_bash_policy._stdout_is_block(payload) is True


def test_stdout_is_block_still_recognizes_legacy_block():
    assert run_bash_policy._stdout_is_block(json.dumps({"decision": "block"})) is True


def test_stdout_is_block_allow_is_not_block():
    payload = json.dumps(
        {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
    )
    assert run_bash_policy._stdout_is_block(payload) is False


# ---------------------------------------------------------------------------
# Finding 8: stdout-JSON block path stays exit-0 (distinct from exit-2/stderr)
# ---------------------------------------------------------------------------


def test_run_script_signals_stdout_block_sentinel(monkeypatch, tmp_path, capsys):
    """A child emitting block JSON on stdout with exit 0 returns the sentinel."""
    hook = tmp_path / "blocker.py"
    hook.write_text(
        "import json,sys\n"
        "print(json.dumps({'decision':'block','reason':'nope'}))\n"
        "sys.exit(0)\n"
    )
    rc = run_bash_policy.run_script("blocker.py", "{}", base=tmp_path)
    assert rc == run_bash_policy._STDOUT_BLOCK_RC
    out = capsys.readouterr().out
    assert json.loads(out.strip())["decision"] == "block"


def test_run_maps_stdout_block_to_exit_zero(monkeypatch, tmp_path):
    """run() short-circuits on a stdout-JSON block but returns exit 0, keeping JSON path."""
    # First shared hook blocks via stdout JSON; second must not run.
    calls: list[str] = []

    def _run_script(path, text, *, base):
        calls.append(path)
        if path == "first.py":
            return run_bash_policy._STDOUT_BLOCK_RC
        return 0

    monkeypatch.setattr(run_bash_policy, "run_script", _run_script)
    rc = run_bash_policy.run(
        [("first", "first.py"), ("second", "second.py")],
        [],
        base_dir=tmp_path,
        payload_text="{}",
        command="echo hi",
    )
    assert rc == 0
    assert calls == ["first.py"]  # later hook short-circuited


def test_run_propagates_real_nonzero_exit(monkeypatch, tmp_path):
    """A genuine non-zero child exit (exit-2/stderr path) is still propagated."""
    monkeypatch.setattr(
        run_bash_policy, "run_script",
        lambda path, text, *, base: 2,
    )
    rc = run_bash_policy.run(
        [("first", "first.py")],
        [],
        base_dir=tmp_path,
        payload_text="{}",
        command="echo hi",
    )
    assert rc == 2
