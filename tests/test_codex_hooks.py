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
    monkeypatch.delenv("GAIA_SESSION_ID", raising=False)
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


def test_gh_pr_ready_with_pull_url_passes_explicit_pr(monkeypatch, tmp_path):
    payload = {
        "cwd": str(tmp_path),
        "session_id": "sess-A",
        "tool_name": "exec_command",
        "tool_input": {"cmd": "gh pr ready https://github.com/o/r/pull/456"},
    }

    calls = _run_arm_hook(monkeypatch, payload, argv=["PostToolUse"])

    assert calls[0][calls[0].index("--pr") + 1] == "456"


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
