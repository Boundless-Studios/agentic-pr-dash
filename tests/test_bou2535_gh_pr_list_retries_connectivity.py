"""BOU-2535: `_gh_pr_list_json` (the loop's `check`/`arm` path — NOT the
dashboard's `list_open_prs`, which already retries via `github_api._run`)
must retry a transient connection failure instead of giving up on the first
attempt.

Observed: the `pr-maintenance-loop` daemon intermittently failed EVERY `gh`
call in a cycle with `error connecting to api.github.com`, with a healthy
token and healthy quota — a transient connection blip, not rate-limiting or
an auth problem, and there was no retry. `github_api._run` already implements
exactly this bounded connectivity-retry for `list_open_prs`; `_gh_pr_list_json`
(used by `_list_my_open_prs` / `_resolve_open_pr_for_branch`, which back the
loop's `check` subprocess) bypassed it with a raw `subprocess.run` call.
"""
from __future__ import annotations

import pytest

from agentic_pr_dash._maintenance import pr_state


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Keep retry backoff instant."""
    from agentic_pr_dash import github_api
    monkeypatch.setattr(github_api, "_GH_RETRY_BASE_DELAY_S", 0.0)


def test_transient_connection_failure_is_retried_then_succeeds(monkeypatch):
    calls = []

    # Keep this retry contract focused on the list subprocess.  ``@me`` is
    # intentionally resolved by a separate ``gh api user`` call in the
    # production path; an explicit author makes that seam deterministic and
    # prevents the viewer lookup from consuming the retry fixture's sequence.
    from agentic_pr_dash import config as config_mod
    import types

    fake_load = lambda cwd=None: types.SimpleNamespace(pr_author="viewer")  # noqa: E731
    fake_load.cache_clear = lambda: None
    monkeypatch.setattr(config_mod, "load", fake_load)

    def fake_run(cmd, *args, **kwargs):
        calls.append(1)
        if len(calls) < 2:
            return _Result(
                returncode=1,
                stderr="error connecting to api.github.com\ncheck your internet "
                "connection or https://githubstatus.com",
            )
        return _Result(
            returncode=0,
            stdout='[{"number": 42, "isDraft": false, "author": {"login": "viewer"}}]',
        )

    import subprocess as _subprocess
    monkeypatch.setattr(_subprocess, "run", fake_run)

    data = pr_state._gh_pr_list_json("/repo", [], "number,isDraft")
    assert data == [
        {"number": 42, "isDraft": False, "author": {"login": "viewer"}}
    ], (
        "a transient connection failure must be retried rather than giving "
        f"up on the first attempt (BOU-2535). calls={len(calls)}"
    )
    assert len(calls) >= 2, "expected at least one retry after the connection failure"
