"""BOU-2567 — the `complete --defer` / `complete --sweep-p2` CLI surface.

Covers the anti-abuse enforcement end-to-end through the CLI (not just the
state-store unit tests in test_deferred_review_state.py), the GitHub-reply
contract ("every comment gets a reply"), and the deliberate absence of a
`resolve_review_thread` call (resolving would erase the deferral — see
BOU-2567's diagnosis of why option 2, "resolve-with-tracking", was rejected).
"""
from __future__ import annotations

import argparse

import pytest

from agentic_pr_dash import github_api
from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash.github_api import (
    ReviewSubmission,
    ReviewThread,
    ReviewThreadComment,
)
from agentic_pr_dash.models import PRData, PRStatus
from agentic_pr_dash._maintenance import deferred_review as dr

PR_NUMBER = 4242


def _thread(node_id: str = "T1", *, is_resolved: bool = False, body: str = "please fix") -> ReviewThread:
    c = ReviewThreadComment(
        database_id=99, path="f.py", line=3, body=body,
        author="rev", created_at="2026-01-01T00:00:00Z",
    )
    return ReviewThread(node_id=node_id, is_resolved=is_resolved, is_outdated=False, top=c)


def _pr() -> PRData:
    return PRData(
        number=PR_NUMBER, title="t", branch="b", url=f"https://x/pull/{PR_NUMBER}",
        worktree_path="/wt", status=PRStatus.CLEAN,
        latest_commit_sha="a" * 40, author="pr-author",
    )


def _wire(monkeypatch, threads):
    resolved_calls: list[str] = []
    reply_calls: list[tuple] = []
    monkeypatch.setattr(mc, "_resolve_pr_by_number", lambda n, cwd, **kw: _pr())
    monkeypatch.setattr(mc, "_resolve_pr_for_branch", lambda cwd, **kw: _pr())
    monkeypatch.setattr(github_api, "get_review_threads", lambda n, cwd=None: list(threads))

    def _resolve(node_id, cwd=None):
        resolved_calls.append(node_id)
        return True

    def _reply(pr_number, comment, body, cwd=None):
        reply_calls.append((comment.thread_id, body))
        return True

    monkeypatch.setattr(github_api, "resolve_review_thread", _resolve)
    monkeypatch.setattr(github_api, "reply_to_review_comment", _reply)
    return resolved_calls, reply_calls


def _args(**overrides):
    base = dict(
        cwd=".", pr=PR_NUMBER, defer=None, sweep_p2=False,
        severity=None, ticket=None, reason="", session_id="sess-1",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# --defer
# ---------------------------------------------------------------------------


def test_defer_persists_state_and_replies_without_resolving(tmp_path, monkeypatch) -> None:
    thread = _thread()
    resolved, replied = _wire(monkeypatch, [thread])

    rc = mc._cmd_complete(_args(
        cwd=str(tmp_path), defer="T1", severity="P2", ticket=None,
        reason="unsupported provider path with no observed occurrence",
    ))

    assert rc == 0
    assert dr.is_thread_deferred(str(tmp_path), PR_NUMBER, "T1") is True
    assert resolved == [], "defer must NEVER resolve the GitHub thread"
    assert len(replied) == 1
    assert replied[0][0] == "T1"
    assert "unsupported provider path" in replied[0][1]


def test_defer_without_ticket_succeeds_with_reason(tmp_path, monkeypatch) -> None:
    thread = _thread()
    _wire(monkeypatch, [thread])

    rc = mc._cmd_complete(
        _args(
            cwd=str(tmp_path),
            defer="T1",
            severity="P2",
            ticket=None,
            reason="No supported-path reproduction.",
        )
    )

    assert rc == 0
    assert dr.is_thread_deferred(str(tmp_path), PR_NUMBER, "T1") is True


def test_defer_p1_without_reason_fails_and_does_not_persist(tmp_path, monkeypatch) -> None:
    thread = _thread()
    _wire(monkeypatch, [thread])

    rc = mc._cmd_complete(_args(
        cwd=str(tmp_path), defer="T1", severity="P1", ticket="BOU-1", reason="",
    ))

    assert rc == 1
    assert dr.is_thread_deferred(str(tmp_path), PR_NUMBER, "T1") is False


def test_defer_p2_without_reason_fails(tmp_path, monkeypatch) -> None:
    thread = _thread()
    _wire(monkeypatch, [thread])

    rc = mc._cmd_complete(_args(
        cwd=str(tmp_path), defer="T1", severity="P2", ticket="BOU-1", reason="",
    ))

    assert rc == 1
    assert dr.is_thread_deferred(str(tmp_path), PR_NUMBER, "T1") is False


# --- BOU-2567 PR #122 review, P1 #2: severity is verified, not merely trusted ---


def test_defer_refuses_a_detected_p1_thread_labeled_p2(tmp_path, monkeypatch) -> None:
    """`_thread_is_p1` recognizes the thread's own body as P1; an operator
    passing --severity P2 must be refused, not silently honored — a P1
    mislabeled P2 skips the P1-only --reason requirement entirely, which is
    exactly the mute-button abuse the anti-abuse rule exists to stop."""
    thread = _thread(body="P1: this null pointer will crash in production")
    resolved, replied = _wire(monkeypatch, [thread])

    rc = mc._cmd_complete(_args(
        cwd=str(tmp_path), defer="T1", severity="P2", ticket="BOU-1",
    ))

    assert rc == 1
    assert dr.is_thread_deferred(str(tmp_path), PR_NUMBER, "T1") is False
    assert resolved == []
    assert replied == []


def test_defer_does_not_silently_upgrade_severity(tmp_path, monkeypatch) -> None:
    """The mismatch must be a hard refusal, not a silent rewrite to P1 — an
    operator's mismatched input is a trust problem either way it's resolved
    silently."""
    thread = _thread(body="P1: this null pointer will crash in production")
    _wire(monkeypatch, [thread])

    rc = mc._cmd_complete(_args(
        cwd=str(tmp_path), defer="T1", severity="P2", ticket="BOU-1",
    ))

    assert rc == 1
    # Not persisted under EITHER severity — a clean refusal, no silent rewrite.
    assert dr.deferred_count_for_pr(str(tmp_path), PR_NUMBER) == 0


def test_defer_refuses_detected_p1_even_with_reason(tmp_path, monkeypatch) -> None:
    thread = _thread(body="P1: this null pointer will crash in production")
    _wire(monkeypatch, [thread])

    rc = mc._cmd_complete(_args(
        cwd=str(tmp_path), defer="T1", severity="P1", ticket="BOU-1",
        reason="out of scope: requires files this PR does not own",
    ))

    assert rc == 1
    assert dr.is_thread_deferred(str(tmp_path), PR_NUMBER, "T1") is False


def test_defer_refuses_operator_labeled_p1(tmp_path, monkeypatch) -> None:
    thread = _thread(body="just a minor nit, nothing urgent")
    _wire(monkeypatch, [thread])

    rc = mc._cmd_complete(_args(
        cwd=str(tmp_path), defer="T1", severity="P1", ticket="BOU-1",
        reason="operator judgment: treat as P1 anyway",
    ))

    assert rc == 1
    assert dr.deferred_count_for_pr(str(tmp_path), PR_NUMBER) == 0


def test_defer_unknown_thread_id_fails(tmp_path, monkeypatch) -> None:
    _wire(monkeypatch, [_thread(node_id="OTHER")])

    rc = mc._cmd_complete(_args(
        cwd=str(tmp_path), defer="NOPE", severity="P2", ticket="BOU-1",
    ))

    assert rc == 1


def test_defer_reply_failure_still_persists_the_deferral(tmp_path, monkeypatch) -> None:
    """The state store is the durable fact; a flaky reply must not lose it —
    the CLI can re-run to retry the reply without re-litigating anything."""
    thread = _thread()
    _wire(monkeypatch, [thread])
    monkeypatch.setattr(github_api, "reply_to_review_comment", lambda *a, **k: False)

    rc = mc._cmd_complete(_args(
        cwd=str(tmp_path), defer="T1", severity="P2", ticket="BOU-1",
        reason="No supported-path reproduction.",
    ))

    assert rc == 0
    assert dr.is_thread_deferred(str(tmp_path), PR_NUMBER, "T1") is True


def test_defer_review_body_p2_by_review_id(tmp_path, monkeypatch) -> None:
    _, replied = _wire(monkeypatch, [])
    monkeypatch.setattr(
        github_api,
        "get_review_submissions",
        lambda *args, **kwargs: [
            ReviewSubmission(
                review_id=123,
                author="review-bot",
                state="COMMENTED",
                commit_id="a" * 40,
                submitted_at="2026-07-28T00:00:00Z",
                body="[P2] Defend an unobserved edge case.",
            )
        ],
    )

    rc = mc._cmd_complete(
        _args(
            cwd=str(tmp_path),
            defer="review:123",
            severity="P2",
            ticket=None,
            reason="No supported-path reproduction.",
        )
    )

    assert rc == 0
    assert dr.is_thread_deferred(
        str(tmp_path),
        PR_NUMBER,
        "review:123",
    )
    assert replied[0][0] is None
    assert "No supported-path reproduction." in replied[0][1]


def test_defer_review_body_requires_ordinal_for_multiple_findings(
    tmp_path,
    monkeypatch,
) -> None:
    _, replied = _wire(monkeypatch, [])
    monkeypatch.setattr(
        github_api,
        "get_review_submissions",
        lambda *args, **kwargs: [
            ReviewSubmission(
                review_id=123,
                author="review-bot",
                state="COMMENTED",
                commit_id="a" * 40,
                submitted_at="2026-07-28T00:00:00Z",
                body="[P2] First edge.\n[P2] Second edge.",
            )
        ],
    )

    ambiguous = mc._cmd_complete(
        _args(
            cwd=str(tmp_path),
            defer="review:123",
            severity="P2",
            reason="Must be individual.",
        )
    )
    second = mc._cmd_complete(
        _args(
            cwd=str(tmp_path),
            defer="review:123:2",
            severity="P2",
            reason="Second has no supported-path reproduction.",
        )
    )

    assert ambiguous == 1
    assert second == 0
    assert dr.is_thread_deferred(
        str(tmp_path),
        PR_NUMBER,
        "review:123:2",
    )
    assert len(replied) == 1


# ---------------------------------------------------------------------------
# --sweep-p2
# ---------------------------------------------------------------------------


def test_sweep_p2_is_disabled(tmp_path, monkeypatch) -> None:
    p2_a = _thread(node_id="A", body="minor nit")
    p2_b = _thread(node_id="B", body="another minor nit")
    resolved, replied = _wire(monkeypatch, [p2_a, p2_b])

    rc = mc._cmd_complete(_args(cwd=str(tmp_path), sweep_p2=True, ticket="BOU-9000"))

    assert rc == 1
    assert resolved == []
    assert dr.deferred_count_for_pr(str(tmp_path), PR_NUMBER) == 0
    assert replied == []


def test_sweep_p2_skips_p1_threads(tmp_path, monkeypatch) -> None:
    p1 = _thread(node_id="P1THREAD", body="P1: this is broken and must be fixed here")
    _wire(monkeypatch, [p1])

    rc = mc._cmd_complete(_args(cwd=str(tmp_path), sweep_p2=True, ticket="BOU-9000"))

    assert rc == 1
    assert dr.is_thread_deferred(str(tmp_path), PR_NUMBER, "P1THREAD") is False


def test_sweep_p2_skips_already_deferred_threads(tmp_path, monkeypatch) -> None:
    already = _thread(node_id="A")
    _wire(monkeypatch, [already])
    dr.defer_thread(str(tmp_path), PR_NUMBER, thread_id="A", comment_id=1,
                    severity="P1", ticket="BOU-1", reason="already handled")

    rc = mc._cmd_complete(_args(cwd=str(tmp_path), sweep_p2=True, ticket="BOU-9000"))

    assert rc == 1
    # Untouched — still the ORIGINAL P1 ticket, not overwritten by the sweep.
    record = dr.deferred_threads_for_pr(str(tmp_path), PR_NUMBER)["A"]
    assert record["ticket"] == "BOU-1"
    assert record["severity"] == "P1"


def test_sweep_p2_requires_a_valid_ticket(tmp_path, monkeypatch) -> None:
    _wire(monkeypatch, [_thread()])

    rc = mc._cmd_complete(_args(cwd=str(tmp_path), sweep_p2=True, ticket=""))

    assert rc == 1
    assert dr.deferred_count_for_pr(str(tmp_path), PR_NUMBER) == 0


def test_defer_wired_through_real_argparse_main(tmp_path, monkeypatch) -> None:
    """End-to-end through `mc.main([...])` — not just `_cmd_complete` called
    directly — so an argparse dest-name typo (e.g. sweep_p2 vs sweep-p2) would
    actually fail this test."""
    thread = _thread()
    _wire(monkeypatch, [thread])

    rc = mc.main([
        "complete", "--defer", "T1", "--severity", "P2",
        "--reason", "No supported-path reproduction.",
        "--cwd", str(tmp_path), "--pr", str(PR_NUMBER),
    ])

    assert rc == 0
    assert dr.is_thread_deferred(str(tmp_path), PR_NUMBER, "T1") is True


def test_sweep_p2_skips_resolved_threads(tmp_path, monkeypatch) -> None:
    resolved_thread = _thread(node_id="R", is_resolved=True)
    _wire(monkeypatch, [resolved_thread])

    rc = mc._cmd_complete(_args(cwd=str(tmp_path), sweep_p2=True, ticket="BOU-9000"))

    assert rc == 1
    assert dr.is_thread_deferred(str(tmp_path), PR_NUMBER, "R") is False
