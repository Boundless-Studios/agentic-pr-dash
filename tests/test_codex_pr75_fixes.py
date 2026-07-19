"""Regression tests for the codex PR #75 review findings (BOU-1962 hardening).

1. A failed CI-watch probe must NOT read as "CI terminal": the clean-state
   early exit needs a positive terminal-CI observation
   (``github_api.checks_probe_failure_seen()``).
2. A warn-only deferral (``NOT clean (no fix dispatched)``) means the PR HAS
   blockers — the tick must not clean-exit.
3. The waiter's clean exit records WHICH PRs it verified; the stop gate skips
   the re-demand for marker-covered clean PRs (the demanded waiter would
   immediately clean-exit again) but still demands one when required CI is
   running (BOU-1789) or the probe is unobservable.
"""
from __future__ import annotations

import json
import os
import types

import pytest

from agentic_pr_dash import config, github_api, maintenance_check as mc
from agentic_pr_dash._maintenance import waiter as _waiter_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import worktree_check as _worktree_check_mod
from agentic_pr_dash._maintenance._common import _repo_slug

SID = "sess-codex-pr75"


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_LOOP_THRESHOLD", "3")
    monkeypatch.setenv("GAIA_PR_LEDGER_DIR", str(tmp_path / "ledger-unused"))
    # Keep waiter pidfiles + clean-exit markers OUT of the real home dir.
    monkeypatch.setenv("AGENTIC_PR_DASH_WAITER_DIR", str(tmp_path / "waiters"))
    # Isolate from any real detached loop daemon.
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(tmp_path / "empty-daemons"))
    config.load.cache_clear()
    github_api.reset_rate_limit_seen()
    github_api.reset_checks_probe_failure_seen()
    yield
    config.load.cache_clear()
    github_api.reset_rate_limit_seen()
    github_api.reset_checks_probe_failure_seen()


def _await_args(tmp_path):
    return [
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", str(os.getpid()),
        "--max-wait=-1",
        "--interval", "0",
    ]


def _wire_await(tmp_path, monkeypatch, wt):
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees",
                        lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records",
                        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda cwd, sid, work: None)
    monkeypatch.setattr("time.sleep", lambda s: None)


# ---------------------------------------------------------------------------
# Finding 2: failed watch-pending probe must suppress the clean exit
# ---------------------------------------------------------------------------


def test_await_probe_failure_suppresses_clean_exit(tmp_path, monkeypatch):
    """Tick 1: the CI-watch probe FAILS (returns False fail-safe but records the
    failure) — the waiter must NOT clean-exit on unobserved CI state. Tick 2:
    the probe succeeds terminal — clean exit fires."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    _wire_await(tmp_path, monkeypatch, wt)

    tick_count = [0]
    probe_calls = [0]

    def fake_check(path, session_id, *, claim=True):
        tick_count[0] += 1
        if tick_count[0] > 3:
            raise AssertionError("await did not exit once the probe was observable")
        return 0, "nothing pending"

    def fake_collect_watch_pending(owned, cwd, session_id):
        probe_calls[0] += 1
        if probe_calls[0] == 1:
            # Simulate required_checks_pending failing on a non-rate-limit gh
            # error: fail-safe False, but the failure is recorded.
            github_api._note_checks_probe_failure()
        return False

    monkeypatch.setattr(mc, "_check_worktree", fake_check)
    monkeypatch.setattr(mc, "_collect_await_watch_pending", fake_collect_watch_pending)

    rc = mc.main(_await_args(tmp_path))
    assert rc == 0
    assert tick_count[0] == 2, "probe-failure tick must not clean-exit; next tick may"


def test_required_checks_pending_records_probe_failure_on_gh_error(monkeypatch):
    monkeypatch.setenv("AGENTIC_PR_DASH_REPO", "owner/name")
    config.load.cache_clear()
    monkeypatch.setattr(
        github_api, "_run",
        lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    github_api.reset_checks_probe_failure_seen()
    assert github_api.required_checks_pending(42) is False
    assert github_api.checks_probe_failure_seen() is True
    config.load.cache_clear()


def test_required_checks_pending_null_rollup_is_valid_observation(monkeypatch):
    """A null statusCheckRollup means NO checks on the head commit — a valid
    terminal observation, not a probe failure."""
    monkeypatch.setenv("AGENTIC_PR_DASH_REPO", "owner/name")
    config.load.cache_clear()
    body = json.dumps({"data": {"repository": {"pullRequest": {"commits": {"nodes": [
        {"commit": {"statusCheckRollup": None}}
    ]}}}}})
    monkeypatch.setattr(
        github_api, "_run",
        lambda cmd, **kw: types.SimpleNamespace(returncode=0, stdout=body, stderr=""),
    )
    github_api.reset_checks_probe_failure_seen()
    assert github_api.required_checks_pending(42) is False
    assert github_api.checks_probe_failure_seen() is False
    config.load.cache_clear()


# ---------------------------------------------------------------------------
# Finding 3: warn-only deferral is NOT clean
# ---------------------------------------------------------------------------


def test_await_warn_only_deferral_suppresses_clean_exit(tmp_path, monkeypatch):
    """A tick whose check defers with the warn-only marker (PR has blockers, a
    live foreign owner is responsible) must not clean-exit; once the deferral
    clears and the PR is genuinely clean, the waiter exits."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    _wire_await(tmp_path, monkeypatch, wt)

    tick_count = [0]

    def fake_check(path, session_id, *, claim=True):
        tick_count[0] += 1
        if tick_count[0] == 1:
            return 0, (
                "owned PR #5 has blockers ['review_comments']; deferring to "
                f"live PR-watch owner session other — {_worktree_check_mod.WARN_ONLY_MARKER}"
            )
        if tick_count[0] > 3:
            raise AssertionError("await did not exit once the deferral cleared")
        return 0, "nothing pending"

    monkeypatch.setattr(mc, "_check_worktree", fake_check)
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)

    rc = mc.main(_await_args(tmp_path))
    assert rc == 0
    assert tick_count[0] == 2, "warn-only deferral tick must not clean-exit"


# ---------------------------------------------------------------------------
# Finding 1: clean-exit marker <-> stop-gate re-demand
# ---------------------------------------------------------------------------


def _arm(tmp_path, pr_number: int):
    wt = tmp_path / "worktree"
    wt.mkdir(exist_ok=True)
    mc._write_arm_marker(str(wt), SID, os.getpid(), pr_number)
    return wt


def _marker_key(wt, pr: int) -> str:
    """The repo-qualified identity the waiter/stop-gate derive for ``wt``'s PR."""
    return _waiter_mod._clean_exit_key(_repo_slug(str(wt)), pr)


def test_await_clean_exit_writes_marker(tmp_path, monkeypatch):
    wt = _arm(tmp_path, 42)
    _wire_await(tmp_path, monkeypatch, wt)
    monkeypatch.setattr(mc, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)
    # The worktree still resolves to the marker PR (round 5: only PRs the tick
    # actually observed may be recorded verified-clean).
    monkeypatch.setattr(mc, "_current_branch", lambda cwd: "feature-42")
    monkeypatch.setattr(mc, "_resolve_open_pr_for_branch", lambda cwd, branch: (42, False))

    rc = mc.main(_await_args(tmp_path))
    assert rc == 0
    assert _waiter_mod._read_clean_exit_keys(SID) == {_marker_key(wt, 42)}


def test_await_exit_10_clears_marker(tmp_path, monkeypatch):
    wt = _arm(tmp_path, 42)
    _wire_await(tmp_path, monkeypatch, wt)
    _waiter_mod._write_clean_exit_marker(SID, {_marker_key(wt, 42)})
    monkeypatch.setattr(
        mc, "_check_worktree",
        lambda path, sid, *, claim=True: (10, "needs review\nPR_NUMBER=42"),
    )

    rc = mc.main(_await_args(tmp_path))
    assert rc == 10
    assert _waiter_mod._read_clean_exit_keys(SID) == set()


def _wire_stop_gate(tmp_path, monkeypatch, wt):
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees",
                        lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(_worktree_check_mod, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records",
                        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])


def test_stop_gate_skips_redemand_for_marker_covered_clean_pr(tmp_path, monkeypatch, capsys):
    """Waiter verified PR #42 clean and exited → stop gate must NOT re-demand a
    waiter that would immediately clean-exit again (CI terminal, observable)."""
    wt = _arm(tmp_path, 42)
    _wire_stop_gate(tmp_path, monkeypatch, wt)
    _waiter_mod._write_clean_exit_marker(SID, {_marker_key(wt, 42)})
    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd=None: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 0
    assert "await" not in err.lower()


def test_stop_gate_still_demands_waiter_when_ci_running_again(tmp_path, monkeypatch, capsys):
    """Marker-covered PR whose required CI is running again: a waiter WOULD stay
    alive (BOU-1789 watch-pending), so the demand stands."""
    wt = _arm(tmp_path, 42)
    _wire_stop_gate(tmp_path, monkeypatch, wt)
    _waiter_mod._write_clean_exit_marker(SID, {_marker_key(wt, 42)})
    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd=None: True)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2
    assert "#42" in err


def test_stop_gate_still_demands_waiter_when_probe_unobservable(tmp_path, monkeypatch, capsys):
    """Marker-covered PR whose CI probe fails: state unobservable → fail toward
    coverage (demand the waiter; it suppresses ITS clean exit the same way)."""
    wt = _arm(tmp_path, 42)
    _wire_stop_gate(tmp_path, monkeypatch, wt)
    _waiter_mod._write_clean_exit_marker(SID, {_marker_key(wt, 42)})

    def failing_probe(n, cwd=None):
        github_api._note_checks_probe_failure()
        return False

    monkeypatch.setattr(github_api, "required_checks_pending", failing_probe)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2
    assert "#42" in err


def test_stop_gate_marker_skip_fails_closed_on_unobservable_check(tmp_path, monkeypatch, capsys):
    """Round 2: a code-2 (gh unobservable) _check_worktree result means 'nothing
    pending' was NOT established — the marker-skip must not fire; the demand
    stands (fail toward coverage)."""
    wt = _arm(tmp_path, 42)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees",
                        lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(_worktree_check_mod, "_check_worktree",
                        lambda path, sid, *, claim=True: (2, "could not list PRs (gh unavailable)"))
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records",
                        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    _waiter_mod._write_clean_exit_marker(SID, {_marker_key(wt, 42)})
    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd=None: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2
    assert "#42" in err


def test_stop_gate_marker_skip_fails_closed_on_warn_only_deferral(tmp_path, monkeypatch, capsys):
    """Round 2 (same class): a warn-only deferral means the PR HAS blockers a
    foreign owner is servicing — not clean; the marker-skip must not fire."""
    wt = _arm(tmp_path, 42)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees",
                        lambda sid, cwd: [str(wt)])
    monkeypatch.setattr(
        _worktree_check_mod, "_check_worktree",
        lambda path, sid, *, claim=True: (
            0,
            "owned PR #42 has blockers ['review_comments']; deferring to "
            f"live PR-watch owner session other — {_worktree_check_mod.WARN_ONLY_MARKER}",
        ),
    )
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records",
                        lambda sid, cwd, include_legacy=True, prune_legacy=True: [])
    _waiter_mod._write_clean_exit_marker(SID, {_marker_key(wt, 42)})
    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd=None: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2
    assert "#42" in err


def test_required_checks_pending_truncated_pagination_is_probe_failure(monkeypatch):
    """Round 2: falling out of the rollup page loop with hasNextPage still true
    means the observation was TRUNCATED — a required pending context may sit on
    a later page, so this must record a probe failure, not read as terminal."""
    monkeypatch.setenv("AGENTIC_PR_DASH_REPO", "owner/name")
    config.load.cache_clear()
    monkeypatch.setattr(github_api, "_ROLLUP_MAX_PAGES", 2)

    def _page(cursor):
        return json.dumps({"data": {"repository": {"pullRequest": {"commits": {"nodes": [
            {"commit": {"statusCheckRollup": {"contexts": {
                "nodes": [{"__typename": "CheckRun", "status": "COMPLETED",
                           "conclusion": "SUCCESS", "isRequired": True}],
                "pageInfo": {"hasNextPage": True, "endCursor": cursor},
            }}}}
        ]}}}}})

    calls = [0]

    def fake_run(cmd, **kw):
        calls[0] += 1
        return types.SimpleNamespace(returncode=0, stdout=_page(f"c{calls[0]}"), stderr="")

    monkeypatch.setattr(github_api, "_run", fake_run)
    github_api.reset_checks_probe_failure_seen()
    assert github_api.required_checks_pending(42) is False
    assert github_api.checks_probe_failure_seen() is True
    assert calls[0] == 2, "must stop at the page cap"
    config.load.cache_clear()


def test_stop_gate_demands_waiter_for_pr_not_covered_by_marker(tmp_path, monkeypatch, capsys):
    """A marker from an earlier clean exit does not cover a NEW PR — the demand
    fires so the new PR gets its first waiter round."""
    wt = _arm(tmp_path, 43)  # new PR; marker only covers 42
    _wire_stop_gate(tmp_path, monkeypatch, wt)
    _waiter_mod._write_clean_exit_marker(SID, {_marker_key(wt, 42)})
    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd=None: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2
    assert "#43" in err


# ---------------------------------------------------------------------------
# Round 3: repo-qualified identity for the marker and detached CI-watch state
# ---------------------------------------------------------------------------


def _detached_record(pr: int, repo: str, *, ci_watch_pending: bool) -> dict:
    return {
        "pr": pr, "url": f"https://example/{repo}/pull/{pr}", "branch": "b",
        "repo": repo, "worktree_present": False, "unresolved_threads": 0,
        "ci_failing": False, "failing_checks": [],
        "ci_watch_pending": ci_watch_pending, "changes_requested": False,
        "review_decision": None, "merge_conflict": False,
        "merge_state": "CLEAN", "mergeable": "MERGEABLE",
        "p1": False, "state": "open",
    }


def test_stop_gate_marker_skip_requires_every_repo_identity(tmp_path, monkeypatch, capsys):
    """Round 3: the same PR number in a SECOND repo (detached record) is a
    distinct identity — a marker covering only the worktree repo's #42 must not
    suppress the demand."""
    wt = _arm(tmp_path, 42)
    _wire_stop_gate(tmp_path, monkeypatch, wt)
    monkeypatch.setattr(
        _worktrees_mod, "_detached_records_across_roots",
        lambda sid, cwd: [_detached_record(42, "other-org/other-repo",
                                           ci_watch_pending=False)],
    )
    _waiter_mod._write_clean_exit_marker(SID, {_marker_key(wt, 42)})
    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd=None: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2
    assert "#42" in err


def test_stop_gate_detached_ci_watch_not_collapsed_across_repos(tmp_path, monkeypatch, capsys):
    """Round 3: two same-numbered detached records in different repos — the one
    with required CI running must keep the demand alive even when another
    record with the same number is CI-terminal (no last-wins collapse)."""
    wt = _arm(tmp_path, 42)
    _wire_stop_gate(tmp_path, monkeypatch, wt)
    rec_running = _detached_record(7, "org-a/repo-a", ci_watch_pending=True)
    rec_terminal = _detached_record(7, "org-b/repo-b", ci_watch_pending=False)
    # Iteration order puts the CI-running record FIRST so a last-wins dict
    # would drop it in favor of the terminal one.
    monkeypatch.setattr(
        _worktrees_mod, "_detached_records_across_roots",
        lambda sid, cwd: [rec_running, rec_terminal],
    )
    _waiter_mod._write_clean_exit_marker(SID, {
        _marker_key(wt, 42),
        _waiter_mod._clean_exit_key("org-a/repo-a", 7),
        _waiter_mod._clean_exit_key("org-b/repo-b", 7),
    })
    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd=None: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    assert rc == 2, "a CI-running detached record must keep the waiter demand"


def test_stop_gate_marker_skip_covers_multi_repo_identities_when_all_clean(tmp_path, monkeypatch, capsys):
    """Round 3 complement: when the marker covers BOTH repo identities and no
    record has CI running, the skip fires."""
    wt = _arm(tmp_path, 42)
    _wire_stop_gate(tmp_path, monkeypatch, wt)
    monkeypatch.setattr(
        _worktrees_mod, "_detached_records_across_roots",
        lambda sid, cwd: [_detached_record(7, "org-a/repo-a", ci_watch_pending=False),
                          _detached_record(7, "org-b/repo-b", ci_watch_pending=False)],
    )
    _waiter_mod._write_clean_exit_marker(SID, {
        _marker_key(wt, 42),
        _waiter_mod._clean_exit_key("org-a/repo-a", 7),
        _waiter_mod._clean_exit_key("org-b/repo-b", 7),
    })
    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd=None: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 0
    assert "await" not in err.lower()


# ---------------------------------------------------------------------------
# Round 4: a failed CI probe during the detached-record read must survive the
# marker-skip's flag reset (a detached PR has no worktree to re-probe)
# ---------------------------------------------------------------------------


def test_stop_gate_marker_skip_preserves_detached_probe_failure(tmp_path, monkeypatch, capsys):
    """Round 4: the detached record's ``ci_watch_pending`` was computed by a
    probe that FAILED (fail-safe ``False`` + module failure flag). The
    marker-skip's flag reset must not discard that observation and clean-exit:
    the detached PR has no worktree in ``pr_to_wts`` with which to re-probe, so
    the record-build read is its ONLY CI observation this pass."""
    wt = _arm(tmp_path, 42)
    _wire_stop_gate(tmp_path, monkeypatch, wt)

    def detached_with_failed_probe(sid, cwd):
        # Mimic required_checks_pending failing inside _detached_pr_records:
        # the record carries the fail-safe False, the module flag the failure.
        github_api._note_checks_probe_failure()
        return [_detached_record(7, "org-a/repo-a", ci_watch_pending=False)]

    monkeypatch.setattr(_worktrees_mod, "_detached_records_across_roots",
                        detached_with_failed_probe)
    # Marker covers EVERY open identity, worktree CI terminal + observable —
    # the ONLY stay-alive signal is the detached read's probe failure.
    _waiter_mod._write_clean_exit_marker(SID, {
        _marker_key(wt, 42),
        _waiter_mod._clean_exit_key("org-a/repo-a", 7),
    })
    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd=None: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2, "unobserved detached CI state must fail toward coverage"
    assert "#7" in err


# ---------------------------------------------------------------------------
# Round 5: (a) a failed get_ci_checks blocker read is unobservable, not "no
# checks"; (b) the clean-exit verified set may only contain PRs the tick
# actually observed (worktree still resolves to the marker PR); (c) a detached
# record whose state could not be read ("unknown") keeps the waiter alive.
# ---------------------------------------------------------------------------


def test_get_ci_checks_unobservable_records_probe_failure(monkeypatch):
    """Both the `gh pr checks` JSON read and the REST fallback's head-SHA
    resolution fail: [] must be flagged unobservable, not read as no-checks."""
    monkeypatch.setattr(
        github_api, "_run",
        lambda cmd, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    github_api.reset_checks_probe_failure_seen()
    assert github_api.get_ci_checks(42) == []
    assert github_api.checks_probe_failure_seen() is True


def test_get_ci_checks_rest_checkrun_read_failure_records_probe_failure(monkeypatch):
    """The REST fallback resolves the head SHA but the check-runs/status reads
    fail: still unobservable."""
    def fake_run(cmd, **kw):
        joined = " ".join(cmd)
        if "pulls/" in joined:
            return types.SimpleNamespace(returncode=0, stdout="abc123\n", stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(github_api, "_run", fake_run)
    github_api.reset_checks_probe_failure_seen()
    assert github_api.get_ci_checks(42) == []
    assert github_api.checks_probe_failure_seen() is True


def test_await_clean_exit_excludes_stale_marker_pr(tmp_path, monkeypatch):
    """The armed marker names PR #42 but the worktree was repointed to a branch
    with NO open PR: the tick never observed #42, so the clean-exit marker must
    not record it as verified (stop-gate marker coverage would otherwise
    suppress the waiter while #42's feedback goes unobserved)."""
    wt = _arm(tmp_path, 42)
    _wire_await(tmp_path, monkeypatch, wt)
    monkeypatch.setattr(mc, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "no open PR for this branch"))
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)
    monkeypatch.setattr(mc, "_current_branch", lambda cwd: "repointed-branch")
    monkeypatch.setattr(mc, "_resolve_open_pr_for_branch", lambda cwd, branch: None)

    rc = mc.main(_await_args(tmp_path))
    assert rc == 0
    assert _waiter_mod._read_clean_exit_keys(SID) == set()


def test_await_clean_exit_excludes_marker_pr_when_branch_resolves_elsewhere(tmp_path, monkeypatch):
    """Marker names #42 but the worktree's current branch resolves to open PR
    #43: the tick verified #43, not #42 — #42 must not be recorded clean."""
    wt = _arm(tmp_path, 42)
    _wire_await(tmp_path, monkeypatch, wt)
    monkeypatch.setattr(mc, "_check_worktree",
                        lambda path, sid, *, claim=True: (0, "nothing pending"))
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)
    monkeypatch.setattr(mc, "_current_branch", lambda cwd: "other-feature")
    monkeypatch.setattr(mc, "_resolve_open_pr_for_branch", lambda cwd, branch: (43, False))

    rc = mc.main(_await_args(tmp_path))
    assert rc == 0
    assert _marker_key(wt, 42) not in _waiter_mod._read_clean_exit_keys(SID)


def test_await_unknown_detached_state_suppresses_clean_exit(tmp_path, monkeypatch):
    """Tick 1: a detached ledger PR's state read failed (state="unknown", a
    non-rate-limit gh error) — its review/merge/CI state was never observed, so
    the clean exit must not fire. Tick 2: the record resolves — clean exit."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    _wire_await(tmp_path, monkeypatch, wt)

    tick = [0]

    def fake_check(path, sid, *, claim=True):
        tick[0] += 1
        if tick[0] > 3:
            raise AssertionError("await did not exit once the record resolved")
        return 0, "nothing pending"

    unknown = dict(_detached_record(9, "org-a/repo-a", ci_watch_pending=False),
                   state="unknown", url="(pr 9)")

    def fake_detached(sid, cwd, include_legacy=True, prune_legacy=True):
        return [unknown] if tick[0] <= 1 else []

    monkeypatch.setattr(mc, "_check_worktree", fake_check)
    monkeypatch.setattr(mc, "_collect_await_watch_pending", lambda owned, cwd, sid: False)
    monkeypatch.setattr(_reconcile_mod, "_detached_pr_records", fake_detached)

    rc = mc.main(_await_args(tmp_path))
    assert rc == 0
    assert tick[0] == 2, "unknown-state detached tick must not clean-exit"


def test_stop_gate_marker_skip_fails_closed_on_blocker_read_probe_failure(tmp_path, monkeypatch, capsys):
    """A get_ci_checks failure during the _check_worktree pass (BEFORE the
    marker-skip's scoped flag reset) means 'nothing pending' was computed
    blind — the marker-skip must not fire (fail toward coverage)."""
    wt = _arm(tmp_path, 42)
    _wire_stop_gate(tmp_path, monkeypatch, wt)

    def blind_check(path, sid, *, claim=True):
        github_api._note_checks_probe_failure()  # blocker-status read failed
        return 0, "nothing pending"

    monkeypatch.setattr(_worktree_check_mod, "_check_worktree", blind_check)
    _waiter_mod._write_clean_exit_marker(SID, {_marker_key(wt, 42)})
    monkeypatch.setattr(github_api, "required_checks_pending", lambda n, cwd=None: False)

    rc = mc.main(["stop-gate", "--cwd", str(tmp_path), "--session-id", SID])
    err = capsys.readouterr().err
    assert rc == 2, "blind blocker read must fail toward coverage"
    assert "#42" in err
