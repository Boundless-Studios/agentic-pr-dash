from __future__ import annotations

from agentic_pr_dash import branch_head_guard


def test_push_segment_finds_plain_push_after_cd() -> None:
    assert branch_head_guard._push_segment("cd /repo && git push") == "git push"


def test_push_segment_ignores_non_push_commands() -> None:
    assert branch_head_guard._push_segment("git status") is None


def test_dry_run_push_is_never_guarded(monkeypatch) -> None:
    monkeypatch.setattr(
        branch_head_guard,
        "_check_one_target",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("queried")),
    )
    assert branch_head_guard.check_command("git push --dry-run", "/repo") is None
