"""BOU-2409: `arm` must reclaim from a foreign session's ADOPTED claim, and
must say so honestly when it cannot.

Repro (BOU-2409): session A auto-adopts PR N into its ownership (a
best-effort backstop for an unowned PR). Session B — the session that
actually owns the worktree/branch for PR N and just pushed a fix — tries to
`arm` it and gets a bare "could not write arm marker in <cwd>", with no
mention of who holds it or why. Two defects:

1. An `armed` claim attempt must OUTRANK a foreign `adopted` claim — adoption
   is advisory, not a lock against the session doing the real work.
2. When `arm` still can't proceed (e.g. the foreign claim is genuinely
   `armed`, not `adopted`), the failure must name the actual holder
   (session id / pid / provenance), not the generic, misleading
   "could not write arm marker".
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentic_pr_dash import config, ownership
from agentic_pr_dash import maintenance_check as mc

REPO = "Boundless-Studios/agentic-pr-dash"
LIVE_PID = os.getpid()


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_PR_DASH_COORDINATOR_STORE", str(tmp_path / "claims.jsonl"))
    monkeypatch.setenv("AGENTIC_PR_DASH_STATE_DIR", str(tmp_path / ".gaia"))
    monkeypatch.setattr(config, "_detect_repo", lambda path: REPO)
    return tmp_path


def _mk(tmp_path: Path, name: str) -> str:
    wt = tmp_path / name
    wt.mkdir()
    return str(wt)


def test_armed_claim_reclaims_a_foreign_adopted_claim(isolated_store, monkeypatch):
    """A session explicitly arming a PR must succeed even though a foreign
    session's ADOPTED claim is still live — adoption is a backstop, not a
    lock against the session actually doing the work."""
    wt = _mk(isolated_store, "wt")

    other_pid = LIVE_PID  # any live pid; the OTHER session's claim is "adopted"
    outcome = ownership.record_ownership(
        repo=REPO, pr_number=555, session_id="foreign-adopter", pid=other_pid,
        worktree_path=wt, provenance=ownership.PROVENANCE_ADOPTED,
    )
    assert outcome.ok, "setup: foreign session must actually hold the adopted claim"

    ok = mc._write_arm_marker(wt, "real-owner-session", LIVE_PID, 555)
    assert ok, (
        "arm must reclaim from a foreign ADOPTED claim, not fail closed against "
        "it — adoption is advisory, never a lock against a session actually "
        "doing the work (BOU-2409)"
    )

    snap = ownership.snapshot()
    claim = snap.claim_for(REPO, 555)
    assert claim is not None
    assert claim.owner.session_id == "real-owner-session", (
        "the reclaiming session must now hold the claim"
    )


def test_arm_never_reclaims_a_foreign_armed_claim(isolated_store, monkeypatch):
    """Control: a foreign session's genuine ARMED claim must still fail closed
    — only ADOPTED claims are reclaimable this way."""
    wt = _mk(isolated_store, "wt")

    outcome = ownership.record_ownership(
        repo=REPO, pr_number=556, session_id="foreign-armer", pid=LIVE_PID,
        worktree_path=wt, provenance=ownership.PROVENANCE_ARMED,
    )
    assert outcome.ok

    ok = mc._write_arm_marker(wt, "someone-else", LIVE_PID, 556)
    assert not ok, "a foreign session's genuine ARMED claim must still fail closed"

    snap = ownership.snapshot()
    claim = snap.claim_for(REPO, 556)
    assert claim.owner.session_id == "foreign-armer", "the original armed claim must be untouched"


def test_arm_cli_names_the_foreign_holder_instead_of_a_generic_message(
    isolated_store, monkeypatch, capsys
):
    """When `arm` genuinely cannot proceed, its message must name the actual
    holder (session/pid/provenance) rather than the misleading, generic
    "could not write arm marker" — which reads as a filesystem/write problem
    when the real cause is a fenced ownership claim."""
    wt = _mk(isolated_store, "wt")

    outcome = ownership.record_ownership(
        repo=REPO, pr_number=557, session_id="foreign-armer-2", pid=LIVE_PID,
        worktree_path=wt, provenance=ownership.PROVENANCE_ARMED,
    )
    assert outcome.ok

    monkeypatch.setattr(mc, "_current_branch", lambda cwd: "my-branch")
    monkeypatch.setattr(mc, "_pr_draft_status_detailed", lambda cwd, pr, deadline=None: (False, ""))
    monkeypatch.setattr(mc, "_pr_head_branch_detailed", lambda cwd, pr, deadline=None: ("my-branch", ""))

    rc = mc.main([
        "arm", "--cwd", wt, "--session-id", "someone-else", "--pid", str(LIVE_PID),
        "--pr", "557",
    ])
    assert rc == 1
    err = capsys.readouterr().err
    assert "could not write arm marker" not in err, (
        f"arm's failure message must name the actual refusal and holder, not "
        f"the generic write-failure text (BOU-2409). Got: {err!r}"
    )
    assert "foreign-armer-2" in err, f"expected the holder's session id in the message. Got: {err!r}"
