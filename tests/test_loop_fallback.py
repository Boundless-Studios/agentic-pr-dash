"""Executor-fallback chain for the maintenance loop (BOU-1734).

The detached loop dispatches a fix prompt to a primary executor (codex). When
that executor fails — e.g. codex is rate-limited and exits non-zero — the loop
must re-dispatch the SAME prompt to a configured fallback executor (claude) on
the same tick, and only report an error when BOTH fail. Before this change the
loop logged "leaving PR for next tick" and serviced nothing, indefinitely.

These tests drive ``loop._tick`` with fake primary/fallback executors and
stubbed check/complete subprocesses, asserting observable behavior: which
executor ran, whether ``complete`` ran, and the coordinator claim-release reason.
"""

import types

from agentic_pr_dash import loop

PRIMARY = "codex exec --full-auto {prompt}"
FALLBACK = "claude --dangerously-skip-permissions -p {prompt}"


def _args(**kw):
    base = dict(no_discover_worktrees=False, session_id="sess", cwd=["/fallback"],
                fallback_executor=FALLBACK)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _wire(monkeypatch, worktree, *, run_executor):
    """Common stubs: one worktree, check finds work, complete/release recorded."""
    calls = []

    def fake_run(cmd, *a, **k):
        if cmd[:3] == [loop.sys.executable, "-m", "agentic_pr_dash"] and "check" in cmd:
            return types.SimpleNamespace(
                returncode=loop.CHECK_WORK_FOUND,
                stdout=(
                    "fix prompt\nPR_NUMBER=7\n"
                    "COORDINATOR_CLAIM_ID=claim-1\n"
                    "COORDINATOR_LEASE_EPOCH=4\n"
                ),
                stderr="",
            )
        if cmd[:3] == [loop.sys.executable, "-m", "agentic_pr_dash"] and "complete" in cmd:
            calls.append(("complete", cmd))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    monkeypatch.setattr(loop, "_discover_cwds", lambda args: [str(worktree)])
    monkeypatch.setattr(loop, "_cleanup_stale_no_pr_worktree", lambda cwd, session_id="": False)
    monkeypatch.setattr(loop, "_baseline_sha", lambda cwd, pr: "base-sha")
    # Streak files are repo-scoped; stub the (gh-backed) slug so this dispatch
    # test doesn't shell out to resolve the repo name.
    monkeypatch.setattr(loop, "_repo_slug", lambda cwd: "testrepo")
    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    monkeypatch.setattr(loop, "_run_executor",
                        lambda executor, prompt, cwd: run_executor(executor, calls))
    monkeypatch.setattr(loop.coordinator, "heartbeat_claim",
                        lambda handle, session_id: None)
    monkeypatch.setattr(loop.coordinator, "release_claim",
                        lambda handle, session_id, reason: calls.append(
                            ("release", handle.claim_id, handle.lease_epoch, reason)
                        ))
    return calls


def test_primary_success_never_runs_fallback(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"; worktree.mkdir()

    def run_executor(executor, calls):
        calls.append(("executor", executor))
        return 0  # codex succeeds

    calls = _wire(monkeypatch, worktree, run_executor=run_executor)
    loop._tick(_args(cwd=["/repo"], session_id="sess-1"), PRIMARY)

    executors_run = [entry[1] for entry in calls if entry[0] == "executor"]
    assert executors_run == [PRIMARY]                 # only the primary ran
    assert any(tag == "complete" for tag, *_ in calls)
    assert ("release", "claim-1", 4, "completed") in calls


def test_primary_fails_fallback_succeeds_services_pr(monkeypatch, tmp_path):
    worktree = tmp_path / "wt"; worktree.mkdir()

    def run_executor(executor, calls):
        calls.append(("executor", executor))
        return 1 if executor == PRIMARY else 0  # codex fails, claude succeeds

    calls = _wire(monkeypatch, worktree, run_executor=run_executor)
    loop._tick(_args(cwd=["/repo"], session_id="sess-1"), PRIMARY)

    executors_run = [entry[1] for entry in calls if entry[0] == "executor"]
    assert executors_run == [PRIMARY, FALLBACK]       # fell back to claude
    assert any(tag == "complete" for tag, *_ in calls)  # PR serviced via fallback
    assert ("release", "claim-1", 4, "completed") in calls


def test_both_executors_fail_reports_error_and_skips_complete(monkeypatch, tmp_path, capsys):
    worktree = tmp_path / "wt"; worktree.mkdir()

    def run_executor(executor, calls):
        calls.append(("executor", executor))
        return 1  # both codex and claude fail

    calls = _wire(monkeypatch, worktree, run_executor=run_executor)
    loop._tick(_args(cwd=["/repo"], session_id="sess-1"), PRIMARY)

    executors_run = [entry[1] for entry in calls if entry[0] == "executor"]
    assert executors_run == [PRIMARY, FALLBACK]       # both attempted
    assert not any(tag == "complete" for tag, *_ in calls)  # PR NOT serviced
    assert ("release", "claim-1", 4, "all_executors_failed") in calls
    err = capsys.readouterr().err
    assert "ERROR" in err and "PR #7" in err          # error reported, not silent
