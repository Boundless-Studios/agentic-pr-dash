"""Regression coverage for BOU-2760 review-observation diagnostics."""

from __future__ import annotations

import json
import subprocess

import pytest
from agent_review_coordinator import (
    ReviewLedger,
    ReviewPolicy,
    ReviewResult,
    ReviewStage,
)

from agentic_pr_dash import github_api
from agentic_pr_dash._maintenance.review_settlement import (
    _observation_key,
    combine_clean_observations,
    evaluate_pr_snapshot,
)
from agentic_pr_dash.models import CICheck, PRData


REPOSITORY = "Boundless-Studios/agentic-pr-dash"
HEAD = "a" * 40


def _cp(
    stdout: str,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _policy() -> ReviewPolicy:
    return ReviewPolicy.model_validate(
        {
            "version": 1,
            "review": {
                "local": {"reviewer_count": 1},
                "backstop": {"reviewer_count": 1, "trigger": "new_head_sha"},
            },
        }
    )


def _ledger_without_backstop() -> ReviewLedger:
    ledger = ReviewLedger(
        repository=REPOSITORY,
        head_sha=HEAD,
        delivery_id="delivery-bou-2760",
        review_charter_version="review-charter-v1",
    )
    ledger.submit(
        ReviewResult(
            repository=REPOSITORY,
            head_sha=HEAD,
            stage=ReviewStage.LOCAL,
            round_number=1,
            slot_number=1,
            reviewer_execution_id="local-review",
        )
    )
    return ledger


def _pr() -> PRData:
    return PRData(
        number=24,
        repo=REPOSITORY,
        title="Backstop diagnostics",
        branch="bou-2760",
        url=f"https://github.com/{REPOSITORY}/pull/24",
        merge_state="CLEAN",
        mergeable="MERGEABLE",
        review_decision="APPROVED",
        latest_commit_sha=HEAD,
        ci_checks=[CICheck(name="tests", status="completed", conclusion="success")],
    )


def test_empty_review_observation_is_typed_as_observed(monkeypatch):
    monkeypatch.setattr(github_api, "get_repo_info", lambda cwd=None: ("o", "r"))
    monkeypatch.setattr(
        github_api,
        "_run",
        lambda *args, **kwargs: _cp(json.dumps([[]])),
    )

    result = github_api.get_review_submissions_observation(24, HEAD, ".")

    assert result.observable
    assert result.state.value == "observed"
    assert result.value == []


def test_review_observation_uses_explicit_repository(monkeypatch):
    monkeypatch.setattr(
        github_api,
        "get_repo_info",
        lambda cwd=None: pytest.fail("cwd repository must not be resolved"),
    )
    endpoints: list[str] = []

    def fake_run(cmd, *args, **kwargs):
        endpoints.extend(
            part for part in cmd if isinstance(part, str) and part.startswith("repos/")
        )
        return _cp(json.dumps([[]]))

    monkeypatch.setattr(github_api, "_run", fake_run)

    result = github_api.get_review_submissions_observation(
        24, HEAD, "/fork/worktree", repository="Upstream/Project"
    )

    assert result.observable
    assert endpoints == [
        "repos/Upstream/Project/pulls/24/reviews",
        "repos/Upstream/Project/issues/24/comments",
    ]


def test_auth_failure_is_unavailable_instead_of_missing_backstop(monkeypatch):
    monkeypatch.setattr(github_api, "get_repo_info", lambda cwd=None: ("o", "r"))
    monkeypatch.setattr(
        github_api,
        "_run",
        lambda *args, **kwargs: _cp("", 1, "HTTP 401: Bad credentials"),
    )

    result = github_api.get_review_submissions_observation(24, HEAD, ".")

    assert not result.observable
    assert result.state.value == "unavailable"
    assert "authentication" in (result.error or "").lower()


def test_rate_limit_retries_review_read_with_user_keyring(monkeypatch):
    monkeypatch.setattr(github_api, "get_repo_info", lambda cwd=None: ("o", "r"))
    calls: list[tuple[str, dict[str, str] | None]] = []

    def fake_run(cmd, *args, **kwargs):
        endpoint = next(
            part for part in cmd if isinstance(part, str) and part.startswith("repos/")
        )
        calls.append((endpoint, kwargs.get("env")))
        if endpoint.endswith("/pulls/24/reviews"):
            if kwargs.get("env", {}).get("GH_TOKEN") == "":
                return _cp(json.dumps([[]]))
            return _cp("", 1, "API rate limit exceeded")
        if endpoint.endswith("/issues/24/comments"):
            return _cp(json.dumps([[]]))
        raise AssertionError(endpoint)

    monkeypatch.setattr(github_api, "_run", fake_run)

    result = github_api.get_review_submissions_observation(24, HEAD, ".")

    assert result.observable
    assert result.state.value == "observed"
    assert any(env is not None and env.get("GH_TOKEN") == "" for _, env in calls)


def test_codex_capability_refusal_is_distinct_from_missing_review(monkeypatch):
    monkeypatch.setattr(github_api, "get_repo_info", lambda cwd=None: ("o", "r"))

    def fake_run(cmd, *args, **kwargs):
        endpoint = next(
            part for part in cmd if isinstance(part, str) and part.startswith("repos/")
        )
        if endpoint.endswith("/pulls/24/reviews"):
            return _cp(
                json.dumps(
                    [[
                        {
                            "id": 701,
                            "user": {"login": "review-bot"},
                            "state": "COMMENTED",
                            "commit_id": HEAD,
                            "submitted_at": "2026-08-24T00:00:00Z",
                            "body": "",
                        }
                    ]]
                )
            )
        if endpoint.endswith("/issues/24/comments"):
            return _cp(
                json.dumps(
                    [
                        [
                            {
                                "id": 901,
                                "user": {"login": "chatgpt-codex-connector[bot]"},
                                "created_at": "2026-08-24T00:00:00Z",
                                "body": (
                                    "You have reached your Codex usage limits "
                                    "for code reviews.\n\n"
                                    f"**Reviewed commit:** `{HEAD}`"
                                ),
                            }
                        ]
                    ]
                )
            )
        raise AssertionError(endpoint)

    monkeypatch.setattr(github_api, "_run", fake_run)

    result = github_api.get_review_submissions_observation(24, HEAD, ".")

    assert result.observable
    assert result.state.value == "capability_refused"
    assert "quota" in (result.error or "").lower()


def test_codex_capability_refusal_is_preserved_with_partial_submission(monkeypatch):
    monkeypatch.setattr(github_api, "get_repo_info", lambda cwd=None: ("o", "r"))

    def fake_run(cmd, *args, **kwargs):
        endpoint = next(
            part for part in cmd if isinstance(part, str) and part.startswith("repos/")
        )
        if endpoint.endswith("/pulls/24/reviews"):
            return _cp(
                json.dumps(
                    [[
                        {
                            "id": 701,
                            "user": {"login": "review-bot"},
                            "state": "COMMENTED",
                            "commit_id": HEAD,
                            "submitted_at": "2026-08-24T00:00:00Z",
                            "body": "",
                        }
                    ]]
                )
            )
        if endpoint.endswith("/issues/24/comments"):
            return _cp(
                json.dumps(
                    [[
                        {
                            "id": 901,
                            "user": {"login": "chatgpt-codex-connector[bot]"},
                            "created_at": "2026-08-24T00:01:00Z",
                            "body": (
                                "You have reached your Codex usage limits "
                                "for code reviews.\n\n"
                                f"**Reviewed commit:** `{HEAD}`"
                            ),
                        }
                    ]]
                )
            )
        raise AssertionError(endpoint)

    monkeypatch.setattr(github_api, "_run", fake_run)

    result = github_api.get_review_submissions_observation(24, HEAD, ".")

    assert result.state.value == "capability_refused"
    assert [review.review_id for review in result.value or []] == [701]


def test_abbreviated_codex_head_lookup_retries_with_user_keyring(monkeypatch):
    monkeypatch.setattr(github_api, "get_repo_info", lambda cwd=None: ("o", "r"))
    calls: list[tuple[str, dict[str, str] | None]] = []

    def fake_run(cmd, *args, **kwargs):
        endpoint = next(
            part for part in cmd if isinstance(part, str) and part.startswith("repos/")
        )
        calls.append((endpoint, kwargs.get("env")))
        if endpoint.endswith("/pulls/24/reviews"):
            return _cp(json.dumps([[]]))
        if endpoint.endswith("/issues/24/comments"):
            return _cp(
                json.dumps(
                    [[
                        {
                            "id": 901,
                            "user": {"login": "chatgpt-codex-connector[bot]"},
                            "created_at": "2026-08-24T00:00:00Z",
                            "body": (
                                "You have reached your Codex usage limits "
                                "for code reviews.\n\n"
                                f"**Reviewed commit:** `{HEAD[:10]}`"
                            ),
                        }
                    ]]
                )
            )
        if endpoint.endswith(f"/commits/{HEAD[:10]}"):
            if kwargs.get("env", {}).get("GH_TOKEN") == "":
                return _cp(json.dumps({"sha": HEAD}))
            return _cp("", 1, "API rate limit exceeded")
        raise AssertionError(endpoint)

    monkeypatch.setattr(github_api, "_run", fake_run)

    result = github_api.get_review_submissions_observation(24, HEAD, ".")

    assert result.state.value == "capability_refused"
    assert any(
        endpoint.endswith(f"/commits/{HEAD[:10]}")
        and env is not None
        and env.get("GH_TOKEN") == ""
        for endpoint, env in calls
    )


def test_abbreviated_codex_head_lookup_failure_is_unavailable(monkeypatch):
    monkeypatch.setattr(github_api, "get_repo_info", lambda cwd=None: ("o", "r"))

    def fake_run(cmd, *args, **kwargs):
        endpoint = next(
            part for part in cmd if isinstance(part, str) and part.startswith("repos/")
        )
        if endpoint.endswith("/pulls/24/reviews"):
            return _cp(
                json.dumps(
                    [[
                        {
                            "id": 701,
                            "user": {"login": "review-bot"},
                            "state": "COMMENTED",
                            "commit_id": HEAD,
                            "submitted_at": "2026-08-24T00:00:00Z",
                            "body": "",
                        }
                    ]]
                )
            )
        if endpoint.endswith("/issues/24/comments"):
            return _cp(
                json.dumps(
                    [[
                        {
                            "id": 901,
                            "user": {"login": "chatgpt-codex-connector[bot]"},
                            "created_at": "2026-08-24T00:00:00Z",
                            "body": (
                                "You have reached your Codex usage limits "
                                "for code reviews.\n\n"
                                f"**Reviewed commit:** `{HEAD[:10]}`"
                            ),
                        }
                    ]]
                )
            )
        if endpoint.endswith(f"/commits/{HEAD[:10]}"):
            return _cp("", 1, "HTTP 401: Bad credentials")
        raise AssertionError(endpoint)

    monkeypatch.setattr(github_api, "_run", fake_run)

    result = github_api.get_review_submissions_observation(24, HEAD, ".")

    assert result.state.value == "unavailable"
    assert not result.observable
    assert "commit" in (result.error or "").lower()
    assert [review.review_id for review in result.value or []] == [701]


def test_codex_capability_refusal_for_earlier_head_is_ignored(monkeypatch):
    monkeypatch.setattr(github_api, "get_repo_info", lambda cwd=None: ("o", "r"))

    def fake_run(cmd, *args, **kwargs):
        endpoint = next(
            part for part in cmd if isinstance(part, str) and part.startswith("repos/")
        )
        if endpoint.endswith("/pulls/24/reviews"):
            return _cp(json.dumps([[]]))
        if endpoint.endswith("/issues/24/comments"):
            return _cp(
                json.dumps(
                    [
                        [
                            {
                                "id": 901,
                                "user": {"login": "chatgpt-codex-connector[bot]"},
                                "created_at": "2026-08-23T00:00:00Z",
                                "body": (
                                    "You have reached your Codex usage limits for code reviews.\n\n"
                                    "**Reviewed commit:** `bbbbbbbbbb`"
                                ),
                            }
                        ]
                    ]
                )
            )
        raise AssertionError(endpoint)

    monkeypatch.setattr(github_api, "_run", fake_run)

    result = github_api.get_review_submissions_observation(24, HEAD, ".")

    assert result.state.value == "observed"
    assert result.value == []


def test_formal_review_with_unreadable_comments_remains_unavailable(monkeypatch):
    monkeypatch.setattr(github_api, "get_repo_info", lambda cwd=None: ("o", "r"))

    def fake_run(cmd, *args, **kwargs):
        endpoint = next(
            part for part in cmd if isinstance(part, str) and part.startswith("repos/")
        )
        if endpoint.endswith("/pulls/24/reviews"):
            return _cp(
                json.dumps(
                    [
                        [
                            {
                                "id": 701,
                                "user": {"login": "review-bot"},
                                "state": "COMMENTED",
                                "commit_id": HEAD,
                                "submitted_at": "2026-08-24T00:00:00Z",
                                "body": "",
                            }
                        ]
                    ]
                )
            )
        if endpoint.endswith("/issues/24/comments"):
            return _cp("", 1, "HTTP 401: Bad credentials")
        raise AssertionError(endpoint)

    monkeypatch.setattr(github_api, "_run", fake_run)

    result = github_api.get_review_submissions_observation(24, HEAD, ".")

    assert result.state.value == "unavailable"
    assert not result.observable
    assert [review.review_id for review in result.value or []] == [701]


@pytest.mark.parametrize("state", ["unavailable", "capability_refused"])
def test_finalization_consumes_typed_review_outcome_without_fake_backstop(
    state,
):
    outcome = (
        github_api.ObservationReadResult.unavailable("review API authentication failed")
        if state == "unavailable"
        else github_api.ObservationReadResult.capability_refused(
            "reviewer quota refusal"
        )
    )

    observation = evaluate_pr_snapshot(
        pr=_pr(),
        policy=_policy(),
        ledger=_ledger_without_backstop(),
        threads=[],
        deferrals={},
        review_observation=outcome,
    )

    assert "backstop:1" not in observation.review.missing_slots
    if state == "unavailable":
        assert not observation.clean
        assert "review_observation_unavailable" in observation.blockers
    else:
        assert observation.clean
        assert observation.review.settled
        assert "capability" in " ".join(observation.diagnostics).lower()


def test_settlement_loop_key_ignores_rendered_diagnostic_wording():
    first = evaluate_pr_snapshot(
        pr=_pr(),
        policy=_policy(),
        ledger=_ledger_without_backstop(),
        threads=[],
        deferrals={},
        review_observation=github_api.ObservationReadResult.capability_refused(
            "first provider wording"
        ),
    )
    second = first.model_copy(update={"diagnostics": ["different provider wording"]})

    assert _observation_key(first) == _observation_key(second)
    assert combine_clean_observations(first, second).settled
