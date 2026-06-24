"""BOU-1641 — `complete` must not auto-resolve a review thread solely because a
post-baseline commit touched the thread's ANCHOR file, when the comment body
requests a change in a DIFFERENT (untouched) file/module.

The confirmed symptom (gaia PR #2139): a thread anchored on
`backend/src/gaia/api/app.py` asked for a fix in `gaia.api.worker_app`
(`worker_app.py`). An unrelated commit touched `app.py`, so the old
`path in touched` heuristic auto-resolved the thread before `worker_app.py` was
ever fixed — stranding real feedback behind a resolved marker.
"""

import argparse

from agentic_pr_dash import github_api, maintenance
from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash.github_api import ReviewThread, ReviewThreadComment
from agentic_pr_dash.models import PRData, PRStatus


ANCHOR = "backend/src/gaia/api/app.py"


def _thread(body, *, path=ANCHOR, created="2026-01-01T00:00:00Z", outdated=False):
    c = ReviewThreadComment(
        database_id=42, path=path, line=7, body=body,
        author="rev", created_at=created,
    )
    return ReviewThread(node_id="t1", is_resolved=False, is_outdated=outdated, top=c)


def _pr():
    return PRData(
        number=2139, title="t", branch="b", url="https://x/pull/2139",
        failing_checks=[], review_comments=[], merge_state="CLEAN",
        latest_commit_sha="headsha", latest_commit_date="2026-02-01T00:00:00Z",
        worktree_path="/wt", status=PRStatus.CLEAN,
    )


def _wire(monkeypatch, *, thread, touched_files):
    """Stub the gh/GraphQL boundary so `_cmd_complete` runs offline.

    Records every `resolve_review_thread` call into the returned list so a test
    can assert whether the thread was (or was not) auto-resolved.
    """
    resolved_calls: list[str] = []

    monkeypatch.setattr(mc, "_resolve_pr_by_number", lambda n, cwd: _pr())
    # No local-head override: keep the API head/date.
    monkeypatch.setattr(github_api, "get_local_pr_head", lambda branch, cwd: ("", ""))
    monkeypatch.setattr(github_api, "_is_ancestor", lambda a, d, cwd: False)
    # One post-baseline commit exists (a real fixing push landed) ...
    monkeypatch.setattr(
        github_api, "get_new_pr_commits",
        lambda *a, **k: [("c0ffee", "fix: logging")],
    )
    # ... and it touched exactly `touched_files`.
    monkeypatch.setattr(
        github_api, "get_commit_changed_files",
        lambda sha, cwd=None: list(touched_files),
    )
    monkeypatch.setattr(github_api, "get_review_threads", lambda n, cwd=None: [thread])

    def _resolve(node_id, cwd=None):
        resolved_calls.append(node_id)
        return True

    monkeypatch.setattr(github_api, "resolve_review_thread", _resolve)
    monkeypatch.setattr(github_api, "reply_to_review_comment", lambda *a, **k: True)
    # Short-circuit the post-resolve bead bookkeeping.
    monkeypatch.setattr(mc, "_mark_maintenance_complete", lambda *a, **k: None)
    monkeypatch.setattr(maintenance, "blockers_for_pr", lambda pr: [])

    return resolved_calls


def _args():
    return argparse.Namespace(cwd=".", pr=2139, baseline="basesha")


# --- negative: anchor touched, body points elsewhere -> DO NOT resolve --------

def test_anchor_touch_but_body_points_at_untouched_module_not_resolved(monkeypatch):
    thread = _thread(
        "Initialize logging for worker Cloud Run app too — see "
        "`gaia.api.worker_app` (worker_app.py)."
    )
    # The fixing commit touched ONLY the anchor file, never worker_app.py.
    resolved = _wire(monkeypatch, thread=thread, touched_files=[ANCHOR])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    # Ambiguous: requested file (worker_app.py) was never touched -> left OPEN.
    assert resolved == []
    # And it stays counted as unresolved so the prompt re-surfaces it.
    monkeypatch.setattr(github_api, "get_review_threads", lambda n, cwd=None: [thread])
    assert mc.pr_has_unresolved_review_threads(2139, ".") is True


def test_anchor_touch_but_body_points_at_untouched_path_not_resolved(monkeypatch):
    thread = _thread("The real change belongs in backend/src/gaia/api/worker_app.py")
    resolved = _wire(monkeypatch, thread=thread, touched_files=[ANCHOR])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []


# --- positive: anchor touched, body has no other-file ref -> still resolves ----

def test_anchor_touch_body_no_other_ref_still_resolves(monkeypatch):
    thread = _thread("Please add a docstring and fix the typo here.")
    resolved = _wire(monkeypatch, thread=thread, touched_files=[ANCHOR])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    # Clear case — anchor file touched, no untouched file referenced.
    assert resolved == ["t1"]


def test_anchor_touch_body_references_the_anchor_file_still_resolves(monkeypatch):
    # Body references its own anchor module — that is NOT "elsewhere".
    thread = _thread("Fix the logging init in app.py / gaia.api.app")
    resolved = _wire(monkeypatch, thread=thread, touched_files=[ANCHOR])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == ["t1"]


def test_anchor_touch_body_references_a_touched_other_file_still_resolves(monkeypatch):
    # Body points at worker_app.py AND the fixing commit DID touch it -> clear.
    thread = _thread("Initialize logging in gaia.api.worker_app too (worker_app.py).")
    resolved = _wire(
        monkeypatch, thread=thread,
        touched_files=[ANCHOR, "backend/src/gaia/api/worker_app.py"],
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == ["t1"]


# --- direct unit coverage of the heuristic helper -----------------------------

def test_thread_points_elsewhere_helper():
    touched = {ANCHOR}
    # References an untouched module -> ambiguous.
    assert mc._thread_points_elsewhere(
        "see gaia.api.worker_app", ANCHOR, touched) is True
    # References only its own anchor -> not elsewhere.
    assert mc._thread_points_elsewhere(
        "fix app.py here", ANCHOR, touched) is False
    # No file/module reference at all -> not elsewhere.
    assert mc._thread_points_elsewhere(
        "add a docstring", ANCHOR, touched) is False
    # Prose dotted token (e.g.) must not trip the gate.
    assert mc._thread_points_elsewhere(
        "do this, e.g. add a guard", ANCHOR, touched) is False


# --- BOU-1748: anchor touched AND thread outdated -> anchor evidence wins ------

DECK_CONF = "worktree-deck.conf"
OTHER_PY = "scripts/cleanup-orphan-worktrees.py"


def test_bou1748_anchor_conf_touched_and_outdated_body_mentions_other_resolves(monkeypatch):
    # Exact reproduction context: thread anchored on worktree-deck.conf, the fix
    # touched worktree-deck.conf, GitHub marks the thread outdated, and the body
    # also mentions scripts/cleanup-orphan-worktrees.py (contextual, untouched).
    thread = _thread(
        "Bump the orphan-sweep age here; this is what "
        "`scripts/cleanup-orphan-worktrees.py` reads.",
        path=DECK_CONF,
        outdated=True,
    )
    resolved = _wire(monkeypatch, thread=thread, touched_files=[DECK_CONF])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    # Anchor file changed + thread outdated -> the body mention of another file
    # no longer blocks; the thread resolves.
    assert resolved == ["t1"]


def test_bou1748_anchor_touched_but_not_outdated_body_points_elsewhere_stays_open(monkeypatch):
    # Same shape but the thread is NOT outdated: the BOU-1641 guard must still
    # hold so a real "fix belongs in the other file" comment is not lost.
    thread = _thread(
        "Bump the orphan-sweep age here; this is what "
        "`scripts/cleanup-orphan-worktrees.py` reads.",
        path=DECK_CONF,
        outdated=False,
    )
    resolved = _wire(monkeypatch, thread=thread, touched_files=[DECK_CONF])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []


def test_bou1748_leaving_open_message_names_the_conflicting_path(monkeypatch, capsys):
    thread = _thread(
        "The real change belongs in backend/src/gaia/api/worker_app.py",
        outdated=False,
    )
    _wire(monkeypatch, thread=thread, touched_files=[ANCHOR])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    err = capsys.readouterr().err
    # AC: when still left open, the message explains the specific conflicting path.
    assert "backend/src/gaia/api/worker_app.py" in err
    assert "ambiguous resolution" in err


def test_thread_elsewhere_refs_helper_returns_conflicting_refs():
    touched = {ANCHOR}
    # Untouched module reference is reported.
    assert mc._thread_elsewhere_refs(
        "see gaia.api.worker_app", ANCHOR, touched) == ["gaia.api.worker_app"]
    # Only the anchor / touched refs -> nothing points elsewhere.
    assert mc._thread_elsewhere_refs("fix app.py here", ANCHOR, touched) == []
    assert mc._thread_elsewhere_refs("add a docstring", ANCHOR, touched) == []
    # De-duplicated, in body order.
    assert mc._thread_elsewhere_refs(
        "scripts/a.py then scripts/a.py then scripts/b.py",
        DECK_CONF, {DECK_CONF},
    ) == ["scripts/a.py", "scripts/b.py"]
