from __future__ import annotations

from pathlib import Path

import pytest

from agentic_pr_dash import config, maintenance_check


SID = "sess-owned"


@pytest.fixture(autouse=True)
def _stop_gate_no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    config.load.cache_clear()


def test_stop_gate_does_not_adopt_unmarked_open_pr_worktrees(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owned = tmp_path / "owned"
    unrelated = tmp_path / "unrelated"
    owned.mkdir()
    unrelated.mkdir()

    monkeypatch.setattr(
        maintenance_check,
        "_iter_worktrees_with_branch",
        lambda cwd: [(str(owned), "owned-branch"), (str(unrelated), "unrelated-branch")],
    )
    monkeypatch.setattr(
        maintenance_check,
        "_marker_session_id",
        lambda path: SID if Path(path) == owned else None,
    )
    monkeypatch.setattr(
        maintenance_check,
        "_list_my_open_prs",
        lambda cwd: {"unrelated-branch": (202, False)},
    )
    adopted: list[str] = []
    monkeypatch.setattr(
        maintenance_check,
        "_write_arm_marker",
        lambda path, session_id, pid, pr_number: adopted.append(path) or True,
    )
    monkeypatch.setattr(
        maintenance_check,
        "_live_independent_owner_paths",
        lambda paths, session_id: set(),
    )

    checked: list[str] = []

    def _check(path: str, session_id: str) -> tuple[int, str]:
        checked.append(path)
        if Path(path) == unrelated:
            return 10, "unrelated blocker\nPR_NUMBER=202"
        return 0, "nothing pending"

    monkeypatch.setattr(maintenance_check, "_check_worktree", _check)

    rc = maintenance_check.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])

    assert rc == 0
    assert checked == [str(owned)]
    assert adopted == []


def test_stop_gate_skips_marker_owned_path_with_live_independent_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()

    monkeypatch.setattr(
        maintenance_check,
        "_iter_worktrees_with_branch",
        lambda cwd: [(str(owned), "owned-branch")],
    )
    monkeypatch.setattr(maintenance_check, "_marker_session_id", lambda path: SID)
    monkeypatch.setattr(maintenance_check, "_list_my_open_prs", lambda cwd: {})
    monkeypatch.setattr(
        maintenance_check,
        "_live_independent_owner_paths",
        lambda paths, session_id: {str(owned)},
    )

    checked: list[str] = []
    monkeypatch.setattr(
        maintenance_check,
        "_check_worktree",
        lambda path, session_id: checked.append(path) or (10, "blocker\nPR_NUMBER=1"),
    )

    rc = maintenance_check.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])

    assert rc == 0
    assert checked == []
