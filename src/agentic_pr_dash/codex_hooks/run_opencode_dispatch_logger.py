"""opencode dispatch-observation hook entrypoint."""

from __future__ import annotations

import sys

from agentic_pr_dash.codex_hooks.dispatch_runner import (
    RepositoryCallback,
    run_provider_entrypoint,
)
from agentic_pr_dash.dispatch_observation import DispatchProvider


def main(
    argv: list[str] | None = None,
    callback: RepositoryCallback | None = None,
) -> int:
    return run_provider_entrypoint(
        DispatchProvider.OPENCODE,
        sys.argv[1:] if argv is None else argv,
        callback,
    )


if __name__ == "__main__":
    raise SystemExit(main())
