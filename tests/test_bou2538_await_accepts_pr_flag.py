"""BOU-2538: `await` must not reject `--pr` with a bare argparse error.

`arm` REQUIRES `--pr N`; `await` resolves owned PRs from the state dir and
takes no `--pr` at all. The natural sequence an agent writes after arming —
passing the same `--pr` value to `await` — used to fail with
"unrecognized arguments: --pr 2857", with no guidance on what to pass instead.
`await` should accept (and ignore, since it resolves its own scope) `--pr`
rather than erroring on the asymmetry.
"""
from __future__ import annotations

import pytest

from agentic_pr_dash import config
from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    monkeypatch.setenv("GAIA_PR_WATCH_STOP_INTERVAL", "0")
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def test_await_accepts_a_pr_flag_without_arg_parse_error(tmp_path, monkeypatch):
    """`await --pr N ...` must not raise argparse's SystemExit(2)."""
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(_worktrees_mod, "_collect_stop_gate_worktrees", lambda sid, cwd: [])

    rc = mc.main([
        "await",
        "--cwd", str(tmp_path),
        "--session-id", "sess-2538",
        "--pr", "2857",
        "--owner-pid", "99999",
        "--max-wait", "1",
    ])
    # The owner pid is dead, so this should reach the ordinary "exit 0"
    # path — the point is that argparse did not reject --pr outright.
    assert rc == 0
