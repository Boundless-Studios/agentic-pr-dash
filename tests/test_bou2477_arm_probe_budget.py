"""BOU-2477: arm's gh-probe retries must stay inside the Stop-hook preflight
budget.

`_gh_pr_view_field` retried up to `_PR_VIEW_ATTEMPTS` times at a 15s
per-attempt subprocess timeout, with NO caller-supplied deadline. When `gh`
hangs rather than fails fast, one field probe could burn ~46.5s and `arm`
makes TWO independent probes (isDraft, headRefName) — each with its own fresh
budget — for a worst case of ~93s against the documented "arm runs inside a
Stop hook with a ~10s preflight budget" constraint. A Stop hook that kills
`arm` mid-probe gets none of BOU-2406's diagnostics: no exit code, no arm
marker, no ledger entry — the PR is silently uncovered.

Fix: thread ONE shared deadline from the `arm` entrypoint through both probes,
so the total (not each probe individually) stays inside budget, and a
budget-exhausted result is distinguishable from "gh unavailable".
"""
from __future__ import annotations

import time

import pytest

from agentic_pr_dash._maintenance import pr_state


class _HangingResult:
    """Simulates `gh` hanging past its subprocess timeout on every attempt."""


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(pr_state, "_PR_VIEW_BACKOFF_SECONDS", 0)


def _install_hanging_gh(monkeypatch, *, sleep_per_call: float) -> list[float]:
    """Every subprocess.run call sleeps `sleep_per_call` real seconds then
    raises TimeoutExpired, exactly like a `gh` call that hangs past its
    per-attempt timeout. Returns the list of per-call elapsed times."""
    elapsed: list[float] = []

    def fake_run(cmd, *, timeout, **kwargs):
        start = time.monotonic()
        time.sleep(min(sleep_per_call, timeout))
        elapsed.append(time.monotonic() - start)
        raise pr_state.subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(pr_state.subprocess, "run", fake_run)
    return elapsed


def test_gh_pr_view_field_honors_a_caller_supplied_deadline(monkeypatch):
    """A hanging `gh` must not be retried past the caller's remaining budget."""
    _install_hanging_gh(monkeypatch, sleep_per_call=0.15)

    start = time.monotonic()
    deadline = start + 0.2
    value, diagnostic = pr_state._gh_pr_view_field(
        "/repo", 1, "isDraft", deadline=deadline
    )
    total = time.monotonic() - start

    assert value is pr_state._GH_UNAVAILABLE
    assert total < 1.0, f"took {total}s against a 0.2s budget — not bounded"
    assert "budget" in diagnostic.lower(), (
        f"a budget-exhausted result must be distinguishable from a plain gh "
        f"failure so it doesn't read as 'gh unavailable' (BOU-2477); got {diagnostic!r}"
    )


def test_full_arm_stays_inside_a_shared_budget_across_both_probes(monkeypatch, tmp_path):
    """`arm`'s two probes (isDraft, headRefName) must draw down ONE shared
    budget, not each get a fresh one — the documented failure mode is ~93s
    (2 independent 46.5s budgets) against a ~10s Stop-hook deadline.

    The first probe (isDraft) resolves quickly (non-draft); the SECOND probe
    (headRefName) is the one that hangs — the realistic shape, since `arm`
    never reaches the second probe if the first one fails outright.
    """
    from agentic_pr_dash import maintenance_check as mc

    class _Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, *, timeout, **kwargs):
        if "isDraft" in cmd:
            return _Result(returncode=0, stdout='{"isDraft": false}')
        time.sleep(min(1.0, timeout))
        raise pr_state.subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(pr_state.subprocess, "run", fake_run)
    monkeypatch.setenv("AGENTIC_PR_DASH_ARM_GH_PROBE_BUDGET_SECONDS", "0.3")

    start = time.monotonic()
    rc = mc.main([
        "arm", "--cwd", str(tmp_path), "--session-id", "sess", "--pid", "123",
        "--pr", "42",
    ])
    total = time.monotonic() - start

    assert rc == 1
    assert total < 2.0, (
        f"a full arm (both probes) took {total}s against a shared ~0.3s "
        f"budget — the budget is not actually shared across both probes "
        f"(BOU-2477)"
    )


def test_bou2406_regression_first_probe_fails_second_succeeds_still_arms(
    monkeypatch, tmp_path,
):
    """Regression: the BOU-2406 case (first gh attempt fails, a later one
    succeeds) must still arm once a budget is threaded in."""
    from agentic_pr_dash import maintenance_check as mc

    calls = {"isDraft": 0, "headRefName": 0}

    class _Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(cmd, *, timeout, **kwargs):
        field = "isDraft" if "isDraft" in cmd else "headRefName"
        calls[field] += 1
        if calls[field] < 2:
            return _Result(returncode=1, stderr="could not resolve to a PullRequest")
        if field == "isDraft":
            return _Result(returncode=0, stdout='{"isDraft": false}')
        return _Result(returncode=0, stdout='{"headRefName": "my-branch"}')

    monkeypatch.setattr(pr_state.subprocess, "run", fake_run)
    monkeypatch.setattr(mc, "_current_branch", lambda cwd: "my-branch")

    rc = mc.main([
        "arm", "--cwd", str(tmp_path), "--session-id", "sess", "--pid", "123",
        "--pr", "42",
    ])
    assert rc == 0, "a transient-then-successful gh probe must still arm (BOU-2406)"
