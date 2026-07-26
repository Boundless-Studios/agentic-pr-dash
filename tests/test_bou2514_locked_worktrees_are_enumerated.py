"""BOU-2514: a git-locked worktree that EXISTS must be visible to enumeration.

`git worktree lock` carries two very different meanings:

* "this worktree is in active use, do not prune it" — gaia's `_agent/`-namespaced
  worktrees lock themselves (`locked active-agent`) so the start-worktree sweep
  cannot delete them mid-session (BOU-2232); and
* "this worktree lives on a portable device or network share that is not always
  mounted" — git's own documented reason, where the path may be absent.

Skipping all locked worktrees broke the first case: a session could `arm` a PR,
hold a valid active ownership claim naming it, and still have `list-owned` return
empty and the `await` waiter answer "watched NOTHING", so review feedback and red
checks never woke it.

Yielding all locked worktrees would break the second: `_collect_owned_worktrees`
would match the absent path's recorded branch to an open PR and call
`_write_arm_marker`, whose `os.makedirs(..., exist_ok=True)` RECREATES the missing
mount point and adopts a phantom worktree (PR #118 review).

The discriminator is whether the path is currently a directory.
"""

from __future__ import annotations

import subprocess

from agentic_pr_dash._maintenance import worktrees as W


def _porcelain(monkeypatch, stdout: str) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(W.subprocess, "run", fake_run)


def _layout(tmp_path):
    """Real directories for everything except the unmounted lock."""
    plain = tmp_path / "plain"
    agent = tmp_path / "_agent" / "agent-wt"
    locked_plain = tmp_path / "locked-no-reason"
    for d in (plain, agent, locked_plain):
        d.mkdir(parents=True)
    unmounted = tmp_path / "on-a-usb-stick"  # deliberately NOT created
    return plain, agent, locked_plain, unmounted


def _stdout(plain, agent, locked_plain, unmounted, bare) -> str:
    return (
        f"worktree {plain}\n"
        "HEAD 1111111111111111111111111111111111111111\n"
        "branch refs/heads/plain-branch\n"
        "\n"
        f"worktree {agent}\n"
        "HEAD 2222222222222222222222222222222222222222\n"
        "branch refs/heads/agent-branch\n"
        "locked active-agent\n"
        "\n"
        f"worktree {locked_plain}\n"
        "HEAD 3333333333333333333333333333333333333333\n"
        "branch refs/heads/other-branch\n"
        "locked\n"
        "\n"
        f"worktree {unmounted}\n"
        "HEAD 4444444444444444444444444444444444444444\n"
        "branch refs/heads/usb-branch\n"
        "locked on a portable device\n"
        "\n"
        f"worktree {bare}\n"
        "bare\n"
    )


def test_locked_worktrees_that_exist_are_enumerated(monkeypatch, tmp_path):
    plain, agent, locked_plain, unmounted = _layout(tmp_path)
    _porcelain(monkeypatch, _stdout(plain, agent, locked_plain, unmounted, tmp_path / "bare"))

    found = dict(W._iter_worktrees_with_branch(str(plain)))

    assert found[str(agent)] == "agent-branch", (
        "a live worktree locked with a reason (`locked active-agent`) must be visible"
    )
    assert found[str(locked_plain)] == "other-branch", (
        "the bare `locked` keyword form must be visible too"
    )
    assert found[str(plain)] == "plain-branch"


def test_a_locked_worktree_whose_path_is_absent_is_skipped(monkeypatch, tmp_path):
    """The unmounted-device case git's lock documentation describes."""
    plain, agent, locked_plain, unmounted = _layout(tmp_path)
    _porcelain(monkeypatch, _stdout(plain, agent, locked_plain, unmounted, tmp_path / "bare"))

    found = dict(W._iter_worktrees_with_branch(str(plain)))

    assert str(unmounted) not in found, (
        "yielding an absent locked path lets _write_arm_marker recreate the "
        "mount point and adopt a phantom worktree"
    )


def test_bare_worktrees_are_still_excluded(monkeypatch, tmp_path):
    """A bare repo has no checked-out branch to resolve a PR against."""
    plain, agent, locked_plain, unmounted = _layout(tmp_path)
    bare = tmp_path / "bare"
    bare.mkdir()
    _porcelain(monkeypatch, _stdout(plain, agent, locked_plain, unmounted, bare))

    found = dict(W._iter_worktrees_with_branch(str(plain)))

    assert str(bare) not in found
    assert len(found) == 3


def test_a_trailing_locked_worktree_is_not_dropped(monkeypatch, tmp_path):
    """The final record has no trailing blank line, so it takes the flush path."""
    plain = tmp_path / "plain"
    last = tmp_path / "_agent" / "last-one"
    plain.mkdir()
    last.mkdir(parents=True)
    _porcelain(
        monkeypatch,
        f"worktree {plain}\n"
        "HEAD 1111111111111111111111111111111111111111\n"
        "branch refs/heads/plain-branch\n"
        "\n"
        f"worktree {last}\n"
        "HEAD 2222222222222222222222222222222222222222\n"
        "branch refs/heads/last-branch\n"
        "locked active-agent\n",
    )

    found = dict(W._iter_worktrees_with_branch(str(plain)))

    assert found.get(str(last)) == "last-branch"


def test_a_trailing_absent_locked_worktree_is_skipped(monkeypatch, tmp_path):
    """The flush path must apply the same directory check as the loop."""
    plain = tmp_path / "plain"
    plain.mkdir()
    gone = tmp_path / "_agent" / "unmounted-last"
    _porcelain(
        monkeypatch,
        f"worktree {plain}\n"
        "HEAD 1111111111111111111111111111111111111111\n"
        "branch refs/heads/plain-branch\n"
        "\n"
        f"worktree {gone}\n"
        "HEAD 2222222222222222222222222222222222222222\n"
        "branch refs/heads/gone-branch\n"
        "locked on a portable device\n",
    )

    found = dict(W._iter_worktrees_with_branch(str(plain)))

    assert str(gone) not in found
