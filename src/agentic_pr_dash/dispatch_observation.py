"""Provider-neutral records for observed agent dispatches."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class DispatchProvider(str, Enum):
    """Agent providers whose dispatches can be observed."""

    CODEX = "codex"
    OPENCODE = "opencode"


class DispatchSource(str, Enum):
    """Execution surfaces that report a dispatch."""

    INTERACTIVE_HOOK = "interactive_hook"
    DETACHED_RUNNER = "detached_runner"


class DispatchOutcome(str, Enum):
    """Portable terminal outcome of a dispatch attempt."""

    SUCCESS = "success"
    FAILURE = "failure"
    UNAVAILABLE = "unavailable"


_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "provider",
        "source",
        "session_id",
        "worktree_root",
        "command",
        "task_type",
        "requested_model",
        "resolved_model",
        "outcome",
        "review_verdict",
    }
)


@dataclass(frozen=True, slots=True)
class DispatchObservation:
    """One normalized dispatch observation suitable for durable persistence."""

    provider: DispatchProvider
    source: DispatchSource
    session_id: str
    worktree_root: str
    command: str
    task_type: str
    requested_model: str | None
    resolved_model: str | None
    outcome: DispatchOutcome
    review_verdict: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.review_verdict is not None and not (
            self.outcome is DispatchOutcome.SUCCESS and self.task_type == "review"
        ):
            raise ValueError(
                "review_verdict is valid only for a completed successful review"
            )

    def to_dict(self) -> dict[str, object]:
        """Serialize using stable string enum values."""

        return {
            "provider": self.provider.value,
            "source": self.source.value,
            "session_id": self.session_id,
            "worktree_root": self.worktree_root,
            "command": self.command,
            "task_type": self.task_type,
            "requested_model": self.requested_model,
            "resolved_model": self.resolved_model,
            "outcome": self.outcome.value,
            "review_verdict": (
                dict(self.review_verdict) if self.review_verdict is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> DispatchObservation:
        """Deserialize a portable record, rejecting provider-specific fields."""

        unknown_fields = set(payload) - _FIELDS
        if unknown_fields:
            fields = ", ".join(sorted(unknown_fields))
            raise ValueError(f"unknown dispatch observation fields: {fields}")

        verdict = payload.get("review_verdict")
        if verdict is not None and not isinstance(verdict, dict):
            raise TypeError("review_verdict must be an object or null")

        return cls(
            provider=DispatchProvider(str(payload["provider"])),
            source=DispatchSource(str(payload["source"])),
            session_id=str(payload["session_id"]),
            worktree_root=str(payload["worktree_root"]),
            command=str(payload["command"]),
            task_type=str(payload["task_type"]),
            requested_model=_optional_string(payload.get("requested_model")),
            resolved_model=_optional_string(payload.get("resolved_model")),
            outcome=DispatchOutcome(str(payload["outcome"])),
            review_verdict=dict(verdict) if verdict is not None else None,
        )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("model fields must be strings or null")
    return value
