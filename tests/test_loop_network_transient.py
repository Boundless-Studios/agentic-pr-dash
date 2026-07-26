"""Network interrupts must read as INCONCLUSIVE ticks, not executor failures (BOU-2417).

`loop._service_cwd` used to treat every non-serviced dispatch as one
undifferentiated executor failure: it burned the per-PR escalation streak AND
voluntarily released the coordinator claim. A transient network outage was
therefore indistinguishable from a genuinely broken executor — three outage
ticks escalated a PR that was never broken, and ownership churned
(release -> re-acquire) on every tick (the BOU-2412 churn signature).

Classification is a reachability probe, NOT the executor's output. Reading the
executor's output was tried in PR #113 review round 1 and reverted in round 2:
the text is an entire coding-agent run, so a genuine task failure that merely
prints a transport-shaped string would be misread as an outage and silently
never escalate — and teeing it risked hanging the loop on a pipe held open by a
descendant of the executor.
"""
from __future__ import annotations

import json
import time
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
# _network_reachable — bounded, killable, proxy-aware
# --------------------------------------------------------------------------

def test_network_reachable_true_when_a_url_answers(monkeypatch):
    monkeypatch.setenv("APD_LOOP_PROBE_URLS", "https://api.github.com")
    monkeypatch.setattr(
        loop.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
    )
    assert loop._network_reachable() is True


def test_network_reachable_false_when_no_url_answers(monkeypatch):
    monkeypatch.setenv("APD_LOOP_PROBE_URLS", "https://a.invalid,https://b.invalid")
    monkeypatch.setattr(
        loop.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=b"", stderr=b""),
    )
    assert loop._network_reachable() is False


def test_network_reachable_fails_open_on_unexpected_error(monkeypatch):
    """A broken probe must degrade to today's behavior, never suppress a failure."""
    monkeypatch.setattr(
        loop.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("probe bug")),
    )
    assert loop._network_reachable() is True


def test_probe_timeout_fails_open_and_leaks_nothing(monkeypatch):
    """PR #113 review: a wedged resolver must neither block nor leak a thread.

    The probe is a killable subprocess, so `run` kills it on timeout. We cannot
    prove the network is down from a timeout, so it fails OPEN — suppressing
    escalation forever is worse than counting one extra failure.
    """
    monkeypatch.setenv("APD_LOOP_PROBE_URLS", "https://stalled.invalid")

    def _timeout(*a, **k):
        raise loop.subprocess.TimeoutExpired(cmd="probe", timeout=k.get("timeout", 1))

    monkeypatch.setattr(loop.subprocess, "run", _timeout)
    assert loop._network_reachable() is True


def test_probe_is_actually_bounded_end_to_end(monkeypatch):
    """The real subprocess path returns quickly against an unroutable target."""
    monkeypatch.setenv("APD_LOOP_PROBE_URLS", "https://192.0.2.1")  # TEST-NET-1
    monkeypatch.setenv("APD_LOOP_PROBE_TIMEOUT_S", "1")
    started = time.monotonic()
    loop._network_reachable()
    assert time.monotonic() - started < 20, "probe must be bounded"


def test_probe_subprocess_is_given_a_hard_timeout(monkeypatch):
    """The bound is passed to subprocess.run, which is what kills the child."""
    seen: dict = {}

    def _spy(cmd, **kw):
        seen.update(kw)
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setenv("APD_LOOP_PROBE_URLS", "https://api.github.com")
    monkeypatch.setattr(loop.subprocess, "run", _spy)
    loop._network_reachable()
    assert seen.get("timeout"), "probe must pass a timeout so the child is killable"


def test_nonpositive_probe_timeout_fails_open(monkeypatch):
    """PR #113 review: a 0/negative timeout makes every urlopen fail instantly.

    The child would exit 1 and a healthy network would read as unreachable,
    suppressing escalation indefinitely. Honor the fail-open promise instead.
    """
    monkeypatch.setenv("APD_LOOP_PROBE_URLS", "https://api.github.com")

    def _never(*a, **k):
        raise AssertionError("must not spawn a probe with a nonsensical timeout")

    monkeypatch.setattr(loop.subprocess, "run", _never)
    for bad in ("0", "-5", "-0.1"):
        monkeypatch.setenv("APD_LOOP_PROBE_TIMEOUT_S", bad)
        assert loop._network_reachable() is True, f"timeout {bad} must fail open"


def test_probe_requires_every_target_to_be_reachable():
    """PR #113 review: OR semantics defeated the documented multi-target remedy.

    With `github,anthropic` and an Anthropic-only outage, an OR probe exits 0 on
    GitHub's response and the provider outage is misread as a genuine failure —
    making the documented config a no-op for the case it was meant to cover.
    """
    import subprocess as _sp
    import sys as _sys

    # Reachable target = a data: URL urlopen can always serve without network.
    ok = "data:text/plain,ok"
    bad = "http://127.0.0.1:9/unreachable"  # discard port, refuses instantly

    def _run(urls):
        return _sp.run(
            [_sys.executable, "-c", loop._PROBE_SCRIPT, "2", *urls],
            capture_output=True, timeout=30,
        ).returncode

    assert _run([ok]) == 0, "a single reachable target is reachable"
    assert _run([ok, ok]) == 0, "all-reachable is reachable"
    assert _run([ok, bad]) == 1, (
        "one unreachable target must make the probe report unreachable"
    )
    assert _run([bad]) == 1


def test_probe_script_uses_urllib_so_proxies_are_honored():
    """PR #113 review: a raw socket connect bypasses HTTP(S)_PROXY.

    In a proxy-only deployment the direct connect fails while the executor works
    fine, which would mask genuine failures as inconclusive.
    """
    assert "urllib" in loop._PROBE_SCRIPT
    assert "socket" not in loop._PROBE_SCRIPT


# --------------------------------------------------------------------------
# Inconclusive-tick behavior in _service_cwd
# --------------------------------------------------------------------------

_CHECK_STDOUT = (
    "fix prompt\n"
    "PR_NUMBER=4242\n"
    "COORDINATOR_CLAIM_ID=claim-abc\n"
    "COORDINATOR_LEASE_EPOCH=1\n"
)

# Deliberately NOT a real binary. `_run_executor` is monkeypatched in every test
# here, so this name only ever feeds `_validate_executor`. Hardcoding a real one
# ("codex") made these tests depend on the dev box's PATH — tick-level viability
# resolved True locally and False in CI, which is a property of the machine, not
# of the behavior under test.
_EXECUTOR = "apd-test-executor-does-not-exist-2417 {prompt}"


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
        loop._tick(_args(tmp_path), _EXECUTOR)

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
        loop._tick(_args(tmp_path), _EXECUTOR)

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
        loop._tick(_args(tmp_path), _EXECUTOR)

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

    `_tick_executor_viability` is pinned to viable here so the assertion is
    about the DISPATCH path only. Without it the test just measures whether the
    configured executor happens to be on PATH — true on a dev box, false in CI,
    where the tick-level stamp is legitimately False before any dispatch runs.
    """
    monkeypatch.setenv("AGENTIC_PR_DASH_ESCALATION_THRESHOLD", "2")
    config.load.cache_clear()
    wt = tmp_path / "wt"
    wt.mkdir()
    released: list = []
    _wire_loop(monkeypatch, wt, reachable=False, released=released)
    monkeypatch.setattr(loop, "_tick_executor_viability", lambda executor, fallback: (True, {}))

    viability: list[bool] = []
    real_record = loop.record_loop_health
    monkeypatch.setattr(
        loop, "record_loop_health",
        lambda cwd, **kw: (viability.append(kw.get("executors_viable")),
                           real_record(cwd, **kw))[1],
    )

    for _ in range(3):
        loop._tick(_args(tmp_path), _EXECUTOR)

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
        loop._tick(_args(tmp_path), _EXECUTOR)

    assert loop.executor_failure_streak(str(wt), 4242) == 2, (
        "a reachable network means a genuine executor failure — streak must still burn"
    )
    marker = loop._escalated_marker_path(str(wt))
    assert marker.exists(), "reaching the threshold must file an escalation marker"
    assert "4242" in json.loads(marker.read_text(encoding="utf-8"))
    assert released, "a genuine failure must still release the claim"


def test_all_spawn_failures_still_counted_during_a_concurrent_outage(
    monkeypatch, tmp_path
):
    """PR #113 review: a local executor defect stays actionable regardless of network.

    A binary removed or chmod'd after startup validation must still burn the
    streak and release the claim even if the network is down at the same time.
    """
    monkeypatch.setenv("AGENTIC_PR_DASH_ESCALATION_THRESHOLD", "2")
    config.load.cache_clear()
    wt = tmp_path / "wt"
    wt.mkdir()
    released: list = []
    _wire_loop(monkeypatch, wt, reachable=False, released=released)  # network DOWN

    def _boom(executor, prompt, cwd):
        raise FileNotFoundError("executor vanished")

    monkeypatch.setattr(loop, "_run_executor", _boom)

    loop._tick(_args(tmp_path), _EXECUTOR)

    assert loop.executor_failure_streak(str(wt), 4242) == 1, (
        "an all-spawn-failure is a LOCAL defect and must be counted even during "
        "a concurrent network outage"
    )
    assert released, "a local executor defect must still release the claim"


def test_run_executor_does_not_pipe_the_executor(monkeypatch, tmp_path):
    """PR #113 review round 2: the streams stay INHERITED.

    Piping risks hanging the loop forever when a descendant of the executor
    holds the pipe's write end open after the executor itself exits.
    """
    seen: dict = {}

    def _spy(parts, **kw):
        seen.update(kw)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(loop.subprocess, "run", _spy)
    assert loop._run_executor("some-exec {prompt}", "p", str(tmp_path)) == 0
    assert "stdout" not in seen and "stderr" not in seen, (
        "the executor's streams must be inherited, not piped"
    )


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


def test_get_new_pr_commits_returns_none_on_blank_stdout(monkeypatch, tmp_path):
    """PR #113 review: a zero exit with empty stdout is an unusable payload.

    `r.stdout or "[]"` turned truncated/lost output into a genuinely empty
    range, so the None path was never reached and `complete` proceeded on
    evidence it never obtained. A real empty range is the literal JSON `[]`.
    """
    monkeypatch.setattr(github_api, "_local_new_commits", lambda *a, **k: [])
    for blank in ("", "   \n"):
        monkeypatch.setattr(
            github_api, "_run",
            lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=blank, stderr=""),
        )
        assert github_api.get_new_pr_commits(7, "", "", str(tmp_path)) is None, (
            f"blank stdout {blank!r} must be unknown, not an empty range"
        )


def test_get_new_pr_commits_returns_none_on_malformed_payload(monkeypatch, tmp_path):
    """A malformed payload is not evidence of an empty range either."""
    monkeypatch.setattr(github_api, "_local_new_commits", lambda *a, **k: [])
    monkeypatch.setattr(
        github_api, "_run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="not json", stderr=""),
    )
    assert github_api.get_new_pr_commits(7, "", "", str(tmp_path)) is None
