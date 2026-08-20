from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agentic_pr_dash.dispatch_observation import (
    DispatchObservation,
    DispatchOutcome,
    DispatchProvider,
    DispatchSource,
)


def test_dispatch_observation_round_trips() -> None:
    observation = DispatchObservation(
        provider=DispatchProvider.CODEX,
        source=DispatchSource.INTERACTIVE_HOOK,
        session_id="session-1",
        worktree_root="/repo/wt",
        command="codex exec review",
        task_type="review",
        requested_model="gpt-5.6-sol",
        resolved_model="gpt-5.6-sol",
        outcome=DispatchOutcome.SUCCESS,
        review_verdict={"status": "clean", "findings": []},
    )

    assert DispatchObservation.from_dict(observation.to_dict()) == observation


def test_dispatch_observation_records_a_portable_timestamp() -> None:
    observation = DispatchObservation(
        provider=DispatchProvider.CODEX,
        source=DispatchSource.INTERACTIVE_HOOK,
        session_id="session-1",
        worktree_root="/repo/wt",
        command="codex exec review",
        task_type="review",
        requested_model=None,
        resolved_model="gpt-5.6-sol",
        outcome=DispatchOutcome.SUCCESS,
    )

    observed_at = observation.to_dict()["observed_at"]
    assert isinstance(observed_at, str)
    assert datetime.fromisoformat(observed_at).tzinfo is not None


def test_dispatch_observation_preserves_legacy_positional_arguments() -> None:
    verdict = {"status": "clean", "findings": []}

    observation = DispatchObservation(
        DispatchProvider.CODEX,
        DispatchSource.INTERACTIVE_HOOK,
        "session-1",
        "/repo/wt",
        "codex exec review",
        "review",
        "gpt-5.6-sol",
        "gpt-5.6-sol",
        DispatchOutcome.SUCCESS,
        verdict,
    )

    assert observation.review_verdict == verdict
    assert datetime.fromisoformat(observation.observed_at).tzinfo is UTC


def test_legacy_dispatch_observation_uses_epoch_timestamp() -> None:
    payload = DispatchObservation(
        provider=DispatchProvider.CODEX,
        source=DispatchSource.INTERACTIVE_HOOK,
        session_id="session-1",
        worktree_root="/repo/wt",
        command="codex exec implement",
        task_type="implementation",
        requested_model=None,
        resolved_model="gpt-5.6-sol",
        outcome=DispatchOutcome.SUCCESS,
    ).to_dict()
    del payload["observed_at"]

    observation = DispatchObservation.from_dict(payload)

    assert observation.observed_at == "1970-01-01T00:00:00+00:00"


def test_deserialized_timestamp_is_normalized_to_utc() -> None:
    payload = DispatchObservation(
        provider=DispatchProvider.CODEX,
        source=DispatchSource.INTERACTIVE_HOOK,
        session_id="session-1",
        worktree_root="/repo/wt",
        command="codex exec implement",
        task_type="implementation",
        requested_model=None,
        resolved_model="gpt-5.6-sol",
        outcome=DispatchOutcome.SUCCESS,
    ).to_dict()
    payload["observed_at"] = "2026-08-20T08:00:00-07:00"

    observation = DispatchObservation.from_dict(payload)

    assert observation.observed_at == "2026-08-20T15:00:00+00:00"


@pytest.mark.parametrize(
    "observed_at", [None, 123, "not-a-timestamp", "2026-08-20T08:00:00"]
)
def test_deserialized_timestamp_rejects_invalid_values(observed_at: object) -> None:
    payload = DispatchObservation(
        provider=DispatchProvider.CODEX,
        source=DispatchSource.INTERACTIVE_HOOK,
        session_id="session-1",
        worktree_root="/repo/wt",
        command="codex exec implement",
        task_type="implementation",
        requested_model=None,
        resolved_model="gpt-5.6-sol",
        outcome=DispatchOutcome.SUCCESS,
    ).to_dict()
    payload["observed_at"] = observed_at

    with pytest.raises((TypeError, ValueError), match="observed_at"):
        DispatchObservation.from_dict(payload)


@pytest.mark.parametrize("provider", list(DispatchProvider))
@pytest.mark.parametrize("source", list(DispatchSource))
@pytest.mark.parametrize("outcome", list(DispatchOutcome))
def test_every_enum_value_serializes(
    provider: DispatchProvider,
    source: DispatchSource,
    outcome: DispatchOutcome,
) -> None:
    observation = DispatchObservation(
        provider=provider,
        source=source,
        session_id="session-1",
        worktree_root="/repo/wt",
        command="provider command",
        task_type="implementation",
        requested_model=None,
        resolved_model=None,
        outcome=outcome,
    )

    payload = observation.to_dict()

    assert payload["provider"] == provider.value
    assert payload["source"] == source.value
    assert payload["outcome"] == outcome.value
    assert DispatchObservation.from_dict(payload) == observation


def test_failed_dispatch_cannot_carry_review_verdict() -> None:
    with pytest.raises(ValueError, match="completed successful review"):
        DispatchObservation(
            provider=DispatchProvider.OPENCODE,
            source=DispatchSource.INTERACTIVE_HOOK,
            session_id="s",
            worktree_root="/repo",
            command="opencode run review",
            task_type="review",
            requested_model=None,
            resolved_model="kimi",
            outcome=DispatchOutcome.FAILURE,
            review_verdict={"status": "clean"},
        )


def test_non_review_dispatch_cannot_carry_review_verdict() -> None:
    with pytest.raises(ValueError, match="completed successful review"):
        DispatchObservation(
            provider=DispatchProvider.CODEX,
            source=DispatchSource.INTERACTIVE_HOOK,
            session_id="s",
            worktree_root="/repo",
            command="codex exec implement",
            task_type="implementation",
            requested_model=None,
            resolved_model="gpt-5.6-sol",
            outcome=DispatchOutcome.SUCCESS,
            review_verdict={"status": "clean"},
        )


def test_provider_specific_fields_are_rejected() -> None:
    payload = {
        "provider": "codex",
        "source": "interactive_hook",
        "session_id": "s",
        "worktree_root": "/repo",
        "command": "codex exec review",
        "task_type": "review",
        "requested_model": None,
        "resolved_model": "gpt-5.6-sol",
        "outcome": "success",
        "review_verdict": None,
        "codex_event_type": "item.completed",
    }

    with pytest.raises(ValueError, match="unknown dispatch observation fields"):
        DispatchObservation.from_dict(payload)
