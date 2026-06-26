"""BOU-1783: the passive Stop-gate probe must honor a LIVE active agent-
coordinator claim (defer, exit 0) the same way the active ``check`` does, while
still surfacing work when the claim is dead/expired (BOU-1789), and never
writing a claim itself. Plus a regression lock on per-worktree prompt alignment.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_coordinator.service import TaskCoordinator
from agent_coordinator.store import JsonlClaimStore

from agentic_pr_dash import coordinator, maintenance_check
from agentic_pr_dash.models import PRData, PRStatus, ReviewComment
from agentic_pr_dash._maintenance import markers as _markers_mod
from agentic_pr_dash._maintenance import pr_state as _pr_state_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash._maintenance import stop_gate as _stop_gate_mod


SID = "sess-in-session"        # the idle interactive session running the stop-gate
LOOP_SID = "sess-loop-owner"   # the detached loop that holds the active claim
LOOP_PID = 1234                # the loop owner's pid (liveness is stubbed below)


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")


def _blocked_pr(worktree: Path) -> PRData:
    return PRData(
        number=42,
        title="needs review",
        branch="feature/x",
        url="https://github.com/Boundless-Studios/gaia-free/pull/42",
        worktree_path=str(worktree),
        status=PRStatus.HAS_COMMENTS,
        review_comments=[
            ReviewComment(id=7, author="r", body="fix", created_at="2026-06-11T12:00:00Z")
        ],
    )


def _stub_gauntlet(monkeypatch: pytest.MonkeyPatch, pr: PRData) -> None:
    """Stub the gh boundary so _check_worktree reaches the claim decision with
    blockers present — but DO NOT stub the coordinator (the layer under test)."""
    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: pr)
    monkeypatch.setattr(_pr_state_mod, "_unresolved_review_threads", lambda n, c: [])
    monkeypatch.setattr(_worktrees_mod, "_live_independent_owner_paths", lambda paths, sid: set())
    monkeypatch.setattr(_markers_mod, "_touch_owner_heartbeat", lambda cwd, sid, work: None)


def _use_deterministic_liveness(
    monkeypatch: pytest.MonkeyPatch, store: Path, live_pids: set[int]
) -> None:
    """Point the coordinator at ``store`` with a deterministic pid_is_live so the
    LIVE vs DEAD claim distinction doesn't depend on real OS pids."""
    monkeypatch.setattr(
        coordinator,
        "_coordinator",
        lambda: TaskCoordinator(JsonlClaimStore(store), pid_is_live=lambda pid: pid in live_pids),
    )


def test_passive_probe_defers_to_live_foreign_coordinator_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Live loop claim on a blocked owned PR → passive stop-gate probe DEFERS
    (exit 0), instead of re-surfacing the work the loop is already handling."""
    store = tmp_path / "claims.jsonl"
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(store))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _blocked_pr(worktree)
    _stub_gauntlet(monkeypatch, pr)
    _use_deterministic_liveness(monkeypatch, store, live_pids={LOOP_PID})

    claimed = coordinator.claim_pr(
        pr, session_id=LOOP_SID, pid=LOOP_PID, agent="loop", lease_seconds=600
    )
    assert claimed is not None

    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=False)

    assert code == 0, f"expected defer (0), got {code}: {text!r}"
    assert "deferring" in text.lower()
    assert "COORDINATOR_CLAIM_ID" not in text


def test_passive_probe_surfaces_when_claim_is_dead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Dead/expired claim must NOT hide the work (BOU-1789 'pid-alive ≠
    working') — the passive probe still surfaces it (exit 10)."""
    store = tmp_path / "claims.jsonl"
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(store))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _blocked_pr(worktree)
    _stub_gauntlet(monkeypatch, pr)
    # LOOP_PID is NOT in the live set → the claim owner reads as dead.
    _use_deterministic_liveness(monkeypatch, store, live_pids=set())

    claimed = coordinator.claim_pr(
        pr, session_id=LOOP_SID, pid=LOOP_PID, agent="loop", lease_seconds=600
    )
    assert claimed is not None

    code, text = maintenance_check._check_worktree(str(worktree), SID, claim=False)

    assert code == 10, f"expected surface (10), got {code}: {text!r}"
    assert "PR_NUMBER=42" in text


def test_passive_probe_never_writes_claim_when_deferring(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Deferring to a live claim is READ-ONLY — the passive probe must not append
    a claim/heartbeat to the store (a stop-gate-created claim would survive the
    idle session and suppress the work on the next Stop)."""
    store = tmp_path / "claims.jsonl"
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(store))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    pr = _blocked_pr(worktree)
    _stub_gauntlet(monkeypatch, pr)
    _use_deterministic_liveness(monkeypatch, store, live_pids={LOOP_PID})
    coordinator.claim_pr(pr, session_id=LOOP_SID, pid=LOOP_PID, agent="loop", lease_seconds=600)

    before = store.read_text()
    code, _ = maintenance_check._check_worktree(str(worktree), SID, claim=False)
    after = store.read_text()

    assert code == 0
    assert before == after, "passive probe wrote to the coordinator store"


def test_build_stop_block_aligns_header_pr_and_cwd_per_worktree() -> None:
    """BOU-1783 bug #2 regression lock: each worktree's header must stay aligned
    with its OWN PR_NUMBER and --cwd completion command — no cross-worktree
    bleed (header PR #2315 / body PR_NUMBER=2290 --cwd revert-recent-audio)."""
    pending = [
        ("/wt/bou-1779", "maint prompt A\nPR_NUMBER=2315"),
        ("/wt/revert-recent-audio", "maint prompt B\nPR_NUMBER=2290"),
    ]
    out = _stop_gate_mod._build_stop_block(pending)

    a = out.index("/wt/bou-1779")
    b = out.index("/wt/revert-recent-audio")
    assert a < b
    seg_a, seg_b = out[a:b], out[b:]

    assert "--pr 2315" in seg_a and "--cwd /wt/bou-1779" in seg_a
    assert "--pr 2290" not in seg_a
    assert "--pr 2290" in seg_b and "--cwd /wt/revert-recent-audio" in seg_b
    assert "--pr 2315" not in seg_b
