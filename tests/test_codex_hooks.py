import io
import json
import os
import sys
from pathlib import Path

from agentic_pr_dash.codex_hooks import run_arm_pr_watch


def _run_arm_hook(
    monkeypatch,
    payload: dict,
    *,
    argv: list[str],
    env: dict[str, str] | None = None,
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_main(args):
        calls.append(args)
        return 0

    monkeypatch.setattr(run_arm_pr_watch.maintenance_check, "main", fake_main)
    monkeypatch.setattr(sys, "argv", ["run_arm_pr_watch.py", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(os, "getppid", lambda: 4242)
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("GAIA_SESSION_ID", raising=False)
    monkeypatch.delenv("GAIA_SESSION_CLI", raising=False)
    monkeypatch.delenv("GAIA_HOOK_RUNTIME", raising=False)
    monkeypatch.delenv("GAIA_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("GAIA_PR_WATCH_AUTOLOOP", raising=False)
    monkeypatch.delenv("AGENTIC_PR_DASH_PR_WATCH_AUTOLOOP", raising=False)
    monkeypatch.delenv("WORKTREE_CONSOLE_CONFIG", raising=False)
    if env:
        for key, value in env.items():
            monkeypatch.setenv(key, value)

    assert run_arm_pr_watch.main() == 0
    return calls


def test_session_start_writes_self_id_and_arms_only_when_opted_in(monkeypatch, tmp_path):
    payload = {"cwd": str(tmp_path), "hook_event_name": "SessionStart"}

    assert _run_arm_hook(monkeypatch, payload, argv=["SessionStart"]) == []

    calls = _run_arm_hook(
        monkeypatch,
        payload,
        argv=["SessionStart"],
        env={"GAIA_PR_WATCH_AUTOLOOP": "true", "CODEX_SESSION_ID": "codex-session"},
    )

    assert calls == [
        [
            "arm",
            "--cwd",
            str(tmp_path),
            "--session-id",
            "codex-session",
        ]
    ]


def test_session_start_writes_self_id_even_without_optin(monkeypatch, tmp_path):
    # BOU-1442: the owning-session self-id is stamped at SessionStart regardless
    # of the pr-watch opt-in (it's "who launched here", no PR side effects) so
    # both Claude and Codex arming route through this one hook.
    from agentic_pr_dash.config import load as load_config

    payload = {
        "cwd": str(tmp_path),
        "hook_event_name": "SessionStart",
        "session_id": "sess-self",
    }

    calls = _run_arm_hook(monkeypatch, payload, argv=["SessionStart"])

    assert calls == []  # not opted in → no arm
    marker = load_config(str(tmp_path)).session_marker_for(str(tmp_path))
    assert marker.read_text(encoding="utf-8").strip() == "sess-self"


def test_payload_session_id_beats_codex_and_legacy_env(monkeypatch, tmp_path):
    payload = {"cwd": str(tmp_path), "session_id": "payload-session"}

    calls = _run_arm_hook(
        monkeypatch,
        payload,
        argv=["PostToolUse"],
        env={
            "CODEX_SESSION_ID": "codex-session",
            "GAIA_SESSION_ID": "legacy-session",
        },
    )

    assert calls == []

    payload["tool_name"] = "exec_command"
    payload["tool_input"] = {"cmd": "gh pr ready"}
    calls = _run_arm_hook(
        monkeypatch,
        payload,
        argv=["PostToolUse"],
        env={
            "CODEX_SESSION_ID": "codex-session",
            "GAIA_SESSION_ID": "legacy-session",
        },
    )

    assert calls[0][calls[0].index("--session-id") + 1] == "payload-session"


def test_codex_session_id_beats_legacy_gaia_session_id(monkeypatch, tmp_path):
    payload = {
        "cwd": str(tmp_path),
        "tool_name": "functions.exec_command",
        "tool_input": {"cmd": "gh pr create --fill"},
    }

    calls = _run_arm_hook(
        monkeypatch,
        payload,
        argv=["PostToolUse"],
        env={
            "CODEX_SESSION_ID": "codex-session",
            "GAIA_SESSION_ID": "legacy-session",
        },
    )

    assert calls[0][calls[0].index("--session-id") + 1] == "codex-session"


def test_codex_session_id_beats_inherited_claude_session_id(monkeypatch, tmp_path):
    payload = {
        "cwd": str(tmp_path),
        "tool_name": "functions.exec_command",
        "tool_input": {"cmd": "gh pr create --fill"},
    }

    calls = _run_arm_hook(
        monkeypatch,
        payload,
        argv=["PostToolUse"],
        env={
            "CODEX_SESSION_ID": "codex-session",
            "CLAUDE_SESSION_ID": "inherited-claude-session",
            "GAIA_SESSION_ID": "legacy-session",
        },
    )

    assert calls[0][calls[0].index("--session-id") + 1] == "codex-session"


def test_claude_session_id_beats_shared_gaia_session_id(monkeypatch, tmp_path):
    """Claude's conversation id must not fall back to the shared launcher id."""
    payload = {"cwd": str(tmp_path), "hook_event_name": "SessionStart"}

    _run_arm_hook(
        monkeypatch,
        payload,
        argv=["SessionStart"],
        env={
            "CLAUDE_SESSION_ID": "claude-conversation",
            "GAIA_SESSION_ID": "shared-launcher-v7",
            "GAIA_SESSION_CLI": "claude",
        },
    )

    marker = run_arm_pr_watch.load_config(str(tmp_path)).session_marker_for(str(tmp_path))
    assert marker.read_text(encoding="utf-8").strip() == "claude-conversation"


def test_session_start_uses_project_dir_when_payload_omits_cwd(monkeypatch, tmp_path):
    """A missing payload cwd must not redirect the marker to the hook process."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    hook_process_dir = tmp_path / "hook-process"
    hook_process_dir.mkdir()
    monkeypatch.chdir(hook_process_dir)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))

    _run_arm_hook(
        monkeypatch,
        {"hook_event_name": "SessionStart", "session_id": "session-in-project"},
        argv=["SessionStart"],
        env={"CLAUDE_PROJECT_DIR": str(project_dir)},
    )

    marker = run_arm_pr_watch.load_config(str(project_dir)).session_marker_for(str(project_dir))
    assert marker.read_text(encoding="utf-8").strip() == "session-in-project"


def test_session_start_prefers_current_worktree_over_stale_project_dir(
    monkeypatch, tmp_path
):
    """A stale launcher variable must not redirect a marker to another worktree."""
    current_dir = tmp_path / "current"
    current_dir.mkdir()
    (current_dir / ".git").mkdir()
    stale_dir = tmp_path / "stale"
    stale_dir.mkdir()
    monkeypatch.chdir(current_dir)

    _run_arm_hook(
        monkeypatch,
        {"hook_event_name": "SessionStart"},
        argv=["SessionStart"],
        env={
            "CODEX_SESSION_ID": "codex-session",
            "GAIA_PROJECT_DIR": str(stale_dir),
            "GAIA_SESSION_CLI": "codex",
        },
    )

    marker = run_arm_pr_watch.load_config(str(current_dir)).session_marker_for(str(current_dir))
    assert marker.read_text(encoding="utf-8").strip() == "codex-session"
    stale_marker = run_arm_pr_watch.load_config(str(stale_dir)).session_marker_for(str(stale_dir))
    assert not stale_marker.exists()


def test_codex_without_native_session_id_does_not_inherit_claude_id(
    monkeypatch, tmp_path
):
    """Codex must leave ownership unbound when only a parent Claude id exists."""
    _run_arm_hook(
        monkeypatch,
        {"hook_event_name": "SessionStart"},
        argv=["SessionStart"],
        env={
            "CLAUDE_SESSION_ID": "inherited-claude-session",
            "GAIA_SESSION_ID": "shared-launcher-id",
            "GAIA_SESSION_CLI": "codex",
        },
    )

    marker = run_arm_pr_watch.load_config(str(tmp_path)).session_marker_for(str(tmp_path))
    assert not marker.exists()


def test_pr_open_arms_without_autoloop_opt_in(monkeypatch, tmp_path):
    payload = {
        "cwd": str(tmp_path),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "command gh pr ready"},
    }

    calls = _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"])

    assert calls == [
        [
            "arm",
            "--cwd",
            str(tmp_path),
            "--session-id",
            "sess-A",
        ]
    ]


def test_arm_omits_pid_so_arm_resolves_durable_owner(monkeypatch, tmp_path):
    # The hook must NOT stamp os.getppid() (the short-lived shell): it lets
    # `maintenance_check arm` walk to the durable claude/codex owner instead.
    payload = {
        "cwd": str(tmp_path),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "gh pr create --fill"},
    }

    calls = _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"])

    assert calls
    assert "--pid" not in calls[0]


def test_gh_pr_ready_with_number_passes_explicit_pr(monkeypatch, tmp_path):
    payload = {
        "cwd": str(tmp_path),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "gh pr ready 123"},
    }

    calls = _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"])

    assert calls[0][calls[0].index("--pr") + 1] == "123"


def test_gh_pr_ready_pull_url_skips_arming(monkeypatch, tmp_path):
    # A pull URL may name another repository we can't arm from this cwd; skip
    # rather than mis-mark the local repo (codex PR #21 review).
    payload = {
        "cwd": str(tmp_path),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "gh pr ready https://github.com/o/r/pull/456"},
    }

    assert _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"]) == []


def test_gh_explicit_repo_skips_arming(monkeypatch, tmp_path):
    # `-R other/repo` targets a different repository; arming in this cwd would
    # mark the wrong PR, so skip.
    payload = {
        "cwd": str(tmp_path),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "gh -R other/repo pr ready 123"},
    }

    assert _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"]) == []


def test_gh_pr_create_option_value_not_treated_as_branch(monkeypatch, tmp_path):
    # `--title 'Fix flaky test'` is an option value, NOT a positional branch;
    # create takes its branch only from --head. With neither --pr nor --branch,
    # arm resolves the current branch (codex PR #21 P1).
    payload = {
        "cwd": str(tmp_path),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "gh pr create --title 'Fix flaky test' --body wip"},
    }

    calls = _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"])

    assert calls
    assert "--branch" not in calls[0]
    assert "--pr" not in calls[0]


def test_inline_shell_comment_not_treated_as_branch(monkeypatch, tmp_path):
    # `# open PR` is a shell comment, not a positional; create arms the current
    # branch (codex PR #21).
    payload = {
        "cwd": str(tmp_path),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "gh pr create --fill # open PR"},
    }

    calls = _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"])

    assert calls
    assert "--branch" not in calls[0]
    assert "--pr" not in calls[0]


def test_or_guarded_gh_pr_segment_is_not_armed(monkeypatch, tmp_path):
    # `cmd || gh pr ready 123` only runs gh on cmd's failure — don't record
    # ownership for a PR action the shell (usually) never ran.
    payload = {
        "cwd": str(tmp_path),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "make build || gh pr ready 123"},
    }

    assert _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"]) == []


def test_and_guarded_segment_skipped_when_command_failed(monkeypatch, tmp_path):
    # `git push && gh pr create` with a non-zero exit means the push failed and
    # gh never ran → do not arm.
    payload = {
        "cwd": str(tmp_path),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "git push && gh pr create --fill"},
        "tool_response": {"exit_code": 1},
    }

    assert _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"]) == []


def test_and_guarded_segment_armed_when_command_succeeded(monkeypatch, tmp_path):
    # Same command, exit 0 → push succeeded, gh pr create ran → arm.
    payload = {
        "cwd": str(tmp_path),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "git push && gh pr create --fill"},
        "tool_response": {"exit_code": 0},
    }

    calls = _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"])
    assert len(calls) == 1
    assert calls[0][:2] == ["arm", "--cwd"]


def test_cd_home_relative_target_is_expanded(monkeypatch, tmp_path):
    home = tmp_path / "home"
    worktree = home / "wt"
    worktree.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    payload = {
        "cwd": str(tmp_path),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "cd ~/wt && gh pr ready"},
    }

    calls = _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"])

    assert calls[0][calls[0].index("--cwd") + 1] == str(worktree)


def test_gh_pr_create_head_passes_branch(monkeypatch, tmp_path):
    payload = {
        "cwd": str(tmp_path),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "gh pr create --fill --head feature-x"},
    }

    calls = _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"])

    assert calls[0][calls[0].index("--branch") + 1] == "feature-x"
    assert "--pr" not in calls[0]


def test_gh_pr_create_attached_head_shorthand_passes_branch(monkeypatch, tmp_path):
    # pflag accepts `-Hfeature` / `-H=feature` attached shorthand.
    for cmd in ("gh pr create --fill -Hfeature-x", "gh pr create --fill -H=feature-x"):
        payload = {
            "cwd": str(tmp_path),
            "session_id": "sess-A",
            "tool_name": "exec_command",
            "tool_input": {"cmd": cmd},
        }
        calls = _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"])
        assert calls[0][calls[0].index("--branch") + 1] == "feature-x", cmd


def test_gh_pr_after_shell_separator_is_detected(monkeypatch, tmp_path):
    payload = {
        "cwd": str(tmp_path),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "git push && gh pr create --fill"},
    }

    # No autoloop opt-in: the git push must NOT arm, but the trailing gh pr
    # create must arm unconditionally even though it follows a separator.
    calls = _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"])

    assert len(calls) == 1
    assert calls[0][:2] == ["arm", "--cwd"]


def test_leading_cd_relocates_arm_cwd(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    payload = {
        "cwd": str(tmp_path),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "cd wt && gh pr ready"},
    }

    calls = _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"])

    assert calls[0][calls[0].index("--cwd") + 1] == str(worktree.resolve())


def test_exec_command_workdir_overrides_payload_cwd(monkeypatch, tmp_path):
    workdir = tmp_path / "sibling"
    workdir.mkdir()
    payload = {
        "cwd": str(tmp_path),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "gh pr create --fill", "workdir": str(workdir)},
    }

    calls = _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"])

    assert calls[0][calls[0].index("--cwd") + 1] == str(workdir)


def test_git_push_honors_opt_in_and_effective_git_cwd(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"
    nested = worktree / "nested"
    nested.mkdir(parents=True)
    payload = {
        "cwd": str(worktree),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "git -C nested push"},
    }

    assert _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"]) == []

    calls = _run_arm_hook(
        monkeypatch,
        payload,
        argv=["PostToolUse"],
        env={"AGENTIC_PR_DASH_PR_WATCH_AUTOLOOP": "1"},
    )

    assert calls[0][calls[0].index("--cwd") + 1] == str(nested)


def test_worktree_console_config_opt_in_is_honored(monkeypatch, tmp_path):
    conf = tmp_path / "worktree-console.conf"
    conf.write_text('WC_PR_WATCH_AUTOLOOP="yes"\n', encoding="utf-8")
    payload = {
        "cwd": str(tmp_path),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "git push"},
    }

    calls = _run_arm_hook(
        monkeypatch,
        payload,
        argv=["PostToolUse"],
        env={"WORKTREE_CONSOLE_CONFIG": str(conf)},
    )

    assert calls


def test_no_session_id_is_best_effort_noop(monkeypatch, tmp_path):
    payload = {
        "cwd": str(tmp_path),
        "tool_name": "exec_command",
        "tool_input": {"cmd": "gh pr create"},
    }

    assert _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"]) == []


def test_malformed_payload_is_best_effort(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(run_arm_pr_watch.maintenance_check, "main", lambda args: calls.append(args) or 0)
    monkeypatch.setattr(sys, "argv", ["run_arm_pr_watch.py", "PostToolUse"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("{"))

    assert run_arm_pr_watch.main() == 0
    assert calls == []


def test_effective_git_cwd_resolves_work_tree_relative_to_dash_c(tmp_path):
    base = tmp_path / "base"
    actual = run_arm_pr_watch.effective_git_cwd(
        "GIT_WORK_TREE=../wt git -C repo push",
        str(base),
    )

    assert actual == str((base / "repo" / "../wt").resolve())
