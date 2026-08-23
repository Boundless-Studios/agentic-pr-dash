"""BOU-2430: the waiter's blocking scope must agree with the stop gate's.

The stop gate explicitly excludes auto-adopted worktrees from its blocking
`pending` set (see test_adopted_pr_provenance.py / stop_gate.py's
`adopted_pending` split) and tells the operator so: "These were auto-adopted,
not armed by this session ... NOT blocking your stop." It then prescribes
launching the `await` waiter.

Before this fix, `_run_await_loop` had no equivalent split: every worktree in
`owned` (armed OR adopted) that `_check_worktree` reported as pending (code 10)
went straight into the waiter's blocking `pending` list, so the waiter exited
10 ("Feedback arrived — address it now") on the very PR the gate had just
called non-blocking. That is an unsatisfiable prescription: the gate says
"start a waiter", the waiter immediately reports required action on a PR nobody
asked this session to service (BOU-2430).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentic_pr_dash import config
from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash import ownership as _ownership_mod
from agentic_pr_dash._maintenance import ownership_resolution as _ownres_mod
from agentic_pr_dash._maintenance import reconcile as _reconcile_mod
from agentic_pr_dash._maintenance import waiter as _waiter_mod
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod

SID = "sess-bou2430"


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def _fake_ownership(provenance_by_wt: dict[str, str]):
    def _resolve(worktree_path, *, kind, snap=None):
        wt = str(Path(worktree_path))
        prov = provenance_by_wt.get(wt, "armed")
        return _ownres_mod.WorktreeOwnership(
            worktree=wt,
            session_id=SID,
            pr_number=999,
            provenance=prov,
            source="marker",
        )
    return _resolve


def test_waiter_does_not_block_on_an_adopted_worktrees_blockers(tmp_path, monkeypatch):
    """An adopted-only owned worktree with blockers must NOT make the waiter
    exit 10 — the gate already told the caller it is not their responsibility.
    """
    adopted_wt = tmp_path / "adopted"
    adopted_wt.mkdir()

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(adopted_wt)],
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    monkeypatch.setattr(
        _ownres_mod, "resolve_worktree", _fake_ownership({str(adopted_wt): "adopted"})
    )

    def _fake_check(path, session_id, *, claim=True):
        assert Path(path) == adopted_wt
        return 10, (
            "PR #999: needs work\nSUMMARY=PR #999: 1 unresolved review comment(s)\n"
            "PR_NUMBER=999"
        )

    monkeypatch.setattr(mc, "_check_worktree", _fake_check)
    watched = []
    monkeypatch.setattr(
        mc, "_await_watch_pending_this_tick",
        lambda owned, detached, cwd, sid: watched.extend(owned) or False,
    )

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "1",
        "--max-wait", "0",
    ])

    # Must NOT be the "feedback arrived, address it now" exit — that PR isn't
    # this session's to service.
    assert rc != 10, (
        "waiter blocked on an ADOPTED worktree's blockers — the stop gate says "
        "this is not the session's responsibility, so the waiter must not "
        "treat it as actionable feedback either (BOU-2430 scope mismatch)"
    )
    assert watched == [], "adopted worktrees must not feed clean-exit watch evidence"


def test_waiter_does_not_publish_or_refresh_adopted_worktree_coverage(
    tmp_path, monkeypatch
):
    """Adopted paths must never advertise wake-capable waiter ownership.

    Otherwise the machine-wide loop defers to this live waiter even though the
    waiter intentionally excludes the adopted PR from its blocker checks.
    """
    adopted_wt = tmp_path / "adopted"
    armed_wt = tmp_path / "armed"
    adopted_wt.mkdir()
    armed_wt.mkdir()

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        _worktrees_mod,
        "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(adopted_wt), str(armed_wt)],
    )
    monkeypatch.setattr(
        _reconcile_mod,
        "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    monkeypatch.setattr(
        _ownres_mod,
        "resolve_worktree",
        _fake_ownership({
            str(adopted_wt): "adopted",
            str(armed_wt): "armed",
        }),
    )
    monkeypatch.setattr(
        mc,
        "_check_worktree",
        lambda path, session_id, *, claim=True: (
            0,
            "PR clean\nPR_NUMBER=999",
        ),
    )
    monkeypatch.setattr(
        mc,
        "_await_watch_pending_this_tick",
        lambda owned, detached, cwd, sid: False,
    )

    published = []
    heartbeats = []
    monkeypatch.setattr(
        mc,
        "_update_await_coverage",
        lambda cwd, sid, roots: published.append(list(roots)),
    )
    monkeypatch.setattr(
        mc,
        "_touch_owner_heartbeat",
        lambda worktree, sid, pending: heartbeats.append(worktree),
    )

    mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "1",
        "--max-wait", "0",
    ])

    assert all(str(adopted_wt) not in roots for roots in published)
    assert str(armed_wt) in published[-1]
    assert str(adopted_wt) not in heartbeats
    assert str(armed_wt) in heartbeats


def test_waiter_never_publishes_an_adopted_anchor(tmp_path, monkeypatch):
    """`_await_anchors` is NOT a safe coverage list.

    It appends every worktree the session ledger references — adopted ones
    included — so publishing `[*anchors, *watched_owned]` (or bare `anchors`
    earlier in the tick) puts the adopted path straight back into
    `covered_roots`, which is the exact thing filtering `owned` was meant to
    stop. `_update_await_coverage` REPLACES `covered_roots`, so an adopted
    anchor published at any point in the tick is enough for `_await_alive` to
    make the machine-wide loop defer a PR this waiter deliberately ignores.
    """
    adopted_wt = tmp_path / "adopted"
    armed_wt = tmp_path / "armed"
    adopted_wt.mkdir()
    armed_wt.mkdir()

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(_waiter_mod, "_await_alive", lambda cwd, sid: False)
    # The adopted worktree reaches coverage as a ledger ANCHOR, not via `owned`.
    monkeypatch.setattr(
        mc,
        "_await_anchors",
        lambda sid, cwd: [str(tmp_path), str(adopted_wt)],
    )
    monkeypatch.setattr(
        _worktrees_mod,
        "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(armed_wt)],
    )
    monkeypatch.setattr(
        _reconcile_mod,
        "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    monkeypatch.setattr(
        _ownres_mod,
        "resolve_worktree",
        _fake_ownership({
            str(adopted_wt): "adopted",
            str(armed_wt): "armed",
        }),
    )
    monkeypatch.setattr(
        mc,
        "_check_worktree",
        lambda path, session_id, *, claim=True: (0, "PR clean\nPR_NUMBER=999"),
    )
    monkeypatch.setattr(
        mc,
        "_await_watch_pending_this_tick",
        lambda owned, detached, cwd, sid: False,
    )
    monkeypatch.setattr(mc, "_touch_owner_heartbeat", lambda wt, sid, pending: None)

    published = []
    monkeypatch.setattr(
        mc,
        "_update_await_coverage",
        lambda cwd, sid, roots: published.append(list(roots)),
    )

    mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "1",
        "--max-wait", "0",
    ])

    assert published, "the waiter must publish its coverage at least once"
    for roots in published:
        assert str(adopted_wt) not in roots, (
            "an adopted worktree reached covered_roots via the anchor list — "
            "the loop will defer to this waiter for a PR it ignores"
        )
    assert str(armed_wt) in published[-1], "armed coverage must still be published"


def _owning_fake(provenance_by_wt: dict[str, str]):
    """Like `_fake_ownership`, but the session genuinely OWNS the worktree.

    `owned_by` reads `marker_session_id`/`claim_session_ids`, not `session_id`,
    so a fake that leaves those unset makes `_request_waiter_coverage` return
    False before it reaches the logic under test — a vacuous pass.
    """
    def _resolve(worktree_path, *, kind, snap=None):
        wt = str(Path(worktree_path))
        return _ownres_mod.WorktreeOwnership(
            worktree=wt,
            session_id=SID,
            pr_number=999,
            provenance=provenance_by_wt.get(wt, "armed"),
            source="marker",
            marker_session_id=SID,
        )
    return _resolve


def _stub_pidfile(tmp_path, monkeypatch):
    pidfile = tmp_path / "await.json"
    pidfile.write_text(
        json.dumps({"pid": os.getpid(), "session_id": SID, "covered_roots": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(_waiter_mod, "_await_pidfile", lambda cwd, sid: str(pidfile))
    monkeypatch.setattr(
        _waiter_mod, "_write_await_pidfile", lambda cwd, data, sid: None
    )


def test_coverage_request_is_refused_for_an_adopted_worktree(tmp_path, monkeypatch):
    """Filtering the published list is not enough on its own.

    `_await_alive` falls back to `_request_waiter_coverage` whenever the path
    is absent from `covered_roots` — and that helper only asks `owned_by`,
    which an ADOPTED worktree satisfies. So the adopted path would be appended
    to `requested_roots`, `_await_alive` would return True anyway, and the
    machine-wide loop would defer exactly as before. `_update_await_coverage`
    keeps any requested root that is not covered, so this leak is permanent
    rather than transient.
    """
    adopted_wt = tmp_path / "adopted"
    adopted_wt.mkdir()

    monkeypatch.setattr(
        _ownres_mod,
        "resolve_worktree",
        _owning_fake({str(adopted_wt): "adopted"}),
    )
    _stub_pidfile(tmp_path, monkeypatch)

    assert _waiter_mod._request_waiter_coverage(str(adopted_wt), SID) is False, (
        "an adopted worktree must not be granted waiter coverage on request"
    )


def test_coverage_request_still_granted_for_an_armed_worktree(tmp_path, monkeypatch):
    """Control: the armed path must still be able to request coverage."""
    armed_wt = tmp_path / "armed"
    armed_wt.mkdir()

    monkeypatch.setattr(
        _ownres_mod,
        "resolve_worktree",
        _owning_fake({str(armed_wt): "armed"}),
    )
    _stub_pidfile(tmp_path, monkeypatch)

    assert _waiter_mod._request_waiter_coverage(str(armed_wt), SID) is True


def test_waiter_still_blocks_on_an_armed_worktrees_blockers(tmp_path, monkeypatch):
    """Control: a genuinely ARMED (this session's own) worktree's blockers must
    still wake the waiter — only the adopted case is excluded."""
    armed_wt = tmp_path / "armed"
    armed_wt.mkdir()

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(armed_wt)],
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    monkeypatch.setattr(
        _ownres_mod, "resolve_worktree", _fake_ownership({str(armed_wt): "armed"})
    )

    def _fake_check(path, session_id, *, claim=True):
        return 10, (
            "PR #1000: needs work\nSUMMARY=PR #1000: 1 unresolved review comment(s)\n"
            "PR_NUMBER=1000"
        )

    monkeypatch.setattr(mc, "_check_worktree", _fake_check)

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "1",
        "--max-wait", "0",
    ])

    assert rc == 10, "an ARMED worktree's real blockers must still wake the waiter"


@pytest.mark.parametrize(
    ("code", "text"),
    [
        (2, "gh could not resolve the PR for this worktree"),
        (0, "PR #999: clean\nPR_NUMBER=999"),
    ],
    ids=["gh-unobservable", "clean-with-running-ci"],
)
def test_adopted_worktree_is_excluded_on_every_outcome(tmp_path, monkeypatch, code, text):
    """Provenance must be resolved for EVERY owned worktree, not only code==10.

    The first version of the adopted-scope exclusion recorded `adopted_worktrees`
    inside the `code == 10` branch only. An adopted worktree returning anything
    else therefore stayed in the waiter's effective scope:

      * code 2 raised the GLOBAL `gh_unobservable` flag, which suppresses the
        clean exit for unrelated ARMED PRs that were perfectly fine;
      * code 0 with running CI stayed in `watched_owned`, so the waiter kept
        itself alive indefinitely watching work the maintenance loop owns.

    Both are the same bug as BOU-2430 itself, just on a different exit code.
    """
    adopted_wt = tmp_path / "adopted"
    adopted_wt.mkdir()

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees",
        lambda sid, cwd: [str(adopted_wt)],
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [],
    )
    monkeypatch.setattr(
        _ownres_mod, "resolve_worktree", _fake_ownership({str(adopted_wt): "adopted"})
    )
    monkeypatch.setattr(
        mc, "_check_worktree", lambda path, session_id, *, claim=True: (code, text)
    )
    watched = []
    monkeypatch.setattr(
        mc, "_await_watch_pending_this_tick",
        lambda owned, detached, cwd, sid: watched.extend(owned) or False,
    )

    mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "1",
        "--max-wait", "0",
    ])

    assert watched == [], (
        f"an ADOPTED worktree returning code {code} must be excluded from the "
        "waiter's watch probes too — provenance has to be resolved for every "
        "owned worktree, not just the code==10 ones"
    )


# --- Detached (worktree-less) adopted PRs -------------------------------------
#
# The exclusions above are keyed on OWNED WORKTREES. A PR whose worktree has
# been torn down never appears there: it reaches the waiter through
# `_detached_pr_records`, which had no provenance check at all. So an adopted
# PR with standing feedback and no worktree still made the waiter exit 10 on
# its first tick — leaving the session's own freshly-armed PR unwatched.


def _adopted_detached_record(pr: int = 3546, repo: str = "acme/widgets") -> dict:
    return {
        "pr": pr,
        "url": f"https://github.com/{repo}/pull/{pr}",
        "branch": "some-adopted-branch",
        "repo": repo,
        "unresolved_threads": 2,
        "ci_failing": False,
        "changes_requested": False,
        "merge_conflict": False,
        "gh_state_unknown": False,
        "p1": True,
    }


def _snapshot_with_provenance(provenance_by_pr: dict[int, str]):
    class _View:
        def __init__(self, provenance):
            self.provenance = provenance

    class _Snap:
        def known(self):
            return True

        def owner_for(self, repo, pr_number):
            prov = provenance_by_pr.get(pr_number)
            return _View(prov) if prov else None

        def live_owner_for(self, repo, pr_number):
            return self.owner_for(repo, pr_number)

    return lambda *a, **k: _Snap()


def test_waiter_does_not_block_on_an_adopted_detached_prs_feedback(
    tmp_path, monkeypatch
):
    """An adopted PR whose worktree is gone must not make the waiter exit 10.

    This is the shape that actually broke: the stop gate reports the adopted PR
    as "(no worktree) ... NOT blocking your stop" and prescribes a waiter; the
    waiter then saw it as a detached record with unresolved threads and exited
    immediately, so the PR the session had just armed was never watched.
    """
    record = _adopted_detached_record()

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: []
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [record],
    )
    monkeypatch.setattr(
        _ownership_mod, "snapshot",
        _snapshot_with_provenance({record["pr"]: "adopted"}),
    )

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "1",
        "--max-wait", "0",
    ])

    assert rc != 10, (
        "waiter exited 'feedback arrived' on an ADOPTED detached PR — the stop "
        "gate calls that PR non-blocking, so the waiter it prescribes must not "
        "treat it as actionable feedback either (BOU-2430, detached path)"
    )


def test_waiter_still_blocks_on_an_armed_detached_prs_feedback(tmp_path, monkeypatch):
    """Positive control: an armed detached PR must still wake the waiter.

    Without this, the fix above could be a blanket "ignore detached records"
    and the suite would not notice.
    """
    record = _adopted_detached_record(pr=3548)

    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        _worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: []
    )
    monkeypatch.setattr(
        _reconcile_mod, "_detached_pr_records",
        lambda sid, cwd, include_legacy=True, prune_legacy=True: [record],
    )
    monkeypatch.setattr(
        _ownership_mod, "snapshot",
        _snapshot_with_provenance({record["pr"]: "armed"}),
    )

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", SID,
        "--owner-pid", "1",
        "--max-wait", "0",
    ])

    assert rc == 10, (
        "an ARMED detached PR with unresolved threads must still exit 10 — this "
        "session armed it and nothing else is watching it"
    )
