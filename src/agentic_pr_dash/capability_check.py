"""Generic installed-package capability reconciliation primitives."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    """An importable module and callable attributes required by an adapter."""

    module: str
    callables: tuple[str, ...] = ()


def missing_capabilities(
    requirements: Iterable[CapabilityRequirement],
) -> tuple[str, ...]:
    """Return stable diagnostics for unavailable required capabilities."""
    missing: list[str] = []
    for requirement in requirements:
        try:
            module = importlib.import_module(requirement.module)
        except (ImportError, AttributeError):
            missing.append(f"{requirement.module} (module unavailable)")
            continue
        for attribute in requirement.callables:
            if not callable(getattr(module, attribute, None)):
                missing.append(
                    f"{requirement.module}.{attribute} (not callable)"
                )
    return tuple(missing)


def require_capabilities(
    requirements: Iterable[CapabilityRequirement],
) -> None:
    """Raise a single actionable error when an installed build is stale."""
    missing = missing_capabilities(requirements)
    if missing:
        raise RuntimeError("missing required capabilities: " + ", ".join(missing))
