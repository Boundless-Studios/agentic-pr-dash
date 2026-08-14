"""BOU-2933 — worktree reaping must gate on liveness, not on CPU activity.

``_cleanup_stale_no_pr_worktree`` (the loop) and ``POST /api/cleanup-worktree``
(the dashboard) both asked ``discover_active_agents`` whether anyone was home
before running ``git worktree remove --force``. That function is the DASHBOARD's
"who is busy right now" query: it skips any process under ``_ACTIVE_CPU_THRESHOLD``
(1.0%), and its own docstring admits "an idle REPL at a prompt will sample near
zero". So an idle — or SIGSTOP'd — agent was invisible to the guard and its
checkout got destroyed underneath it. Because codex's log DB is shared
machine-wide, the resulting wedged process then took ``codex`` down in every
worktree on the box.

Two distinct fail-open bugs, both from reusing a display heuristic as a
destructive-action safety gate:

  1. The CPU floor hides idle/stopped occupants.
  2. ``discover_active_agents`` returns ``{}`` when the ``ps``/``lsof`` scan
     fails. For a display that means "show nothing"; for a gate it means
     "nobody's home, delete it" — failing OPEN on its own blindness.

The fix is ``worktree_occupants``: no CPU floor, no CLI allow-list (a stray
pytest or a user's shell living in the tree is also a reason not to nuke it),
and it RAISES rather than returning empty when it cannot see.
"""

from __future__ import annotations

import types

import pytest

from agentic_pr_dash import agents, app as app_module, loop, worktrees
from agentic_pr_dash.agents import ProcessScanUnavailable


# `ps -axo pid=,ppid=,%cpu=,command=` rows.
_PS_IDLE_CODEX = "4242 4200  0.0 /opt/homebrew/bin/codex --sandbox danger-full-access\n"
_PS_BUSY_CODEX = "4242 4200 37.5 /opt/homebrew/bin/codex --sandbox danger-full-access\n"
_PS_ORPHAN_PYTEST = "7777 1  0.0 /wt/.venv/bin/python -m pytest backend/test/unit\n"


def _scan(monkeypatch, ps_table: str, cwds: dict[int, str]) -> None:
    monkeypatch.setattr(agents, "_run_process_table", lambda: ps_table)
    monkeypatch.setattr(agents, "_collect_cwds", lambda: cwds)


# ---------------------------------------------------------------------------
# The occupancy scan itself
# ---------------------------------------------------------------------------

def test_idle_agent_is_invisible_to_the_display_query(monkeypatch):
    """Pins the fail-open behaviour the guard used to inherit."""
    _scan(monkeypatch, _PS_IDLE_CODEX, {4242: "/wt"})

    assert agents.discover_active_agents(["/wt"]) == {}


def test_idle_agent_is_an_occupant(monkeypatch):
    """AC #1 — a 0.0%-CPU agent still owns its worktree."""
    _scan(monkeypatch, _PS_IDLE_CODEX, {4242: "/wt"})

    occupants = agents.worktree_occupants(["/wt"])

    assert [p.pid for p in occupants["/wt"]] == [4242]


def test_busy_agent_is_still_an_occupant(monkeypatch):
    _scan(monkeypatch, _PS_BUSY_CODEX, {4242: "/wt"})

    assert [p.pid for p in agents.worktree_occupants(["/wt"])["/wt"]] == [4242]


def test_non_agent_process_is_an_occupant(monkeypatch):
    """AC #2 — an orphaned pytest in the tree also blocks the reap.

    The incident left a stopped pytest running out of a `.venv` inside the
    destroyed checkout. A CLI allow-list would not have seen it.
    """
    _scan(monkeypatch, _PS_ORPHAN_PYTEST, {7777: "/wt"})

    assert [p.pid for p in agents.worktree_occupants(["/wt"])["/wt"]] == [7777]


def test_occupant_in_a_subdirectory_counts(monkeypatch):
    _scan(monkeypatch, _PS_IDLE_CODEX, {4242: "/wt/backend/test"})

    assert [p.pid for p in agents.worktree_occupants(["/wt"])["/wt"]] == [4242]


def test_process_outside_the_worktree_is_not_an_occupant(monkeypatch):
    """A sibling worktree's agent must not pin an unrelated tree."""
    _scan(monkeypatch, _PS_IDLE_CODEX, {4242: "/other-wt"})

    assert agents.worktree_occupants(["/wt"]) == {}


def test_prefix_collision_is_not_an_occupant(monkeypatch):
    """`/wt-2` must not read as living inside `/wt`."""
    _scan(monkeypatch, _PS_IDLE_CODEX, {4242: "/wt-2"})

    assert agents.worktree_occupants(["/wt"]) == {}


# ---------------------------------------------------------------------------
# Fail-closed on a blind scan
# ---------------------------------------------------------------------------

def test_raises_when_the_process_table_is_unavailable(monkeypatch):
    """AC #3 — `ps` failing is not evidence that nobody is home."""
    _scan(monkeypatch, "", {4242: "/wt"})

    with pytest.raises(ProcessScanUnavailable):
        agents.worktree_occupants(["/wt"])


def test_raises_when_the_cwd_scan_is_unavailable(monkeypatch):
    """AC #3 — neither is `lsof` failing."""
    _scan(monkeypatch, _PS_IDLE_CODEX, {})

    with pytest.raises(ProcessScanUnavailable):
        agents.worktree_occupants(["/wt"])


def test_no_paths_is_not_a_scan_failure(monkeypatch):
    _scan(monkeypatch, "", {})

    assert agents.worktree_occupants([]) == {}


# ---------------------------------------------------------------------------
# The display query keeps its old semantics
# ---------------------------------------------------------------------------

def test_display_query_still_gates_on_cpu(monkeypatch):
    """The dashboard's "who's busy now" column must not become "who exists"."""
    _scan(monkeypatch, _PS_BUSY_CODEX + _PS_IDLE_CODEX.replace("4242 4200", "4243 4200"),
          {4242: "/wt", 4243: "/wt"})

    active = agents.discover_active_agents(["/wt"])

    assert [p.pid for p in active["/wt"]] == [4242]


def test_display_query_can_opt_into_liveness(monkeypatch):
    """`min_cpu=0.0` is the ownership/liveness reading (BOU-1540 precedent)."""
    _scan(monkeypatch, _PS_IDLE_CODEX, {4242: "/wt"})

    assert [p.pid for p in agents.discover_active_agents(["/wt"], min_cpu=0.0)["/wt"]] == [4242]


# ---------------------------------------------------------------------------
# The loop's destructive path
# ---------------------------------------------------------------------------

def _arm_cleanup(monkeypatch, tmp_path, removed: list[str]):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(loop, "find_worktree_for_path", lambda cwd: {"path": cwd, "branch": "b"})
    monkeypatch.setattr(loop, "_live_independent_owner_paths", lambda paths, session_id: set())
    monkeypatch.setattr(
        loop, "selected_worktree_cleanup_reason",
        lambda worktree, active_agents: (not active_agents, "stale orphan"),
    )
    monkeypatch.setattr(loop, "remove_worktree", lambda cwd: (removed.append(cwd), (True, ""))[1])
    return worktree


def test_loop_refuses_to_reap_a_worktree_with_an_idle_agent(monkeypatch, tmp_path):
    """The BOU-2933 repro: continue-2707 with a SIGSTOP'd codex sitting in it."""
    removed: list[str] = []
    worktree = _arm_cleanup(monkeypatch, tmp_path, removed)
    _scan(monkeypatch, _PS_IDLE_CODEX, {4242: str(worktree)})

    assert loop._cleanup_stale_no_pr_worktree(str(worktree), "loop-session") is False
    assert removed == []


def test_loop_fails_closed_when_it_cannot_see_processes(monkeypatch, tmp_path):
    removed: list[str] = []
    worktree = _arm_cleanup(monkeypatch, tmp_path, removed)
    _scan(monkeypatch, "", {})

    assert loop._cleanup_stale_no_pr_worktree(str(worktree), "loop-session") is False
    assert removed == []


def test_loop_still_reaps_a_genuinely_empty_worktree(monkeypatch, tmp_path):
    """The guard must stay useful — an unoccupied stale tree is still reclaimed."""
    removed: list[str] = []
    worktree = _arm_cleanup(monkeypatch, tmp_path, removed)
    _scan(monkeypatch, _PS_IDLE_CODEX, {4242: "/somewhere-else"})

    assert loop._cleanup_stale_no_pr_worktree(str(worktree), "loop-session") is True
    assert removed == [str(worktree)]


# ---------------------------------------------------------------------------
# The dashboard's destructive endpoint
# ---------------------------------------------------------------------------

def _post_cleanup(path: str):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app).post("/api/cleanup-worktree", data={"path": path})


def _arm_endpoint(monkeypatch, path: str) -> None:
    monkeypatch.setattr(app_module, "discover_worktrees", lambda: [{"path": path, "branch": "b"}])
    monkeypatch.setattr(app_module.orchestrator, "prs", {}, raising=False)
    monkeypatch.setattr(
        app_module, "_selected_worktree_cleanup_reason",
        lambda worktree, agents_, **kw: (True, "stale orphan"),
    )
    monkeypatch.setattr(
        app_module, "remove_worktree",
        lambda p: pytest.fail(f"endpoint must not remove an occupied worktree: {p}"),
    )


def test_endpoint_refuses_when_an_idle_agent_occupies_the_worktree(monkeypatch):
    _arm_endpoint(monkeypatch, "/wt")
    _scan(monkeypatch, _PS_IDLE_CODEX, {4242: "/wt"})

    response = _post_cleanup("/wt")

    assert response.status_code == 409
    assert "worktree in use by" in response.text
    assert "4242" in response.text


def test_endpoint_fails_closed_when_it_cannot_see_processes(monkeypatch):
    _arm_endpoint(monkeypatch, "/wt")
    _scan(monkeypatch, "", {})

    response = _post_cleanup("/wt")

    assert response.status_code == 409
    assert "process scan unavailable" in response.text


# ---------------------------------------------------------------------------
# remove_worktree's post-check
# ---------------------------------------------------------------------------

def test_successful_removal_is_not_reported_as_failure_when_scaffolding_returns(
    monkeypatch, tmp_path
):
    """The second trap from the incident.

    `git worktree remove --force` succeeds, then the agent-session guardian
    recreates `.agent-session-harness/` and `.gaia/` under the dead path. The
    old `Path(path).exists()` post-check saw a directory and reported
    "selected worktree still exists" — AFTER the checkout was already gone. The
    log read as "nothing happened" and the entry retried forever.
    """
    path = tmp_path / "wt"
    path.mkdir()
    (path / ".agent-session-harness").mkdir()

    monkeypatch.setattr(worktrees, "get_main_repo_root", lambda p: "/main")
    monkeypatch.setattr(
        worktrees.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(worktrees, "_worktree_is_registered", lambda p: False)

    assert worktrees.remove_worktree(str(path)) == (True, "")


def test_removal_that_left_the_worktree_registered_is_a_failure(monkeypatch, tmp_path):
    path = tmp_path / "wt"
    path.mkdir()

    monkeypatch.setattr(worktrees, "get_main_repo_root", lambda p: "/main")
    monkeypatch.setattr(
        worktrees.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(worktrees, "_worktree_is_registered", lambda p: True)

    removed, detail = worktrees.remove_worktree(str(path))

    assert removed is False
    assert "still registered" in detail
