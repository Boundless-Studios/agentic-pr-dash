from __future__ import annotations

import builtins
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from agentic_pr_dash import config, maintenance
from agentic_pr_dash._maintenance import stop_gate
from agentic_pr_dash._maintenance import waiter as _waiter_mod
from agentic_pr_dash._maintenance import worktree_check as _worktree_check_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash.models import PRData, ReviewComment


COMMENT_BODY = "shields.io badge noise body text"
SUMMARY = "PR #62 (bou-demo-branch): 3 unresolved review comment(s), CI green"
VERBOSE_PROMPT = f"""PR #62 has pending maintenance work.

### Comment 12345 by @codex-reviewer on `src/foo.py:10`

{COMMENT_BODY}

SUMMARY={SUMMARY}
PR_NUMBER=62"""


@pytest.fixture(autouse=True)
def _stop_gate_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _payload_path(cwd: Path) -> Path:
    return config.load(str(cwd)).state_dir_for(cwd) / "pr-watch.stop-payload.md"


def _patch_pending_stop(
    monkeypatch: pytest.MonkeyPatch,
    cwd: Path,
    *,
    prompt: str = VERBOSE_PROMPT,
    detached_records: list[dict] | None = None,
) -> SimpleNamespace:
    """Keep the real stop-gate decision/rendering path and fake its I/O seams."""
    owned = [] if detached_records is not None else [str(cwd)]
    monkeypatch.setattr(
        _worktrees_mod,
        "_reconcile_owned_across_roots",
        lambda session_id, anchor, pid, deadline=None: (owned, []),
    )
    monkeypatch.setattr(
        _worktrees_mod,
        "_owned_worktrees_across_roots",
        lambda session_id, anchor: owned,
    )
    monkeypatch.setattr(
        _worktrees_mod,
        "_detached_records_across_roots",
        lambda session_id, anchor: detached_records or [],
    )
    monkeypatch.setattr(
        _worktree_check_mod,
        "_check_worktree",
        lambda path, session_id, *, claim=False: (10, prompt),
    )
    monkeypatch.setattr(stop_gate, "_owned_open_pr_pairs", lambda worktrees: [])
    monkeypatch.setattr(_waiter_mod, "_detached_loop_alive", lambda cwd, session_id: False)
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, session_id: False)
    return SimpleNamespace(
        cwd=str(cwd), session_id="sess-test", pid=None, no_waiter=True
    )


def test_concise_stderr_contains_summary_and_pointer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _patch_pending_stop(monkeypatch, tmp_path)

    rc = stop_gate._stop_gate_impl(args)

    stderr = capsys.readouterr().err
    assert rc == 2
    assert SUMMARY in stderr
    assert str(_payload_path(tmp_path)) in stderr


def test_concise_stderr_omits_verbose_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _patch_pending_stop(monkeypatch, tmp_path)

    rc = stop_gate._stop_gate_impl(args)

    stderr = capsys.readouterr().err
    assert rc == 2
    assert COMMENT_BODY not in stderr
    assert "### Comment" not in stderr
    assert "FIRST, before changing anything" not in stderr


def test_payload_file_has_full_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _patch_pending_stop(monkeypatch, tmp_path)

    rc = stop_gate._stop_gate_impl(args)
    capsys.readouterr()

    assert rc == 2
    payload = _payload_path(tmp_path)
    assert payload.exists()
    text = payload.read_text(encoding="utf-8")
    assert COMMENT_BODY in text
    assert "### Comment" in text
    assert "agentic-pr-dash complete" in text
    assert "FIRST, before changing anything" in text


def test_payload_write_failure_falls_back_verbose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _patch_pending_stop(monkeypatch, tmp_path)
    payload = _payload_path(tmp_path)

    # Prefer the future payload seam when it exists. Until then, selectively
    # fail common file-writing APIs without disturbing the stop-loop state file.
    if hasattr(stop_gate, "_write_stop_payload"):
        monkeypatch.setattr(
            stop_gate,
            "_write_stop_payload",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )
    else:
        real_open = builtins.open
        real_write_text = Path.write_text

        def fail_payload_open(file, mode="r", *args, **kwargs):
            if os.fspath(file) == os.fspath(payload) and "w" in mode:
                raise OSError("disk full")
            return real_open(file, mode, *args, **kwargs)

        def fail_payload_write_text(path: Path, data: str, *args, **kwargs):
            if path == payload:
                raise OSError("disk full")
            return real_write_text(path, data, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", fail_payload_open)
        monkeypatch.setattr(Path, "write_text", fail_payload_write_text)

    rc = stop_gate._stop_gate_impl(args)

    stderr = capsys.readouterr().err
    assert rc == 2
    assert "### Comment" in stderr
    assert COMMENT_BODY in stderr
    assert str(payload) not in stderr
    # This fallback invariant already passes before concise rendering exists.


def test_detached_entry_gets_summary_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    detached = {
        "pr": 77,
        "url": "https://github.com/example/repo/pull/77",
        "branch": "bou-detached-branch",
        "worktree_present": False,
        "unresolved_threads": 2,
        "ci_failing": True,
        "failing_checks": ["tests"],
        "changes_requested": False,
        "merge_conflict": False,
        "review_decision": "",
        "merge_state": "",
        "mergeable": "",
        "p1": True,
        "state": "open",
    }
    args = _patch_pending_stop(monkeypatch, tmp_path, detached_records=[detached])

    rc = stop_gate._stop_gate_impl(args)

    stderr = capsys.readouterr().err
    pr_lines = [line for line in stderr.splitlines() if "PR #77" in line]
    assert rc == 2
    assert len(pr_lines) == 1
    assert "2 unresolved review comment(s)" in pr_lines[0]
    assert "This PR's worktree was torn down" not in stderr
    payload_text = _payload_path(tmp_path).read_text(encoding="utf-8")
    assert "This PR's worktree was torn down" in payload_text


def test_fingerprint_loop_break_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _patch_pending_stop(monkeypatch, tmp_path)

    results = [stop_gate._stop_gate_impl(args) for _ in range(3)]
    capsys.readouterr()

    assert results == [2, 2, 0]


class TestBuildMaintenanceSummary:
    @staticmethod
    def _summary_fn() -> Callable[[PRData], str]:
        summary_fn = getattr(maintenance, "build_maintenance_summary", None)
        assert summary_fn is not None, "build_maintenance_summary missing"
        return summary_fn

    @staticmethod
    def _pr(**overrides) -> PRData:
        values = {
            "number": 5,
            "title": "summary fixture",
            "branch": "br",
            "url": "https://github.com/example/repo/pull/5",
        }
        values.update(overrides)
        return PRData(**values)

    def test_all_parts_are_in_stable_order(self) -> None:
        pr = self._pr(
            review_comments=[
                ReviewComment(id=1, author="a", body="one", created_at="2026-01-01"),
                ReviewComment(id=2, author="b", body="two", created_at="2026-01-02"),
            ],
            failing_checks=["tests"],
            merge_state="DIRTY",
            review_decision="CHANGES_REQUESTED",
        )

        summary = self._summary_fn()(pr)

        assert summary == (
            "PR #5 (br): 2 unresolved review comment(s), 1 failing CI check(s), "
            "merge conflict, changes requested"
        )

    def test_no_failing_checks_reports_ci_green(self) -> None:
        summary = self._summary_fn()(self._pr(failing_checks=[]))

        assert "CI green" in summary

    def test_falsey_parts_are_absent(self) -> None:
        summary = self._summary_fn()(
            self._pr(
                review_comments=[],
                failing_checks=[],
                merge_state="CLEAN",
                review_decision="APPROVED",
            )
        )

        assert "0 unresolved review comment(s)" not in summary
        assert "merge conflict" not in summary
        assert "changes requested" not in summary
