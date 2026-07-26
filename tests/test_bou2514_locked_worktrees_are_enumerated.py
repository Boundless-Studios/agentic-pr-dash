"""BOU-2514: a git-locked worktree must still be visible to ownership enumeration.

`git worktree lock` means "do not prune or remove me" -- an administrative
protection, not an absence. Treating locked worktrees as invisible made the most
actively-used ones vanish: gaia's `_agent/`-namespaced worktrees lock themselves
(`locked active-agent`) so the start-worktree sweep cannot delete them
mid-session, so every one of them dropped out of `_collect_owned_worktrees`.

The user-visible failure: a session could `arm` a PR successfully and hold a
valid, active ownership claim naming it, while `list-owned` returned empty and
the `await` waiter reported "watched NOTHING" -- so review feedback and red
checks never woke it.
"""

from __future__ import annotations

import subprocess

from agentic_pr_dash._maintenance import worktrees as W


def _porcelain(monkeypatch, stdout: str) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(W.subprocess, "run", fake_run)


# The exact shape `git worktree list --porcelain` emits on the reporting machine:
# a plain worktree, an agent worktree locked with a reason, one locked with the
# bare `locked` keyword, and a bare repo.
PORCELAIN = """\
worktree /repo/plain
HEAD 1111111111111111111111111111111111111111
branch refs/heads/plain-branch

worktree /repo/_agent/agent-wt
HEAD 2222222222222222222222222222222222222222
branch refs/heads/agent-branch
locked active-agent

worktree /repo/locked-no-reason
HEAD 3333333333333333333333333333333333333333
branch refs/heads/other-branch
locked

worktree /repo/bare-one
bare
"""


def test_locked_worktrees_are_enumerated(monkeypatch):
    _porcelain(monkeypatch, PORCELAIN)

    found = dict(W._iter_worktrees_with_branch("/repo/plain"))

    assert found["/repo/_agent/agent-wt"] == "agent-branch", (
        "a worktree locked with a reason (`locked active-agent`) must be visible"
    )
    assert found["/repo/locked-no-reason"] == "other-branch", (
        "the bare `locked` keyword form must be visible too"
    )
    assert found["/repo/plain"] == "plain-branch"


def test_bare_worktrees_are_still_excluded(monkeypatch):
    """A bare repo has no checked-out branch to resolve a PR against."""
    _porcelain(monkeypatch, PORCELAIN)

    found = dict(W._iter_worktrees_with_branch("/repo/plain"))

    assert "/repo/bare-one" not in found
    assert len(found) == 3


def test_a_trailing_locked_worktree_is_not_dropped(monkeypatch):
    """The final record has no trailing blank line, so it takes the flush path."""
    trailing = (
        "worktree /repo/plain\n"
        "HEAD 1111111111111111111111111111111111111111\n"
        "branch refs/heads/plain-branch\n"
        "\n"
        "worktree /repo/_agent/last-one\n"
        "HEAD 2222222222222222222222222222222222222222\n"
        "branch refs/heads/last-branch\n"
        "locked active-agent\n"
    )
    _porcelain(monkeypatch, trailing)

    found = dict(W._iter_worktrees_with_branch("/repo/plain"))

    assert found.get("/repo/_agent/last-one") == "last-branch"
