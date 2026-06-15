"""Regression tests for surfacing real ``gh`` diagnostics (BOU-1638 / BOU-1694).

Before this change, ANY failure of ``list_open_prs`` (timeout, gh-not-on-PATH,
auth lapse, rate-limit, malformed JSON) collapsed to an opaque
"could not list PRs (gh unavailable)" — the completion path aborted with zero
actionable detail even when the exact ``gh pr list`` ran fine from the operator's
shell. These tests pin that:

* ``_run`` no longer blanks stderr on ``TimeoutExpired`` / ``OSError``.
* ``list_open_prs`` records structured diagnostics (and preserves the load-bearing
  ``None`` (=failure) vs ``[]`` (=no PRs) distinction so a transient outage never
  prunes tracked PRs).
* the operator-facing ``complete`` / ``check`` output now contains the underlying
  stderr + a remediation hint, NOT just "gh unavailable".
"""

from __future__ import annotations

import argparse
import subprocess

from agentic_pr_dash import github_api, maintenance_check


def _cp(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# --------------------------------------------------------------------------- #
# _run preserves the failure detail
# --------------------------------------------------------------------------- #

def test_run_captures_oserror_into_stderr(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "gh")

    monkeypatch.setattr(subprocess, "run", boom)
    r = github_api._run(["gh", "pr", "list"])
    assert r.returncode != 0
    # The exception text is preserved instead of a blank "" stderr.
    assert r.stderr
    assert "FileNotFoundError" in r.stderr
    assert "No such file or directory" in r.stderr


def test_run_captures_timeout_into_stderr(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=["gh", "pr", "list"], timeout=30)

    monkeypatch.setattr(subprocess, "run", boom)
    r = github_api._run(["gh", "pr", "list"], timeout_s=30)
    assert r.returncode != 0
    assert "timed out" in r.stderr.lower()
    assert "30s" in r.stderr


# --------------------------------------------------------------------------- #
# list_open_prs records diagnostics and preserves None-vs-[] invariant
# --------------------------------------------------------------------------- #

def test_list_open_prs_records_failure_diagnostics(monkeypatch):
    monkeypatch.setattr(
        github_api, "_run",
        lambda *a, **k: _cp(returncode=1, stderr="gh: command not found"),
    )
    assert github_api.list_open_prs(".") is None  # failure → None (NOT [])
    failure = github_api.last_list_open_prs_failure()
    assert failure is not None
    assert failure.returncode == 1
    assert "gh: command not found" in failure.stderr
    assert "gh" in failure.command_str and "pr" in failure.command_str
    # describe() carries stderr + a re-runnable self-check command.
    described = failure.describe()
    assert "gh: command not found" in described
    assert "remediation" in described
    assert "gh pr list" in described


def test_list_open_prs_clears_diagnostics_on_success(monkeypatch):
    # Prime a stale failure, then succeed — the stale diagnostic must not bleed.
    monkeypatch.setattr(
        github_api, "_run", lambda *a, **k: _cp(returncode=1, stderr="boom"))
    assert github_api.list_open_prs(".") is None
    assert github_api.last_list_open_prs_failure() is not None

    monkeypatch.setattr(github_api, "_run", lambda *a, **k: _cp(stdout="[]"))
    assert github_api.list_open_prs(".") == []  # genuinely no PRs → [] (NOT None)
    assert github_api.last_list_open_prs_failure() is None


def test_none_vs_empty_invariant_distinguishes_outage_from_no_prs(monkeypatch):
    # [] = genuinely no open PRs.
    monkeypatch.setattr(github_api, "_run", lambda *a, **k: _cp(stdout="[]"))
    assert github_api.list_open_prs(".") == []
    # None = the gh call failed; must never degrade to [] (which would prune
    # every tracked PR on a transient outage).
    monkeypatch.setattr(
        github_api, "_run", lambda *a, **k: _cp(returncode=1, stderr="rate limited"))
    assert github_api.list_open_prs(".") is None


# --------------------------------------------------------------------------- #
# complete / check surface the real diagnostics, not "gh unavailable" alone
# --------------------------------------------------------------------------- #

def _simulate_gh_connectivity_failure(monkeypatch):
    """Make every gh shell-out fail like the BOU-1638 subprocess (OSError path)."""
    def boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "gh")

    monkeypatch.setattr(subprocess, "run", boom)


def test_cmd_complete_surfaces_underlying_stderr(monkeypatch, capsys):
    _simulate_gh_connectivity_failure(monkeypatch)
    args = argparse.Namespace(cwd=".", pr="123", baseline="")
    code = maintenance_check._cmd_complete(args)
    out = capsys.readouterr().out

    assert code == 2
    # Still flags the gh-unavailable condition for back-compat scanners…
    assert "gh unavailable" in out
    # …but now ALSO carries the real evidence and a fix hint, not just the bare
    # opaque string.
    assert "FileNotFoundError" in out or "No such file or directory" in out
    assert "remediation" in out
    assert "gh pr list" in out
    # The opaque-only line must no longer be the entire message.
    assert out.strip() != "could not list PRs (gh unavailable)"


def test_check_worktree_surfaces_underlying_stderr(monkeypatch):
    _simulate_gh_connectivity_failure(monkeypatch)
    # Force a non-empty branch so resolution proceeds to list_open_prs.
    monkeypatch.setattr(maintenance_check, "_current_branch", lambda cwd: "feature/x")
    monkeypatch.setattr(maintenance_check, "_live_foreign_owner", lambda *a, **k: None)

    code, text = maintenance_check._check_worktree(".", "self-session")
    assert code == 2
    assert "gh unavailable" in text
    assert "FileNotFoundError" in text or "No such file or directory" in text
    assert "remediation" in text
    assert text.strip() != "could not list PRs (gh unavailable)"
