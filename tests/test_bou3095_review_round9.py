"""BOU-3095 PR #169 review round 9 (codex).

``probe_open_prs_rest`` resolves the ``@me`` sentinel through
``_rest_viewer_login``, which returns ``""`` on failure — explicitly including a
GitHub App installation token, which cannot call ``/user``. An empty author then
made ``expected_author`` empty, and the filter loop skips filtering entirely, so
the probe reported EVERY author's PRs as the tracked projection.

``_rest_viewer_login``'s own docstring says callers "must then fail closed
rather than adopt a PR whose author it cannot verify", so this was violating a
contract its dependency documents. It also feeds directly into the round-6
projection comparison: unrelated authors' activity reads as a tracked change and
schedules a rich GraphQL relist, reintroducing the exact quota drain that
comparison exists to prevent.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from agentic_pr_dash import github_api


def _rest_item(number: int, login: str) -> dict:
    return {
        "number": number,
        "user": {"login": login},
        "head": {"sha": "h", "ref": f"f/{number}", "repo": {"owner": {}}},
        "base": {"ref": "main"},
        "html_url": f"https://github.com/org/widgets/pull/{number}",
        "updated_at": "2026-08-22T00:00:00Z",
    }


def _fake_run(payload: str, *, viewer_ok: bool):
    def run(cmd, cwd=None, timeout_s=30):
        joined = " ".join(cmd)
        if "user" in cmd and "--jq" in joined:
            if viewer_ok:
                return subprocess.CompletedProcess(cmd, 0, stdout="alice\n", stderr="")
            # App installation token: /user is not callable.
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="Resource not accessible by integration"
            )
        return subprocess.CompletedProcess(
            cmd, 0, stdout=f'HTTP/2 200 OK\r\nETag: "v1"\r\n\r\n{payload}', stderr=""
        )

    return run


def test_unresolvable_me_makes_the_probe_unobservable(monkeypatch) -> None:
    """Fail closed: an unfiltered list is not this author's open set."""
    payload = json.dumps(
        [_rest_item(1, "someone-else"), _rest_item(2, "another-person")]
    )
    monkeypatch.setattr(github_api, "_run", _fake_run(payload, viewer_ok=False))

    probe = github_api.probe_open_prs_rest("org", "widgets", author="@me")

    assert probe.observable is False, (
        "an unresolvable @me produced a probe carrying every author's PRs as if "
        "they were the tracked projection"
    )
    assert probe.prs == []
    assert probe.error


def test_resolvable_me_filters_normally(monkeypatch) -> None:
    payload = json.dumps([_rest_item(1, "alice"), _rest_item(2, "someone-else")])
    monkeypatch.setattr(github_api, "_run", _fake_run(payload, viewer_ok=True))

    probe = github_api.probe_open_prs_rest("org", "widgets", author="@me")

    assert probe.observable is True
    assert [pr["number"] for pr in probe.prs] == [1]


def test_explicit_author_is_unaffected_by_viewer_lookup(monkeypatch) -> None:
    """A configured login never needs /user, so a broken token is irrelevant."""
    payload = json.dumps([_rest_item(1, "alice"), _rest_item(2, "someone-else")])
    monkeypatch.setattr(github_api, "_run", _fake_run(payload, viewer_ok=False))

    probe = github_api.probe_open_prs_rest("org", "widgets", author="alice")

    assert probe.observable is True
    assert [pr["number"] for pr in probe.prs] == [1]


def test_an_unobservable_probe_cannot_drive_pruning_or_scheduling() -> None:
    """The typed contract the orchestrator relies on."""
    probe = github_api.ConditionalPRListProbe(
        None, [], error="cannot resolve @me"
    )

    assert probe.observable is False
    assert probe.changed is False
    assert probe.not_modified is False
