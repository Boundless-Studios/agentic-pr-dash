"""Typed completion-state contract with repository-policy callback seams."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from typing import Any, Final, TypeAlias


SCHEMA_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class CompletionStateRequest:
    """Stable context supplied to generic probes and repository policy."""

    branch: str
    head_oid: str
    session_id: str = ""


@dataclass(frozen=True, slots=True)
class CompletionBlocker:
    """One actionable reason completion cannot yet be claimed."""

    check_id: str
    message: str
    remediation: str


@dataclass(frozen=True, slots=True)
class CompletionStateResult:
    """Versioned, serializable completion evaluation result."""

    branch: str
    head_oid: str
    blockers: tuple[CompletionBlocker, ...] = ()
    advisories: tuple[str, ...] = ()
    observable: bool = True
    schema_version: int = SCHEMA_VERSION

    @property
    def complete(self) -> bool:
        return self.observable and not self.blockers

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, payload: str) -> CompletionStateResult:
        raw: dict[str, Any] = json.loads(payload)
        version = int(raw.get("schema_version", 0))
        if version != SCHEMA_VERSION:
            raise ValueError(f"unsupported completion-state schema: {version}")
        return cls(
            branch=str(raw["branch"]),
            head_oid=str(raw["head_oid"]),
            blockers=tuple(CompletionBlocker(**item) for item in raw["blockers"]),
            advisories=tuple(str(item) for item in raw["advisories"]),
            observable=bool(raw["observable"]),
            schema_version=version,
        )


ProbeValue: TypeAlias = CompletionBlocker | Iterable[CompletionBlocker] | Exception | None
CompletionProbe: TypeAlias = Callable[[CompletionStateRequest], ProbeValue]
CommandRunner: TypeAlias = Callable[[tuple[str, ...]], str]
PRLookup: TypeAlias = Callable[[str], dict[str, Any] | None]
CompletionPolicy: TypeAlias = Callable[
    [CompletionStateRequest, dict[str, Any]], Iterable[CompletionBlocker]
]


def evaluate_completion_state(
    request: CompletionStateRequest,
    *,
    probes: Iterable[CompletionProbe] = (),
    policy_callbacks: Iterable[CompletionProbe] = (),
) -> CompletionStateResult:
    """Evaluate generic probes, then repository-specific policy callbacks.

    An unavailable observation is advisory but marks the result non-observable,
    preventing callers from confusing an outage with a verified clean state.
    """
    blockers: list[CompletionBlocker] = []
    advisories: list[str] = []
    observable = True
    for probe in (*tuple(probes), *tuple(policy_callbacks)):
        try:
            value = probe(request)
        except Exception as exc:  # noqa: BLE001 - observation boundary
            value = exc
        if value is None:
            continue
        if isinstance(value, Exception):
            advisories.append(str(value))
            observable = False
        elif isinstance(value, CompletionBlocker):
            blockers.append(value)
        else:
            blockers.extend(value)
    return CompletionStateResult(
        branch=request.branch,
        head_oid=request.head_oid,
        blockers=tuple(blockers),
        advisories=tuple(advisories),
        observable=observable,
    )


def collect_completion_state(
    request: CompletionStateRequest,
    *,
    command_runner: CommandRunner,
    pr_lookup: PRLookup,
    policy_callbacks: Iterable[CompletionPolicy] = (),
) -> CompletionStateResult:
    """Collect reusable git/PR facts and apply repository-owned PR policy."""
    blockers: list[CompletionBlocker] = []
    advisories: list[str] = []
    observable = True
    try:
        if command_runner(("git", "status", "--porcelain")).strip():
            blockers.append(
                CompletionBlocker(
                    "uncommitted-files",
                    "Working tree has uncommitted changes",
                    "Commit the intended changes before completion",
                )
            )
        ahead = command_runner(
            ("git", "rev-list", "--count", "@{u}..HEAD")
        ).strip()
        if ahead and int(ahead) > 0:
            blockers.append(
                CompletionBlocker(
                    "unpushed-commits",
                    f"Branch has {ahead} unpushed commit(s)",
                    "Push the branch before completion",
                )
            )
    except (OSError, RuntimeError, ValueError) as exc:
        advisories.append(f"Git state unavailable: {exc}")
        observable = False

    try:
        pr = pr_lookup(request.branch)
        pr_lookup_observable = True
    except Exception as exc:  # noqa: BLE001 - GitHub observation boundary
        advisories.append(f"PR lookup unavailable: {exc}")
        pr = None
        pr_lookup_observable = False
        observable = False
    if pr is None and pr_lookup_observable:
        advisories.append("No open PR found")
    else:
        for callback in policy_callbacks:
            try:
                blockers.extend(callback(request, pr))
            except Exception as exc:  # noqa: BLE001 - repository policy seam
                advisories.append(f"Completion policy unavailable: {exc}")
                observable = False

    return CompletionStateResult(
        branch=request.branch,
        head_oid=request.head_oid,
        blockers=tuple(blockers),
        advisories=tuple(advisories),
        observable=observable,
    )
