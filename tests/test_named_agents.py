"""Named agent sessions + the conflict/worktree fixes that ride along.

Covers three independent additions:
  1. session_registry persists/propagates an ``agent_name`` per session.
  2. orchestrator flags a merge conflict from EITHER GitHub signal
     (mergeStateStatus==DIRTY or mergeable==CONFLICTING), not DIRTY alone.
  3. app associates an "unassigned" PR (no live checkout of its head branch) to
     the live session working that branch, so the card shows the agent + its
     worktree instead of "No worktree".
"""

from __future__ import annotations

from agentic_pr_dash import app, session_registry
from agentic_pr_dash.models import PRData, PRStatus
from agentic_pr_dash.orchestrator import Orchestrator
from agentic_pr_dash.session_registry import RuntimeSessionState


# ── 1. agent_name roundtrip ──────────────────────────────────────────────────

def test_agent_name_roundtrips_through_registry(tmp_path):
    registry = tmp_path / "events.jsonl"
    session_registry.record_event(
        event="started",
        session_id="gaia-1",
        cli="claude",
        agent_name="brave-otter",
        worktree_path=str(tmp_path / "wt"),
        branch="feature/x",
        path=registry,
    )
    summary = session_registry.summarize_sessions(path=registry)
    assert summary.sessions["gaia-1"].agent_name == "brave-otter"


def test_agent_name_is_sticky_across_later_events(tmp_path):
    """A later event that omits agent_name must not clear it (matches cli/bead_id)."""
    registry = tmp_path / "events.jsonl"
    session_registry.record_event(
        event="started", session_id="gaia-1", cli="codex",
        agent_name="silver-lynx", branch="b", path=registry,
    )
    session_registry.record_event(
        event="heartbeat", session_id="gaia-1", cli="codex", branch="b", path=registry,
    )
    summary = session_registry.summarize_sessions(path=registry)
    assert summary.sessions["gaia-1"].agent_name == "silver-lynx"


def test_agent_name_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_AGENT_NAME", "golden-raven")
    registry = tmp_path / "events.jsonl"
    event = session_registry.record_event(
        event="started", session_id="gaia-1", path=registry,
    )
    assert event["agent_name"] == "golden-raven"


# ── 2. merge-conflict detection from either signal ───────────────────────────

def _pr(**kw) -> PRData:
    base = dict(number=1, title="t", branch="b", url="u")
    base.update(kw)
    return PRData(**base)


def test_conflict_from_mergeable_field_even_when_state_unstable():
    orch = Orchestrator(repo_cwd=None)
    pr = _pr(merge_state="UNSTABLE", mergeable="CONFLICTING")
    assert orch._compute_status(pr) is PRStatus.MERGE_CONFLICT


def test_conflict_from_dirty_state_still_detected():
    orch = Orchestrator(repo_cwd=None)
    pr = _pr(merge_state="DIRTY", mergeable="UNKNOWN")
    assert orch._compute_status(pr) is PRStatus.MERGE_CONFLICT


def test_clean_pr_is_not_a_conflict():
    orch = Orchestrator(repo_cwd=None)
    pr = _pr(merge_state="UNSTABLE", mergeable="MERGEABLE")
    # UNSTABLE = a non-required check pending/failing, not a conflict.
    assert orch._compute_status(pr) is not PRStatus.MERGE_CONFLICT


# ── 3. branch-matched session association (fixes "No worktree") ───────────────

def _session(**kw) -> RuntimeSessionState:
    base = dict(session_id="gaia-1", event="started", timestamp="2026-06-11T00:00:00Z")
    base.update(kw)
    return RuntimeSessionState(**base)


def test_live_session_for_branch_matches_live_session(monkeypatch):
    monkeypatch.setattr(session_registry, "pid_is_live", lambda pid: True)
    summary = session_registry.SessionSummary(
        sessions={"gaia-1": _session(branch="feature/x", pid=4242,
                                      worktree_path="/w/foo", agent_name="brave-otter")}
    )
    found = app._live_session_for_branch("feature/x", summary)
    assert found is not None and found.agent_name == "brave-otter"


def test_live_session_for_branch_ignores_dead_and_terminal(monkeypatch):
    monkeypatch.setattr(session_registry, "pid_is_live", lambda pid: False)
    summary = session_registry.SessionSummary(
        sessions={"gaia-1": _session(branch="feature/x", pid=4242, worktree_path="/w/foo")}
    )
    assert app._live_session_for_branch("feature/x", summary) is None


def test_unassigned_pr_card_attributes_to_session_worktree(monkeypatch):
    """An unassigned PR with a live branch session shows the agent + worktree,
    not 'No worktree', and reads as agent-working."""
    monkeypatch.setattr(app.orchestrator, "_inflight_prs", set(), raising=False)
    session = _session(cli="claude", agent_name="brave-otter",
                       worktree_path="/w/foo", branch="b", pid=4242)
    card = app._build_unassigned_pr_card(
        _pr(),
        active_agents=[],
        runtime_session=session,
        session_worktree_path="/w/foo",
    )
    assert card.agent_name == "brave-otter"
    assert card.worktree_name == "foo"
    assert card.worktree_path == "/w/foo"
    assert card.status is PRStatus.AGENT_WORKING


def test_unassigned_pr_card_without_session_says_no_worktree(monkeypatch):
    monkeypatch.setattr(app.orchestrator, "_inflight_prs", set(), raising=False)
    card = app._build_unassigned_pr_card(_pr(), active_agents=[])
    assert card.worktree_name == "No worktree"
    assert card.agent_name is None


def test_live_session_for_branch_scopes_to_allowed_worktrees(monkeypatch):
    """A same-named branch in an unrelated repo must not hijack the card."""
    monkeypatch.setattr(session_registry, "pid_is_live", lambda pid: True)
    summary = session_registry.SessionSummary(
        sessions={"gaia-1": _session(branch="feature/x", pid=4242,
                                      worktree_path="/other/repo/wt", agent_name="brave-otter")}
    )
    # The session's worktree isn't in this repo's allowed set -> no match.
    assert app._live_session_for_branch(
        "feature/x", summary, allowed_worktree_paths={"/this/repo/wt"}
    ) is None
    # ...but it matches when its worktree is allowed.
    assert app._live_session_for_branch(
        "feature/x", summary, allowed_worktree_paths={"/other/repo/wt"}
    ) is not None


def test_hidden_agent_worktree_card_is_non_navigable(monkeypatch):
    """A hidden agent worktree keeps worktree_path None so the card can't be focused."""
    monkeypatch.setattr(app.orchestrator, "_inflight_prs", set(), raising=False)
    session = _session(cli="claude", agent_name="brave-otter",
                       worktree_path="/w/agent-hidden", branch="b", pid=4242)
    card = app._build_unassigned_pr_card(
        _pr(),
        active_agents=[],
        worktree_hidden=True,
        runtime_session=session,
        session_worktree_path="/w/agent-hidden",
    )
    assert card.worktree_path is None
    assert card.worktree_name == "Agent worktree hidden"


def test_repo_session_summary_unions_custom_registries(tmp_path, monkeypatch):
    """A worktree with a custom registry contributes its sessions to attribution."""
    default = session_registry.SessionSummary(
        sessions={"gaia-default": _session(session_id="gaia-default", branch="a")}
    )
    custom_reg = tmp_path / "custom.jsonl"
    session_registry.record_event(
        event="started", session_id="gaia-custom", cli="codex",
        agent_name="silver-lynx", branch="b", worktree_path="/w/custom", path=custom_reg,
    )
    # Worktree /w/custom resolves its registry to custom_reg; default resolves elsewhere.
    def fake_registry_path(cwd=None):
        return custom_reg if cwd == "/w/custom" else (tmp_path / "default.jsonl")
    monkeypatch.setattr(session_registry, "registry_path", fake_registry_path)
    merged = app._repo_session_summary([{"path": "/w/custom"}], default)
    assert "gaia-default" in merged.sessions
    assert merged.sessions["gaia-custom"].agent_name == "silver-lynx"


def test_maintenance_blockers_detect_mergeable_conflict():
    from agentic_pr_dash.maintenance import blockers_for_pr
    pr = _pr(merge_state="UNSTABLE", mergeable="CONFLICTING")
    assert "merge_conflict" in blockers_for_pr(pr)


def test_known_conflict_preserved_when_bulk_unknown_and_refetch_fails(monkeypatch):
    """A CONFLICTING PR must not slide back to Clean when the next bulk poll
    returns UNKNOWN and the per-PR refetch then fails."""
    import asyncio
    from agentic_pr_dash import orchestrator, github_api

    bulk = {
        "number": 1, "title": "t", "headRefName": "b", "baseRefName": "main",
        "url": "u", "isDraft": False, "labels": [],
        "mergeStateStatus": "UNSTABLE", "mergeable": "CONFLICTING",
    }
    monkeypatch.setattr(github_api, "list_open_prs", lambda cwd=None: [bulk])
    monkeypatch.setattr(orchestrator, "find_worktree_for_branch", lambda branch: None)
    monkeypatch.setattr(github_api, "get_weekly_runner_execution_summary", lambda cwd=None: None)
    monkeypatch.setattr(github_api, "get_latest_commit", lambda n, cwd=None: ("sha", "2026-06-11T12:00:00Z"))
    monkeypatch.setattr(github_api, "get_ci_checks", lambda n, cwd=None: [])
    monkeypatch.setattr(github_api, "get_unaddressed_comments", lambda n, d, cwd=None: [])
    monkeypatch.setattr(github_api, "get_mergeability", lambda n, cwd=None: ("", ""))
    monkeypatch.setattr(asyncio, "create_task", lambda coro: coro.close())

    orch = orchestrator.Orchestrator(repo_cwd=None)

    asyncio.run(orch.refresh_prs())
    assert orch.get_pr(1).mergeable == "CONFLICTING"
    assert orch.get_pr(1).status is PRStatus.MERGE_CONFLICT

    # Next poll: bulk now reports UNKNOWN, and the per-PR refetch fails ("","").
    bulk["mergeStateStatus"] = "UNKNOWN"
    bulk["mergeable"] = "UNKNOWN"
    asyncio.run(orch.refresh_prs())
    assert orch.get_pr(1).mergeable == "CONFLICTING"  # preserved, not erased
    assert orch.get_pr(1).status is PRStatus.MERGE_CONFLICT
