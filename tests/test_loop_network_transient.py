"""Network interrupts must read as INCONCLUSIVE ticks, not executor failures (BOU-2417).

`loop._service_cwd` used to treat every non-serviced dispatch as one
undifferentiated executor failure: it burned the per-PR escalation streak AND
voluntarily released the coordinator claim. A transient network outage was
therefore indistinguishable from a genuinely broken executor — three outage
ticks escalated a PR that was never broken, and ownership churned
(release -> re-acquire) on every tick (the BOU-2412 churn signature).

The classifier is a direct reachability probe rather than error-text matching:
`_run_executor` runs the executor WITHOUT capture (the daemon log streams live
codex/claude output), so `_try_run` only ever yields ``f"exit {rc}"`` — there is
no stderr for `_is_transient_connectivity_failure` to match on.
"""
from __future__ import annotations

import json
import socket
import types

import pytest

from agentic_pr_dash import config, github_api, loop


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    """Point the daemon dir at tmp_path so health/escalation files are isolated."""
    monkeypatch.setenv("GAIA_DAEMON_DIR", str(tmp_path / "daemons"))
    monkeypatch.setenv("AGENTIC_PR_DASH_DAEMON_DIR", str(tmp_path / "daemons"))
    config.load.cache_clear()
    yield
    config.load.cache_clear()


# --------------------------------------------------------------------------
# _network_reachable
# --------------------------------------------------------------------------

def test_network_reachable_true_when_a_host_connects(monkeypatch):
    """Any single probe host accepting a TCP connect means the network is up."""
    monkeypatch.setenv("APD_LOOP_PROBE_HOSTS", "unreachable.invalid:443,api.github.com:443")

    attempted: list[tuple[str, int]] = []

    class _Sock:
        def close(self):
            pass

    def _connect(addr, timeout=None):
        attempted.append(addr)
        if addr[0] == "api.github.com":
            return _Sock()
        raise OSError("no route to host")

    monkeypatch.setattr(socket, "create_connection", _connect)
    assert loop._network_reachable() is True
    assert ("api.github.com", 443) in attempted


def test_network_reachable_false_when_no_host_connects(monkeypatch):
    """Every configured host failing to connect means the network is down."""
    monkeypatch.setenv("APD_LOOP_PROBE_HOSTS", "a.invalid:443,b.invalid:443")
    monkeypatch.setattr(
        socket, "create_connection",
        lambda addr, timeout=None: (_ for _ in ()).throw(OSError("unreachable")),
    )
    assert loop._network_reachable() is False


def test_network_reachable_fails_open_on_unexpected_error(monkeypatch):
    """A broken probe must degrade to today's behavior, never suppress a real failure."""
    monkeypatch.setenv("APD_LOOP_PROBE_HOSTS", "garbage-without-a-port")
    monkeypatch.setattr(
        socket, "create_connection",
        lambda addr, timeout=None: (_ for _ in ()).throw(RuntimeError("probe bug")),
    )
    assert loop._network_reachable() is True


# --------------------------------------------------------------------------
# Inconclusive-tick behavior in _service_cwd
# --------------------------------------------------------------------------

_CHECK_STDOUT = (
    "fix prompt\n"
    "PR_NUMBER=4242\n"
    "COORDINATOR_CLAIM_ID=claim-abc\n"
    "COORDINATOR_LEASE_EPOCH=1\n"
)


def _wire_loop(monkeypatch, wt, *, reachable: bool, released: list, exit_code: int = 1):
    """Wire a tick that finds work for PR #4242 and whose executor exits non-zero.

    NOTE the executor EXITS non-zero rather than failing to spawn: that is what a
    network outage actually looks like (the local binary launches fine, then its
    model API is unreachable). A spawn failure is a different, genuinely-local
    defect.
    """
    monkeypatch.setattr(loop, "_discover_cwds", lambda args: [str(wt)])
    monkeypatch.setattr(loop, "_cleanup_stale_no_pr_worktree", lambda cwd, session_id="": False)
    monkeypatch.setattr(loop, "_baseline_sha", lambda cwd, pr: "sha")
    monkeypatch.setattr(loop, "_resolve_repo_slug", lambda cwd: "owner/repo")
    monkeypatch.setattr(loop, "_append_resolved_direction", lambda prompt, *a, **k: prompt)
    monkeypatch.setattr(loop, "_decision_requested_during_dispatch", lambda *a, **k: False)
    monkeypatch.setattr(
        loop.subprocess, "run",
        lambda cmd, *a, **k: types.SimpleNamespace(
            returncode=loop.CHECK_WORK_FOUND, stdout=_CHECK_STDOUT, stderr=""),
    )
    monkeypatch.setattr(loop, "_run_executor", lambda executor, prompt, cwd: exit_code)
    monkeypatch.setattr(loop.coordinator, "heartbeat_claim", lambda *a, **k: None)
    monkeypatch.setattr(
        loop.coordinator, "release_claim",
        lambda handle, session, reason: released.append((handle.claim_id, session, reason)),
    )
    monkeypatch.setattr(loop, "_network_reachable", lambda: reachable)


def _args(tmp_path):
    return types.SimpleNamespace(
        no_discover_worktrees=False, session_id="sess-1", cwd=[str(tmp_path)],
        fallback_executor="", once=True,
    )


def test_outage_ticks_do_not_burn_the_streak(monkeypatch, tmp_path):
    """N ticks past the escalation threshold with the network down leave the streak at 0."""
    monkeypatch.setenv("AGENTIC_PR_DASH_ESCALATION_THRESHOLD", "2")
    config.load.cache_clear()
    wt = tmp_path / "wt"
    wt.mkdir()
    released: list = []
    _wire_loop(monkeypatch, wt, reachable=False, released=released)

    for _ in range(3):  # 3 > threshold of 2
        loop._tick(_args(tmp_path), "codex {prompt}")

    assert loop.executor_failure_streak(str(wt), 4242) == 0, (
        "an unreachable network must not burn the per-PR escalation streak"
    )


def test_outage_ticks_write_no_escalation_marker(monkeypatch, tmp_path):
    """No escalation marker is filed for a PR that only ever hit network outages."""
    monkeypatch.setenv("AGENTIC_PR_DASH_ESCALATION_THRESHOLD", "2")
    config.load.cache_clear()
    wt = tmp_path / "wt"
    wt.mkdir()
    released: list = []
    _wire_loop(monkeypatch, wt, reachable=False, released=released)

    for _ in range(3):
        loop._tick(_args(tmp_path), "codex {prompt}")

    marker = loop._escalated_marker_path(str(wt))
    if marker.exists():
        existing = json.loads(marker.read_text(encoding="utf-8"))
        assert "4242" not in existing, "network outages must not escalate a PR"


def test_outage_tick_holds_the_coordinator_claim(monkeypatch, tmp_path):
    """The claim is HELD across an inconclusive tick (BOU-2389: fail closed on the claim)."""
    monkeypatch.setenv("AGENTIC_PR_DASH_ESCALATION_THRESHOLD", "2")
    config.load.cache_clear()
    wt = tmp_path / "wt"
    wt.mkdir()
    released: list = []
    _wire_loop(monkeypatch, wt, reachable=False, released=released)

    for _ in range(3):
        loop._tick(_args(tmp_path), "codex {prompt}")

    assert released == [], (
        "ownership must lapse via the coordinator lease if the process is dead, "
        f"not be handed back on a network blip; got releases: {released}"
    )


def test_outage_does_not_flip_executors_viable(monkeypatch, tmp_path):
    """Regression: an unreachable-network dispatch failure never downgrades repo viability.

    The viability branch requires EVERY error to carry `_SPAWN_FAILED_PREFIX`,
    which is set only when `subprocess.run` itself raises (binary missing). A
    network outage lets the binary spawn and exit non-zero, so viability must
    stay true. This pins behavior the ticket assumed was broken.
    """
    monkeypatch.setenv("AGENTIC_PR_DASH_ESCALATION_THRESHOLD", "2")
    config.load.cache_clear()
    wt = tmp_path / "wt"
    wt.mkdir()
    released: list = []
    _wire_loop(monkeypatch, wt, reachable=False, released=released)

    viability: list[bool] = []
    real_record = loop.record_loop_health
    monkeypatch.setattr(
        loop, "record_loop_health",
        lambda cwd, **kw: (viability.append(kw.get("executors_viable")),
                           real_record(cwd, **kw))[1],
    )

    for _ in range(3):
        loop._tick(_args(tmp_path), "codex {prompt}")

    assert viability, "the tick should have stamped at least one health record"
    assert all(v is True for v in viability), (
        f"executors_viable must not be flipped false by a network outage; got {viability}"
    )


def test_genuine_executor_failure_still_escalates(monkeypatch, tmp_path):
    """A real (non-network) executor defect increments the streak and escalates as today."""
    monkeypatch.setenv("AGENTIC_PR_DASH_ESCALATION_THRESHOLD", "2")
    config.load.cache_clear()
    wt = tmp_path / "wt"
    wt.mkdir()
    released: list = []
    _wire_loop(monkeypatch, wt, reachable=True, released=released)
    monkeypatch.setattr("agentic_pr_dash.iterm.notify", lambda *a, **k: None)

    for _ in range(2):
        loop._tick(_args(tmp_path), "codex {prompt}")

    assert loop.executor_failure_streak(str(wt), 4242) == 2, (
        "a reachable network means a genuine executor failure — streak must still burn"
    )
    marker = loop._escalated_marker_path(str(wt))
    assert marker.exists(), "reaching the threshold must file an escalation marker"
    assert "4242" in json.loads(marker.read_text(encoding="utf-8"))
    assert released, "a genuine failure must still release the claim"


# --------------------------------------------------------------------------
# get_new_pr_commits: None (unknown) vs [] (genuinely empty)
# --------------------------------------------------------------------------

def test_get_new_pr_commits_returns_none_on_gh_failure(monkeypatch, tmp_path):
    """A failed gh call is UNKNOWN, not "no new commits" (matches list_open_prs)."""
    monkeypatch.setattr(github_api, "_local_new_commits", lambda *a, **k: [])
    monkeypatch.setattr(
        github_api, "_run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="could not resolve host"),
    )
    result = github_api.get_new_pr_commits(7, "base-sha", "", str(tmp_path))
    assert result is None, (
        "returning [] lets an outage masquerade as an empty commit range (BOU-2200)"
    )


def test_get_new_pr_commits_returns_empty_list_on_genuinely_empty_range(monkeypatch, tmp_path):
    """A successful gh call with no commits past the baseline is still []."""
    monkeypatch.setattr(github_api, "_local_new_commits", lambda *a, **k: [])
    monkeypatch.setattr(
        github_api, "_run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="[]", stderr=""),
    )
    result = github_api.get_new_pr_commits(7, "", "", str(tmp_path))
    assert result == [], "an empty range must stay distinguishable from an outage"


def test_get_new_pr_commits_returns_none_on_malformed_payload(monkeypatch, tmp_path):
    """A malformed payload is not evidence of an empty range either."""
    monkeypatch.setattr(github_api, "_local_new_commits", lambda *a, **k: [])
    monkeypatch.setattr(
        github_api, "_run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="not json", stderr=""),
    )
    assert github_api.get_new_pr_commits(7, "", "", str(tmp_path)) is None
