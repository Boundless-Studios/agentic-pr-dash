from __future__ import annotations

from pathlib import Path

from agentic_pr_dash import branch_head_guard


def _guard_calls(monkeypatch, *, branches: list[str] | None = None):
    calls: list[tuple[str | None, str | None, Path]] = []
    monkeypatch.setattr(
        branch_head_guard,
        "_check_one_target",
        lambda remote, refspec, cwd, _deadline: calls.append((remote, refspec, cwd)),
    )
    if branches is not None:
        monkeypatch.setattr(branch_head_guard, "_local_branches", lambda _cwd: branches)
    return calls


def test_push_segment_finds_plain_push_after_cd() -> None:
    assert branch_head_guard._push_segment("cd /repo && git push") == "git push"


def test_push_segment_ignores_non_push_commands() -> None:
    assert branch_head_guard._push_segment("git status") is None


def test_check_uses_cwd_changed_by_prior_segment(monkeypatch, tmp_path: Path) -> None:
    other = tmp_path / "other-worktree"
    other.mkdir()
    calls = _guard_calls(monkeypatch)
    branch_head_guard.check_command("cd other-worktree && git push", tmp_path)
    assert calls == [(None, None, other)]


def test_check_recognizes_git_global_dash_c(monkeypatch, tmp_path: Path) -> None:
    calls = _guard_calls(monkeypatch)
    branch_head_guard.check_command("git -C other-worktree push", tmp_path)
    assert calls == [(None, None, tmp_path / "other-worktree")]


def test_push_option_value_named_dry_run_is_not_a_dry_run(monkeypatch) -> None:
    calls = _guard_calls(monkeypatch)
    branch_head_guard.check_command("git push -o --dry-run origin feature", Path("/repo"))
    assert calls == [("origin", "feature", Path("/repo"))]


def test_actual_dry_run_push_is_never_guarded(monkeypatch) -> None:
    monkeypatch.setattr(
        branch_head_guard,
        "_check_one_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("queried")),
    )
    assert branch_head_guard.check_command("git push --dry-run", Path("/repo")) is None


def test_all_and_mirror_check_every_local_branch(monkeypatch) -> None:
    for flag in ("--all", "--mirror"):
        calls = _guard_calls(monkeypatch, branches=["main", "feature"])
        branch_head_guard.check_command(f"git push {flag} origin", Path("/repo"))
        assert calls == [
            ("origin", "main:main", Path("/repo")),
            ("origin", "feature:feature", Path("/repo")),
        ]


def test_bare_all_uses_current_branch_configured_push_remote(monkeypatch) -> None:
    calls = _guard_calls(monkeypatch, branches=["main", "feature"])
    monkeypatch.setattr(branch_head_guard, "_current_branch_name", lambda _cwd: "main")
    monkeypatch.setattr(branch_head_guard, "_push_remote", lambda branch, _cwd: "origin")
    branch_head_guard.check_command("git push --all", Path("/repo"))
    assert calls == [
        ("origin", "main:main", Path("/repo")),
        ("origin", "feature:feature", Path("/repo")),
    ]


def test_at_refspec_is_resolved_as_current_branch(monkeypatch) -> None:
    monkeypatch.setattr(branch_head_guard, "_current_branch_name", lambda _cwd: "feature")
    monkeypatch.setattr(
        branch_head_guard,
        "_tracks_destination",
        lambda *args: args[:3] == ("feature", "origin", "topic"),
    )
    monkeypatch.setattr(branch_head_guard, "_remote_ref_exists", lambda *_args: False)
    assert branch_head_guard._check_one_target(
        "origin", "@:topic", Path("/repo"), float("inf")
    ) is not None


def test_bare_push_uses_configured_push_remote(monkeypatch) -> None:
    monkeypatch.setattr(branch_head_guard, "_current_branch_name", lambda _cwd: "feature")
    values = {
        "branch.feature.pushRemote": "fork\n",
        "branch.feature.remote": "origin\n",
    }
    monkeypatch.setattr(branch_head_guard, "_config_value", lambda key, _cwd: values.get(key))
    seen: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        branch_head_guard,
        "_tracks_destination",
        lambda *args: seen.append(args[:3]) or False,
    )
    branch_head_guard._check_one_target(None, None, Path("/repo"), float("inf"))
    assert seen == [("feature", "fork", "feature")]


def test_matching_bare_push_does_not_check_deleted_current_branch(monkeypatch) -> None:
    monkeypatch.setattr(branch_head_guard, "_current_branch_name", lambda _cwd: "feature")
    monkeypatch.setattr(
        branch_head_guard,
        "_config_value",
        lambda key, _cwd: "matching\n" if key == "push.default" else None,
    )
    monkeypatch.setattr(
        branch_head_guard,
        "_tracks_destination",
        lambda *_args: (_ for _ in ()).throw(AssertionError("queried")),
    )
    assert branch_head_guard._check_one_target(None, None, Path("/repo"), float("inf")) is None


def test_push_default_nothing_does_not_check_current_branch(monkeypatch) -> None:
    monkeypatch.setattr(branch_head_guard, "_current_branch_name", lambda _cwd: "feature")
    monkeypatch.setattr(
        branch_head_guard,
        "_config_value",
        lambda key, _cwd: "nothing\n" if key == "push.default" else None,
    )
    monkeypatch.setattr(
        branch_head_guard,
        "_tracks_destination",
        lambda *_args: (_ for _ in ()).throw(AssertionError("queried")),
    )
    assert branch_head_guard._check_one_target(None, None, Path("/repo"), float("inf")) is None


def test_upstream_mode_skips_incompatible_push_remote(monkeypatch) -> None:
    monkeypatch.setattr(branch_head_guard, "_current_branch_name", lambda _cwd: "feature")
    monkeypatch.setattr(
        branch_head_guard,
        "_config_value",
        lambda key, _cwd: "upstream\n" if key == "push.default" else None,
    )
    monkeypatch.setattr(branch_head_guard, "_configured_upstream", lambda *_args: ("origin", "feature"))
    monkeypatch.setattr(branch_head_guard, "_push_remote", lambda *_args: "fork")
    monkeypatch.setattr(branch_head_guard, "_same_remote", lambda left, right, _cwd: left == right)
    monkeypatch.setattr(
        branch_head_guard,
        "_tracks_destination",
        lambda *_args: (_ for _ in ()).throw(AssertionError("queried")),
    )
    assert branch_head_guard._check_one_target(None, None, Path("/repo"), float("inf")) is None


def test_remote_probe_uses_push_url(monkeypatch) -> None:
    recorded: list[list[str]] = []
    monkeypatch.setattr(
        branch_head_guard, "_push_urls", lambda _remote, _cwd: ["/push/repo.git"]
    )
    monkeypatch.setattr(branch_head_guard.time, "monotonic", lambda: 0)

    class Result:
        returncode = 2
        stdout = ""

    def run(args, **_kwargs):
        recorded.append(args)
        return Result()

    monkeypatch.setattr(branch_head_guard.subprocess, "run", run)
    assert branch_head_guard._remote_ref_exists("origin", "feature", Path("/repo"), 10) is False
    assert recorded[0][-2] == "/push/repo.git"


def test_remote_probe_checks_every_push_url(monkeypatch) -> None:
    monkeypatch.setattr(
        branch_head_guard,
        "_push_urls",
        lambda _remote, _cwd: ["/push/one.git", "/push/two.git"],
    )
    monkeypatch.setattr(branch_head_guard.time, "monotonic", lambda: 0)
    seen: list[str] = []

    class Result:
        def __init__(self, returncode, stdout=""):
            self.returncode = returncode
            self.stdout = stdout

    def run(args, **_kwargs):
        seen.append(args[-2])
        if args[-2].endswith("one.git"):
            return Result(0, "abc\trefs/heads/feature\n")
        return Result(2)

    monkeypatch.setattr(branch_head_guard.subprocess, "run", run)
    assert branch_head_guard._remote_ref_exists("origin", "feature", Path("/repo"), 10) is False
    assert seen == ["/push/one.git", "/push/two.git"]


def test_cd_context_expands_home_and_ignores_failed_or_branch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "other").mkdir()
    calls = _guard_calls(monkeypatch)
    branch_head_guard.check_command("cd ~/other && git push", Path("/repo"))
    branch_head_guard.check_command("cd missing || git push", Path("/repo"))
    assert calls == [
        (None, None, tmp_path / "other"),
        (None, None, Path("/repo")),
    ]


def test_failed_cd_before_semicolon_keeps_original_cwd(monkeypatch) -> None:
    calls = _guard_calls(monkeypatch)
    branch_head_guard.check_command("cd missing; git push", Path("/repo"))
    assert calls == [(None, None, Path("/repo"))]


def test_failed_cd_before_and_means_push_cannot_execute(monkeypatch) -> None:
    monkeypatch.setattr(
        branch_head_guard,
        "_check_one_target",
        lambda *_args: (_ for _ in ()).throw(AssertionError("queried")),
    )
    assert branch_head_guard.check_command("cd missing && git push", Path("/repo")) is None


def test_conditionally_executed_cd_after_unknown_command_fails_open(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "other").mkdir()
    monkeypatch.setattr(
        branch_head_guard,
        "_check_one_target",
        lambda *_args: (_ for _ in ()).throw(AssertionError("queried")),
    )
    assert branch_head_guard.check_command(
        "unknown-command && cd other; git push", tmp_path
    ) is None
