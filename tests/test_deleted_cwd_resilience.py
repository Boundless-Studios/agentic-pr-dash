"""The dashboard must survive having its working directory deleted (BOU-2193).

The dashboard daemon inherits the cwd of whatever launched it — in practice an
ephemeral gaia worktree. Stale-worktree reaping deletes that directory while the
process is still running, at which point ``os.getcwd()`` raises
``FileNotFoundError: [Errno 2]`` for the remaining life of the process.

Observed in production: 1523 such tracebacks in ``~/.claude/daemons/pr-dashboard.log``,
all from ``worktrees.py:get_main_repo_root`` via ``app.py:_build_unassigned_pr_card``
via ``app.py:board_partial``. Every ``/partials/board`` render 500s, htmx skips the
swap, and the board silently freezes on stale content.

BOU-1905 already solved this class of bug for the waiter by adding
``config._safe_cwd()``, but only wired it into ``config.py``. These tests pin the
call sites BOU-1905 missed.

Note on technique: ``tests/test_waiter_resilience.py`` fakes the failure with
``monkeypatch.setattr(config.Path, "cwd", _boom)``. These tests deliberately do NOT
do that — they delete a real directory and let the real ``os.getcwd()`` raise, so the
guard cannot pass against a mock of the very layer the bug lives in.
"""

import json
import os
import shutil
import tempfile

import pytest

from agentic_pr_dash import session_registry, worktrees


@pytest.fixture
def deleted_cwd():
    """Chdir into a real directory, then delete it out from under the process.

    Restores the original cwd on teardown. After this fixture runs, ``os.getcwd()``
    raises ``FileNotFoundError`` — exactly the production state.
    """
    original = os.getcwd()
    doomed = tempfile.mkdtemp(prefix="bou2193-doomed-")
    os.chdir(doomed)
    shutil.rmtree(doomed, ignore_errors=True)
    try:
        # Sanity: the premise of every test below is that the cwd is really gone.
        with pytest.raises(FileNotFoundError):
            os.getcwd()
        yield
    finally:
        os.chdir(original)


def test_get_main_repo_root_survives_deleted_cwd(deleted_cwd):
    """The frame in the production traceback: worktrees.py:49 scan_root = root or os.getcwd()."""
    root = worktrees.get_main_repo_root()

    assert root, "get_main_repo_root() must return a usable root, not an empty value"
    assert isinstance(root, str), (
        f"get_main_repo_root() is annotated -> str; got {type(root).__name__}. "
        "A bare Path breaks orchestrator.py's `repo_cwd not in roots` membership "
        "check, which compares against a list of str."
    )


def test_get_main_repo_root_returns_str_with_live_cwd():
    """The str contract must hold on the happy path too, not just the fallback path."""
    assert isinstance(worktrees.get_main_repo_root(), str)


def test_explicit_root_is_honored_verbatim(tmp_path, deleted_cwd):
    """The $HOME fallback must apply ONLY to the ambient cwd, never to a caller's root.

    A caller that passes a root is making an explicit statement about which repo to
    scan; silently redirecting that to $HOME would be a worse bug than the crash.
    """
    explicit = str(tmp_path)

    assert worktrees.get_main_repo_root(explicit) == explicit


def test_session_event_is_json_serializable_with_deleted_cwd(tmp_path, monkeypatch, deleted_cwd):
    """session_registry's ambient-cwd sites feed a JSONL event log.

    Substituting a bare ``Path`` here would swap the FileNotFoundError for a
    TypeError on ``json.dumps`` — fixing the crash into a different crash.
    """
    monkeypatch.delenv("PROJECT_DIR", raising=False)
    registry = tmp_path / "sessions.jsonl"

    event = session_registry.record_event(
        event="session_start",
        session_id="bou2193-test",
        path=registry,
    )

    round_tripped = json.loads(json.dumps(event))
    assert round_tripped["worktree_path"], "event must still record a worktree path"
    assert isinstance(round_tripped["worktree_path"], str)
