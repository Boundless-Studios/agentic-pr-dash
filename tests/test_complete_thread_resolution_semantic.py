"""Semantic guardrails for `complete`'s review-thread auto-resolution.

BOU-1641 — `complete` must not auto-resolve a review thread solely because a
post-baseline commit touched the thread's ANCHOR file, when the comment body
requests a change in a DIFFERENT (untouched) file/module.

The confirmed symptom (gaia PR #2139): a thread anchored on
`backend/src/gaia/api/app.py` asked for a fix in `gaia.api.worker_app`
(`worker_app.py`). An unrelated commit touched `app.py`, so the old
`path in touched` heuristic auto-resolved the thread before `worker_app.py` was
ever fixed — stranding real feedback behind a resolved marker.

BOU-2095 — beyond BOU-1641, "anchor file touched" is not sufficient evidence at
all: resolution now requires the completing commits to have changed the
thread's ANCHORED HUNK (base..head diff intersects the anchor line ± context),
or an explicit completion-marker reply on the thread. GitHub's "outdated" flag
(pure line drift) never resolves anything, and the former BOU-1748
"anchor touched AND outdated" widening is removed.
"""

import argparse

from agentic_pr_dash import github_api, maintenance
from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash.github_api import COMPLETE_MARKER, ReviewThread, ReviewThreadComment
from agentic_pr_dash.models import PRData, PRStatus


ANCHOR = "backend/src/gaia/api/app.py"

# Spans intersecting the default anchor line (7): the fixing commits changed
# the thread's own hunk — the genuine-fix shape.
SPANS_AT_ANCHOR = [(6, 8, 6, 9)]
# Spans far from the default anchor line: same file touched, different hunk.
SPANS_ELSEWHERE_IN_FILE = [(100, 110, 100, 112)]


def _thread(body, *, path=ANCHOR, created="2026-01-01T00:00:00Z", outdated=False,
            line=7, original_line=None, replies=(), node_id="t1"):
    c = ReviewThreadComment(
        database_id=42, path=path, line=line, body=body,
        author="rev", created_at=created, original_line=original_line,
    )
    reply_comments = [
        ReviewThreadComment(
            database_id=43 + i, path=path, line=line, body=reply_body,
            author="bot", created_at=created,
        )
        for i, reply_body in enumerate(replies)
    ]
    return ReviewThread(node_id=node_id, is_resolved=False, is_outdated=outdated,
                        top=c, replies=reply_comments)


def _pr():
    return PRData(
        number=2139, title="t", branch="b", url="https://x/pull/2139",
        failing_checks=[], review_comments=[], merge_state="CLEAN",
        latest_commit_sha="headsha", latest_commit_date="2026-02-01T00:00:00Z",
        worktree_path="/wt", status=PRStatus.CLEAN,
    )


def _wire(monkeypatch, *, thread, touched_files, spans=SPANS_AT_ANCHOR,
          threads=None):
    """Stub the gh/GraphQL boundary so `_cmd_complete` runs offline.

    Records every `resolve_review_thread` call into ``resolved`` and every
    completion reply into ``replied`` so a test can assert whether the thread
    was (or was not) auto-resolved / replied to. ``spans`` is what the
    base..head diff reports as changed hunks for ANY anchor path (default: the
    fix changed the default anchor line's own hunk); pass ``None`` to model an
    unavailable diff.
    """
    resolved_calls: list[str] = []
    reply_calls: list[object] = []

    monkeypatch.setattr(mc, "_resolve_pr_by_number", lambda n, cwd, **kw: _pr())
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
    # ... changing exactly `spans` (hunk line ranges) in each touched file.
    monkeypatch.setattr(
        github_api, "get_changed_line_spans",
        lambda base, head, path, cwd=None: None if spans is None else list(spans),
    )
    all_threads = threads if threads is not None else [thread]
    monkeypatch.setattr(github_api, "get_review_threads",
                        lambda n, cwd=None: list(all_threads))

    def _resolve(node_id, cwd=None):
        resolved_calls.append(node_id)
        return True

    def _reply(pr_number, comment, body, cwd=None):
        reply_calls.append((comment.thread_id, body))
        return True

    monkeypatch.setattr(github_api, "resolve_review_thread", _resolve)
    monkeypatch.setattr(github_api, "reply_to_review_comment", _reply)
    # Short-circuit the post-resolve bead bookkeeping.
    monkeypatch.setattr(mc, "_mark_maintenance_complete", lambda *a, **k: None)
    monkeypatch.setattr(maintenance, "blockers_for_pr", lambda pr: [])

    return resolved_calls, reply_calls


def _args():
    return argparse.Namespace(cwd=".", pr=2139, baseline="basesha")


# --- negative: anchor touched, body points elsewhere -> DO NOT resolve --------

def test_anchor_touch_but_body_points_at_untouched_module_not_resolved(monkeypatch):
    thread = _thread(
        "Initialize logging for worker Cloud Run app too — see "
        "`gaia.api.worker_app` (worker_app.py)."
    )
    # The fixing commit touched ONLY the anchor file, never worker_app.py.
    resolved, replied = _wire(monkeypatch, thread=thread, touched_files=[ANCHOR])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    # Ambiguous: requested file (worker_app.py) was never touched -> left OPEN.
    assert resolved == []
    assert replied == []
    # And it stays counted as unresolved so the prompt re-surfaces it.
    monkeypatch.setattr(github_api, "get_review_threads", lambda n, cwd=None: [thread])
    assert mc.pr_has_unresolved_review_threads(2139, ".") is True


def test_anchor_touch_but_body_points_at_untouched_path_not_resolved(monkeypatch):
    thread = _thread("The real change belongs in backend/src/gaia/api/worker_app.py")
    resolved, _ = _wire(monkeypatch, thread=thread, touched_files=[ANCHOR])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []


# --- positive: anchored hunk changed, body has no other-file ref -> resolves ---

def test_anchored_hunk_changed_body_no_other_ref_still_resolves(monkeypatch):
    thread = _thread("Please add a docstring and fix the typo here.")
    resolved, replied = _wire(monkeypatch, thread=thread, touched_files=[ANCHOR])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    # Clear case — the fix changed the thread's own hunk, no untouched file
    # referenced.
    assert resolved == ["t1"]
    assert [t for t, _ in replied] == ["t1"]


def test_anchored_hunk_changed_body_references_the_anchor_file_still_resolves(monkeypatch):
    # Body references its own anchor module — that is NOT "elsewhere".
    thread = _thread("Fix the logging init in app.py / gaia.api.app")
    resolved, _ = _wire(monkeypatch, thread=thread, touched_files=[ANCHOR])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == ["t1"]


def test_anchored_hunk_changed_body_references_a_touched_other_file_still_resolves(monkeypatch):
    # Body points at worker_app.py AND the fixing commit DID touch it -> clear.
    thread = _thread("Initialize logging in gaia.api.worker_app too (worker_app.py).")
    resolved, _ = _wire(
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


# --- BOU-2095 supersedes BOU-1748: "outdated" never widens resolution ----------

DECK_CONF = "worktree-deck.conf"
OTHER_PY = "scripts/cleanup-orphan-worktrees.py"


def test_bou2095_outdated_plus_elsewhere_mention_no_longer_resolves(monkeypatch):
    # BOU-1748's former widening: thread anchored on worktree-deck.conf, the fix
    # touched worktree-deck.conf, GitHub marks the thread outdated, and the body
    # also mentions scripts/cleanup-orphan-worktrees.py (untouched). BOU-2095
    # removes the widening: "outdated" is line drift, not evidence, and a
    # cross-file ask stays ambiguous even when the anchored hunk changed —
    # the thread stays OPEN for a human.
    thread = _thread(
        "Bump the orphan-sweep age here; this is what "
        "`scripts/cleanup-orphan-worktrees.py` reads.",
        path=DECK_CONF,
        outdated=True,
    )
    resolved, replied = _wire(monkeypatch, thread=thread, touched_files=[DECK_CONF])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []
    assert replied == []


def test_anchor_touched_but_not_outdated_body_points_elsewhere_stays_open(monkeypatch):
    # Same shape but the thread is NOT outdated: the BOU-1641 guard must still
    # hold so a real "fix belongs in the other file" comment is not lost.
    thread = _thread(
        "Bump the orphan-sweep age here; this is what "
        "`scripts/cleanup-orphan-worktrees.py` reads.",
        path=DECK_CONF,
        outdated=False,
    )
    resolved, _ = _wire(monkeypatch, thread=thread, touched_files=[DECK_CONF])

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []


def test_leaving_open_message_names_the_conflicting_path(monkeypatch, capsys):
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


# --- BOU-2095: hunk-level evidence required; drift/file-touch never resolve ----


def test_bou2095_file_touched_but_anchored_hunk_untouched_not_resolved(monkeypatch):
    # Case (a): thread on ANCHOR line 7; the completing commit touched ANCHOR
    # but only changed lines 100-110 — a different hunk. Must stay OPEN with no
    # "Addressed by" reply.
    thread = _thread("Guard against a None campaign here.")
    resolved, replied = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_ELSEWHERE_IN_FILE,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []
    assert replied == []


def test_bou2095_pure_line_drift_outdated_not_resolved(monkeypatch):
    # Case (b): the commit inserted lines at the TOP of the file, so GitHub
    # marks the thread outdated (line=None, originalLine kept) — pure drift.
    # The anchored hunk (line 50) never changed. Must stay OPEN.
    thread = _thread(
        "Rename this variable for clarity.",
        outdated=True, line=None, original_line=50,
    )
    # Insertion at top: old side empty (end < start), new lines 1-3 added.
    resolved, replied = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=[(1, 0, 1, 3)],
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []
    assert replied == []


def test_bou2095_anchored_hunk_content_changed_resolves(monkeypatch):
    # Case (c): the completing commit modified the anchored hunk itself —
    # genuine auto-resolution is preserved, with the completion reply.
    thread = _thread("Guard against a None campaign here.")
    resolved, replied = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_AT_ANCHOR,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == ["t1"]
    assert [t for t, _ in replied] == ["t1"]


def test_bou2095_multi_thread_same_file_only_fixed_hunk_resolves(monkeypatch):
    # Case (d): two threads on the same file; the fix changed only t1's hunk
    # (line 7). t2 (line 200) must stay OPEN.
    t1 = _thread("Fix the off-by-one here.", node_id="t1", line=7)
    t2 = _thread("This branch needs a docstring.", node_id="t2", line=200)
    resolved, replied = _wire(
        monkeypatch, thread=t1, threads=[t1, t2], touched_files=[ANCHOR],
        spans=SPANS_AT_ANCHOR,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == ["t1"]
    assert [t for t, _ in replied] == ["t1"]


def test_bou2095_outdated_anchored_hunk_changed_still_resolves(monkeypatch):
    # Outdated is not a blocker either: when the base..head diff shows the
    # ORIGINAL anchor line's hunk was rewritten, that is positive evidence.
    thread = _thread(
        "Guard against a None campaign here.",
        outdated=True, line=None, original_line=7,
    )
    resolved, _ = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_AT_ANCHOR,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == ["t1"]


def test_bou2095_diff_unavailable_is_no_evidence_not_resolved(monkeypatch):
    # Absence of proof is not proof: when the base..head diff cannot be
    # computed (spans=None), the thread stays OPEN even though the file was
    # touched.
    thread = _thread("Guard against a None campaign here.")
    resolved, replied = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR], spans=None,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []
    assert replied == []


def test_bou2095_existing_completion_reply_resolves_as_retry(monkeypatch):
    # An earlier `complete` run already posted the completion-marker reply but
    # the resolve mutation failed. Retrying resolves even without hunk
    # intersection — the reply IS the explicit per-thread evidence.
    thread = _thread(
        "Guard against a None campaign here.",
        replies=(f"{COMPLETE_MARKER}\nAddressed by the local maintenance loop.",),
    )
    resolved, _ = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_ELSEWHERE_IN_FILE,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == ["t1"]


def test_pr78_stale_marker_after_reviewer_followup_does_not_resolve(monkeypatch):
    # PR #78 review (comment 3605704909): a historical completion marker whose
    # thread was REOPENED by a later human follow-up is NOT evidence — the
    # reviewer rejected the completion. Without hunk evidence the thread must
    # stay open instead of short-circuiting on the stale marker.
    thread = _thread(
        "Guard against a None campaign here.",
        replies=(
            f"{COMPLETE_MARKER}\nAddressed by the local maintenance loop.",
            "This was NOT addressed — the guard is still missing.",
        ),
    )
    resolved, replied = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_ELSEWHERE_IN_FILE,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []
    assert replied == []


def test_pr78_head_side_anchor_matching_only_old_span_not_resolved(monkeypatch):
    # PR #78 review (comment 3605704914): a live thread's `line` is a HEAD-side
    # coordinate. A rewrite whose OLD-side span happens to overlap that number
    # (48-52) while the new-side content moved elsewhere (120-124) must not
    # count as hunk evidence.
    thread = _thread("Guard against a None campaign here.", line=50)
    resolved, replied = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=[(48, 52, 120, 124)],
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []
    assert replied == []


def test_pr78_outdated_anchor_matching_only_new_span_not_resolved(monkeypatch):
    # Mirror case: an outdated thread's `original_line` is a BASE-side
    # coordinate; overlap with only the new-side span is not evidence.
    thread = _thread(
        "Guard against a None campaign here.",
        outdated=True, line=None, original_line=122,
    )
    resolved, _ = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=[(48, 52, 120, 124)],
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []


def test_pr78_left_open_outdated_thread_keeps_review_comments_blocker(
        monkeypatch, capsys):
    # PR #78 review (comment 3605704917, P1): a drift-outdated thread the gate
    # deliberately leaves open must keep the bead open as a review_comments
    # blocker — the downstream scan filters skip is_outdated, so without this
    # `complete` would report "no blockers remain" and close the bead.
    thread = _thread(
        "Rename this variable for clarity.",
        outdated=True, line=None, original_line=50,
    )
    resolved, _ = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=[(1, 0, 1, 3)],
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == []
    captured = capsys.readouterr()
    assert "bead left open" in captured.out
    assert "review_comments" in captured.out
    assert "remain unresolved" in captured.err


def test_bou2095_file_level_comment_resolves_on_file_content_change(monkeypatch):
    # A file-level comment (no line anchor at all) anchors the whole file, so
    # any content change in the file is anchored-hunk evidence.
    thread = _thread("Please add a module docstring.", line=None, original_line=None)
    resolved, _ = _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_ELSEWHERE_IN_FILE,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    assert resolved == ["t1"]


def test_bou2095_leaving_open_message_explains_hunk_mismatch(monkeypatch, capsys):
    thread = _thread("Guard against a None campaign here.")
    _wire(
        monkeypatch, thread=thread, touched_files=[ANCHOR],
        spans=SPANS_ELSEWHERE_IN_FILE,
    )

    rc = mc._cmd_complete(_args())

    assert rc == 0
    err = capsys.readouterr().err
    assert "leaving thread t1 open" in err
    assert "file-touch/line-drift alone" in err


# --- direct unit coverage of the span helpers ----------------------------------


def test_spans_intersect_line_helper():
    spans = [(10, 12, 10, 14)]
    # Inside the hunk, matched on the anchor's own side.
    assert mc._spans_intersect_line(spans, 11, "new") is True
    assert mc._spans_intersect_line(spans, 11, "old") is True
    # Within the ±context fuzz.
    assert mc._spans_intersect_line(spans, 14 + mc._ANCHOR_CONTEXT_LINES, "new") is True
    assert mc._spans_intersect_line(spans, 10 - mc._ANCHOR_CONTEXT_LINES, "new") is True
    # Beyond the fuzz.
    assert mc._spans_intersect_line(spans, 14 + mc._ANCHOR_CONTEXT_LINES + 1, "new") is False
    assert mc._spans_intersect_line(spans, 1, "new") is False
    # PR #78 review: each coordinate is compared ONLY against its own diff
    # side — old-side overlap is not evidence for a head-side anchor and
    # vice versa.
    disjoint = [(48, 52, 120, 124)]
    assert mc._spans_intersect_line(disjoint, 50, "old") is True
    assert mc._spans_intersect_line(disjoint, 50, "new") is False
    assert mc._spans_intersect_line(disjoint, 122, "new") is True
    assert mc._spans_intersect_line(disjoint, 122, "old") is False
    # Empty side (pure insertion: old side (1, 0)) never matches on that side.
    assert mc._spans_intersect_line([(1, 0, 1, 3)], 50, "old") is False
    assert mc._spans_intersect_line([(1, 0, 1, 3)], 2, "old") is False
    assert mc._spans_intersect_line([], 5, "new") is False


def test_get_changed_line_spans_parses_real_git_diff(tmp_path):
    import subprocess

    def _git(*args):
        subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True, capture_output=True, text=True,
        )

    _git("init", "-q")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    f = tmp_path / "a.py"
    f.write_text("".join(f"line{i}\n" for i in range(1, 21)))
    _git("add", "a.py")
    _git("commit", "-qm", "base")
    base = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    # Change line 5 and delete line 15.
    lines = f.read_text().splitlines(keepends=True)
    lines[4] = "line5-changed\n"
    del lines[14]
    f.write_text("".join(lines))
    _git("commit", "-aqm", "fix")
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    spans = github_api.get_changed_line_spans(base, head, "a.py", str(tmp_path))

    # Deletion hunk: old line 15 removed; git reports the new side as
    # `+14,0` -> empty new span encoded (14, 13).
    assert spans == [(5, 5, 5, 5), (15, 15, 14, 13)]
    # Unknown SHA -> None (no evidence), not [].
    assert github_api.get_changed_line_spans(
        "0" * 40, head, "a.py", str(tmp_path)) is None
    # Untouched path -> [] (real evidence of no change).
    assert github_api.get_changed_line_spans(base, head, "other.py", str(tmp_path)) == []
