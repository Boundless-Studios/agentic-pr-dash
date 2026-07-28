"""BOU-1787 regression: stop-gate must run list-owned-equivalent adoption with
the durable owner pid BEFORE its clean-stop rate-limit early-return.

The bug: ``_stop_gate_impl`` returned 0 from the rate-limit path (a clean stop
younger than STOP_INTERVAL) *before* it ever collected/adopted owned worktrees,
and it collected them with the passive (never-adopting) helper. So a PR created
mid-session whose arm hook was missed stayed invisible until the 180s interval
expired. After the fix, stop-gate reconciles (adopts) first, and a fresh
adoption bypasses the rate-limit for that tick so the worktree is inspected on
the same invocation.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentic_pr_dash import config, maintenance_check
from agentic_pr_dash._maintenance import markers as _markers_mod
from agentic_pr_dash._maintenance import stop_gate as _stop_gate_mod
from agentic_pr_dash._maintenance import worktree_check as _worktree_check_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod


SID = "sess-1787"
PR_NUMBER = 909


def _seed_recent_clean_stop(cwd: str) -> None:
    """Write a stop-loop state that would trigger the rate-limit early-return:
    recent ``ts`` (now), no pending ``fingerprint``."""
    import time

    _stop_gate_mod._save_stop_state(cwd, {"ts": time.time()})


def _wire_unmarked_open_pr_worktree(
    monkeypatch: pytest.MonkeyPatch,
    worktree: Path,
    *,
    adopted: list[tuple[str, str, int, int]],
    checked: list[str],
    resolved_pid: int = 4242,
) -> None:
    """One candidate worktree, initially unmarked, with an open non-draft PR and
    no live foreign/independent owner. ``_write_arm_marker`` records the adoption
    and flips the marker so later reads in the same invocation see the session.
    """
    marker_state: dict[str, str] = {}

    monkeypatch.setattr(
        _worktrees_mod,
        "_iter_worktrees_with_branch",
        lambda cwd: [(str(worktree), "feature-branch")],
    )
    monkeypatch.setattr(
        _worktrees_mod,
        "_live_independent_owner_paths",
        lambda paths, session_id: set(),
    )
    monkeypatch.setattr(
        _worktrees_mod,
        "_list_my_open_prs",
        lambda cwd, timeout=15: {"feature-branch": (PR_NUMBER, False)},
    )
    # Default owner-pid resolution (no --pid passed) must yield the durable pid.
    monkeypatch.setattr(_worktrees_mod, "_resolve_owner_pid", lambda: resolved_pid)

    monkeypatch.setattr(
        _markers_mod,
        "_marker_session_id",
        lambda path: marker_state.get(str(Path(path))),
    )
    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda path, sid: None)

    def _adopt(
        path: str, session_id: str, pid: int, pr_number: int, provenance: str = "armed"
    ) -> bool:
        adopted.append((str(Path(path)), session_id, int(pid), int(pr_number)))
        marker_state[str(Path(path))] = session_id
        return True

    monkeypatch.setattr(_markers_mod, "_write_arm_marker", _adopt)
    # Marker read after a clean check (for stale-prune) — return the adopted dict.
    monkeypatch.setattr(
        _markers_mod,
        "_read_marker",
        lambda path: {"pr": str(PR_NUMBER)} if marker_state.get(str(Path(path))) else {},
    )

    def _check(path: str, session_id: str, *, claim: bool = True) -> tuple[int, str]:
        checked.append(str(Path(path)))
        return 10, f"pending review\nPR_NUMBER={PR_NUMBER}"

    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", _check)


@pytest.fixture(autouse=True)
def _clear_config_cache() -> None:
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def test_stop_gate_inspects_freshly_adopted_pr_within_rate_limit_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The core BOU-1787 regression. A recent clean stop is inside the
    STOP_INTERVAL window, but reconciliation adopts a previously-unowned
    non-draft PR — stop-gate must NOT idle; it must inspect that worktree and
    block (exit 2) on the same invocation."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "180")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    config.load.cache_clear()

    worktree = tmp_path / "wt"
    worktree.mkdir()
    adopted: list[tuple[str, str, int, int]] = []
    checked: list[str] = []
    _wire_unmarked_open_pr_worktree(
        monkeypatch, worktree, adopted=adopted, checked=checked
    )

    _seed_recent_clean_stop(str(tmp_path))

    rc = maintenance_check.main(
        ["stop-gate", "--cwd", str(tmp_path), "--session-id", SID]
    )

    # Adoption happened, and the freshly-adopted worktree WAS inspected despite
    # the recent clean stop being inside the rate-limit window.
    assert adopted and adopted[0][0] == str(worktree)
    assert checked == [str(worktree)], "adopted worktree must be inspected this tick"
    assert rc == 2


def test_stop_gate_threads_durable_owner_pid_into_adoption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With no --pid, stop-gate resolves the durable owner pid and passes it into
    marker adoption (Stop-context ownership)."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "180")
    config.load.cache_clear()

    worktree = tmp_path / "wt"
    worktree.mkdir()
    adopted: list[tuple[str, str, int, int]] = []
    checked: list[str] = []
    _wire_unmarked_open_pr_worktree(
        monkeypatch, worktree, adopted=adopted, checked=checked, resolved_pid=4242
    )
    _seed_recent_clean_stop(str(tmp_path))

    maintenance_check.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])

    assert adopted, "expected an adoption"
    assert adopted[0][2] == 4242, "durable owner pid must be threaded into adoption"
    assert adopted[0][3] == PR_NUMBER


def test_stop_gate_explicit_pid_used_for_adoption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An explicit --pid overrides the resolved owner pid."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "180")
    config.load.cache_clear()

    worktree = tmp_path / "wt"
    worktree.mkdir()
    adopted: list[tuple[str, str, int, int]] = []
    checked: list[str] = []
    _wire_unmarked_open_pr_worktree(
        monkeypatch, worktree, adopted=adopted, checked=checked, resolved_pid=4242
    )
    _seed_recent_clean_stop(str(tmp_path))

    maintenance_check.main(
        ["stop-gate", "--cwd", str(tmp_path), "--session-id", SID, "--pid", "777"]
    )

    assert adopted and adopted[0][2] == 777


def test_stop_gate_uses_session_marker_fallback_for_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When --session-id is omitted, stop-gate resolves the session from the
    pr-watch.session marker and still reconciles/adopts (no Gaia duplication)."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "180")
    config.load.cache_clear()

    worktree = tmp_path / "wt"
    worktree.mkdir()
    adopted: list[tuple[str, str, int, int]] = []
    checked: list[str] = []
    _wire_unmarked_open_pr_worktree(
        monkeypatch, worktree, adopted=adopted, checked=checked
    )
    monkeypatch.setattr(_stop_gate_mod, "_read_session_marker", lambda cwd: SID)
    _seed_recent_clean_stop(str(tmp_path))

    rc = maintenance_check.main(["stop-gate", "--cwd", str(tmp_path)])

    assert adopted and adopted[0][1] == SID
    assert checked == [str(worktree)]
    assert rc == 2


def test_stop_gate_still_idles_when_nothing_adopted_within_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cost-preservation: a recent clean stop with NOTHING to adopt still
    early-returns 0 (rate-limit intact) and never runs the per-worktree check."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "180")
    config.load.cache_clear()

    worktree = tmp_path / "wt"
    worktree.mkdir()
    # Already markered to this session → no adoption; no open PR to adopt anyway.
    monkeypatch.setattr(
        _worktrees_mod,
        "_iter_worktrees_with_branch",
        lambda cwd: [(str(worktree), "feature-branch")],
    )
    monkeypatch.setattr(
        _worktrees_mod, "_live_independent_owner_paths", lambda paths, sid: set()
    )
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda path: SID)
    adopted: list[str] = []
    monkeypatch.setattr(
        _markers_mod,
        "_write_arm_marker",
        lambda path, sid, pid, pr: adopted.append(path) or True,
    )
    checked: list[str] = []
    monkeypatch.setattr(
        _worktree_check_mod,
        "_check_worktree",
        lambda path, sid, *, claim=True: checked.append(path) or (0, "clean"),
    )
    _seed_recent_clean_stop(str(tmp_path))

    rc = maintenance_check.main(
        ["stop-gate", "--cwd", str(tmp_path), "--session-id", SID]
    )

    assert rc == 0
    assert adopted == []
    assert checked == [], "rate-limit must still short-circuit when nothing adopted"


def test_collect_owned_skips_gh_probe_when_over_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """BOU-1787 review: an already-passed deadline must skip the gh adoption probe
    entirely (no _list_my_open_prs call), so a single slow root can't blow the
    Stop-hook deadline. Markered owners are still returned."""
    import time

    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(
        _worktrees_mod,
        "_iter_worktrees_with_branch",
        lambda cwd: [(str(worktree), "feature-branch")],
    )
    monkeypatch.setattr(
        _worktrees_mod, "_live_independent_owner_paths", lambda paths, sid: set()
    )
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda path: None)
    gh_calls: list[float] = []

    def _list(cwd, timeout=15):
        gh_calls.append(timeout)
        return {"feature-branch": (PR_NUMBER, False)}

    monkeypatch.setattr(_worktrees_mod, "_list_my_open_prs", _list)

    past = time.monotonic() - 1.0
    owned = _worktrees_mod._collect_owned_worktrees(SID, str(tmp_path), 4242, past)

    assert gh_calls == [], "gh probe must be skipped once the budget is exhausted"
    assert owned == []


def test_collect_owned_caps_gh_timeout_to_remaining_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A near deadline must cap the gh subprocess timeout to the remaining budget
    (not the full 15s), bounding a single slow root."""
    import time

    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(
        _worktrees_mod,
        "_iter_worktrees_with_branch",
        lambda cwd: [(str(worktree), "feature-branch")],
    )
    monkeypatch.setattr(
        _worktrees_mod, "_live_independent_owner_paths", lambda paths, sid: set()
    )
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda path: None)
    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda path, sid: None)
    monkeypatch.setattr(
        _markers_mod, "_write_arm_marker", lambda *a, **k: True
    )
    gh_timeouts: list[float] = []

    def _list(cwd, timeout=15):
        gh_timeouts.append(timeout)
        return {"feature-branch": (PR_NUMBER, False)}

    monkeypatch.setattr(_worktrees_mod, "_list_my_open_prs", _list)

    near = time.monotonic() + 2.0  # ~2s of budget left
    _worktrees_mod._collect_owned_worktrees(SID, str(tmp_path), 4242, near)

    assert gh_timeouts, "gh probe should run while budget remains"
    assert gh_timeouts[0] <= 2.0 + 0.5, gh_timeouts
    assert gh_timeouts[0] < 15.0


def test_gh_pr_list_honors_sub_second_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #54 review round 2: a sub-second budget must be passed to the gh
    subprocess verbatim — no max(1.0, ...) floor that would overrun the deadline."""
    from agentic_pr_dash._maintenance import pr_state as _pr_state

    captured: dict[str, float] = {}

    class _Result:
        returncode = 0
        stdout = "[]"

    def _run(cmd, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _Result()

    monkeypatch.setattr(_pr_state.subprocess, "run", _run)

    _pr_state._list_my_open_prs("/tmp/x", timeout=0.3)

    assert captured["timeout"] == 0.3, "sub-second budget must not be floored to 1s"


def test_gh_pr_list_skips_subprocess_when_no_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-positive budget means no time left — skip the gh call entirely
    rather than spending any wall-clock past the deadline."""
    from agentic_pr_dash._maintenance import pr_state as _pr_state

    calls: list[int] = []
    monkeypatch.setattr(
        _pr_state.subprocess, "run", lambda *a, **k: calls.append(1)
    )

    out = _pr_state._list_my_open_prs("/tmp/x", timeout=0)

    assert calls == [], "no gh subprocess should spawn when the budget is gone"
    assert out == {}


def test_list_open_prs_retries_share_one_deadline(monkeypatch):
    from agentic_pr_dash import github_api
    from agentic_pr_dash._maintenance import pr_state as _pr_state

    clock = {"now": 100.0}
    timeouts: list[float] = []

    def _once(cmd, timeout_s=20, cwd=None, env=None):
        timeouts.append(timeout_s)
        clock["now"] += timeout_s
        return subprocess.CompletedProcess(cmd, 1, "", "error connecting to api.github.com")

    monkeypatch.setattr(github_api, "_run_once", _once)
    monkeypatch.setattr(github_api.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(github_api.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(github_api, "_GH_RETRY_BASE_DELAY_S", 0)
    _pr_state._list_my_open_prs("/tmp/x", timeout=0.3)
    assert sum(timeouts) == pytest.approx(0.3)


def test_stop_gate_exposes_reconcile_capability_sentinel() -> None:
    """gaia PR #2337 probes stop_gate.RECONCILES_BEFORE_RATE_LIMIT to confirm the
    package actually reconciles before the rate-limit (not just that a helper
    exists). The sentinel must be exposed and True now that _stop_gate_impl is
    wired to reconcile first (the behavior is proven by
    test_stop_gate_inspects_freshly_adopted_pr_within_rate_limit_window)."""
    assert _stop_gate_mod.RECONCILES_BEFORE_RATE_LIMIT is True


def test_stop_gate_rewrites_stale_marker_pr_within_rate_limit_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PR #2337 review (superset parity): a worktree we already marker-own whose
    marker ``pr`` points at a superseded PR — while the branch now has a DIFFERENT
    open non-draft PR — must be rewritten and inspected on the same Stop tick,
    even inside the clean-stop rate-limit window. This is the preflight behavior
    the package must subsume before the gaia wrapper can drop its preflight."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "180")
    config.load.cache_clear()

    worktree = tmp_path / "wt"
    worktree.mkdir()
    old_pr, new_pr = 111, 909
    marker = {"pr": str(old_pr), "session_id": SID}

    monkeypatch.setattr(
        _worktrees_mod,
        "_iter_worktrees_with_branch",
        lambda cwd: [(str(worktree), "feature-branch")],
    )
    monkeypatch.setattr(
        _worktrees_mod, "_live_independent_owner_paths", lambda paths, sid: set()
    )
    # Always ours (markered to SID); the marker's PR is what's stale.
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda path: SID)
    # Branch's CURRENT open non-draft PR (909) differs from the marker (111).
    monkeypatch.setattr(
        _worktrees_mod,
        "_list_my_open_prs",
        lambda cwd, timeout=15: {"feature-branch": (new_pr, False)},
    )
    monkeypatch.setattr(
        _markers_mod, "_read_marker", lambda path: dict(marker)
    )

    rewrites: list[int] = []

    def _rewrite(path, session_id, pid, pr_number):
        rewrites.append(int(pr_number))
        marker["pr"] = str(pr_number)  # stateful: the rewrite persists
        return True

    monkeypatch.setattr(_markers_mod, "_write_arm_marker", _rewrite)

    checked: list[str] = []

    def _check(path, session_id, *, claim=True):
        checked.append(str(Path(path)))
        return 10, f"pending review\nPR_NUMBER={new_pr}"

    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", _check)

    _seed_recent_clean_stop(str(tmp_path))

    rc = maintenance_check.main(
        ["stop-gate", "--cwd", str(tmp_path), "--session-id", SID]
    )

    assert rewrites == [new_pr], "stale marker PR must be rewritten to the current PR"
    assert checked == [str(worktree)], "rewritten worktree must be inspected this tick"
    assert rc == 2


def test_stop_gate_keeps_fresh_marker_pr_untouched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A marker whose ``pr`` already matches the branch's current PR is NOT
    rewritten (no spurious adoption / rate-limit bypass)."""
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "180")
    config.load.cache_clear()

    worktree = tmp_path / "wt"
    worktree.mkdir()
    cur_pr = 500

    monkeypatch.setattr(
        _worktrees_mod,
        "_iter_worktrees_with_branch",
        lambda cwd: [(str(worktree), "feature-branch")],
    )
    monkeypatch.setattr(
        _worktrees_mod, "_live_independent_owner_paths", lambda paths, sid: set()
    )
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda path: SID)
    monkeypatch.setattr(
        _worktrees_mod,
        "_list_my_open_prs",
        lambda cwd, timeout=15: {"feature-branch": (cur_pr, False)},
    )
    monkeypatch.setattr(
        _markers_mod, "_read_marker", lambda path: {"pr": str(cur_pr), "session_id": SID}
    )
    rewrites: list[int] = []
    monkeypatch.setattr(
        _markers_mod,
        "_write_arm_marker",
        lambda *a, **k: rewrites.append(1) or True,
    )
    checked: list[str] = []
    monkeypatch.setattr(
        _worktree_check_mod,
        "_check_worktree",
        lambda path, sid, *, claim=True: checked.append(path) or (0, "clean"),
    )
    _seed_recent_clean_stop(str(tmp_path))

    rc = maintenance_check.main(
        ["stop-gate", "--cwd", str(tmp_path), "--session-id", SID]
    )

    assert rewrites == [], "a current marker PR must not be rewritten"
    assert checked == [], "no rewrite → rate-limit still short-circuits this tick"
    assert rc == 0
