"""Ownership-marker round-trip + lease wiring (the one-agent-per-PR mechanism)."""

import os
import types
from datetime import datetime, timezone

import pytest

from agentic_pr_dash import agents
from agentic_pr_dash import config
from agentic_pr_dash import maintenance_check as mc
from agentic_pr_dash import session_registry
from agentic_pr_dash._maintenance import worktrees as _worktrees_mod
from agentic_pr_dash._maintenance import markers as _markers_mod
from agentic_pr_dash._maintenance import pr_state as _pr_state_mod


@pytest.fixture(autouse=True)
def _clear_cache():
    config.load.cache_clear()
    yield
    config.load.cache_clear()


def test_arm_marker_roundtrip(tmp_path, legacy_marker_writes):
    """Marker writes are off by default from Stage 4; this pins the on-disk
    shape a pre-Stage-4 install still produces (the read-only shim's contract)."""
    assert mc._write_arm_marker(str(tmp_path), "sess-1", 4242, 7) is True

    marker = tmp_path / ".agentic-pr-dash" / "pr-watch.armed"
    assert marker.exists()

    fields = mc._read_marker(str(tmp_path))
    assert fields is not None
    assert fields["pr"] == "7"
    assert fields["session_id"] == "sess-1"
    assert fields["pid"] == "4242"
    assert "last_heartbeat" not in fields
    assert "heartbeat" not in fields

    session = tmp_path / ".agentic-pr-dash" / "pr-watch.session"
    assert session.read_text(encoding="utf-8").strip() == "sess-1"


def test_marker_honors_legacy_state_dir(tmp_path, legacy_marker_writes):
    # An existing .gaia dir is adopted, so an existing install's markers keep
    # landing in the same place after the rename.
    (tmp_path / ".gaia").mkdir()
    config.load.cache_clear()
    assert mc._write_arm_marker(str(tmp_path), "s", 1, 2) is True
    assert (tmp_path / ".gaia" / "pr-watch.armed").exists()
    assert not (tmp_path / ".agentic-pr-dash").exists()


def test_marker_path_uses_configured_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_PR_DASH_STATE_DIR", ".markers")
    config.load.cache_clear()
    assert mc._marker_path(str(tmp_path)).endswith(os.path.join(".markers", "pr-watch.armed"))


def test_marker_session_id_read(tmp_path, legacy_marker_writes):
    mc._write_arm_marker(str(tmp_path), "owner-xyz", 99, 3)
    assert mc._marker_session_id(str(tmp_path)) == "owner-xyz"


def test_marker_session_id_absent(tmp_path):
    assert mc._marker_session_id(str(tmp_path)) is None


def test_lease_seconds_from_config(tmp_path, monkeypatch):
    monkeypatch.delenv("GAIA_PR_WATCH_LEASE_SECONDS", raising=False)
    monkeypatch.setenv("AGENTIC_PR_DASH_LEASE_SECONDS", "123")
    config.load.cache_clear()
    assert mc._fix_lease_seconds() == 123


def test_lease_legacy_env_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("GAIA_PR_WATCH_LEASE_SECONDS", "777")
    config.load.cache_clear()
    assert mc._fix_lease_seconds() == 777


def _state(pid, worktree_path, *, terminal=False, fp=True, src="launch-worktree-cli",
           cli="claude", session_id="gaia-other"):
    return types.SimpleNamespace(
        pid=pid, worktree_path=worktree_path, is_terminal=terminal,
        is_feature_pipeline=fp, launch_source=src, cli=cli, session_id=session_id,
    )


def test_live_independent_owner_paths_excludes_matching_session_id(tmp_path, monkeypatch):
    """A registry entry whose session_id equals ours is never an independent owner,
    even if its pid is not in our ancestor chain (supervisor-restarted same-id
    loop) — else we'd defer to our own PR (PR #7 P2)."""
    owned = tmp_path / "owned"
    owned.mkdir()
    monkeypatch.setattr(mc, "_self_pid_chain", lambda: {555})  # 999 NOT in chain
    _no_process(monkeypatch)
    monkeypatch.setattr(session_registry, "pid_is_live", lambda pid: True)
    monkeypatch.setattr(
        session_registry, "summarize_sessions",
        lambda path=None: _summary(_state(999, str(owned), session_id="sess-self")),
    )
    assert mc._live_independent_owner_paths([str(owned)], "sess-self") == set()


def _summary(*states):
    return types.SimpleNamespace(sessions={i: s for i, s in enumerate(states)})


def _no_registry(monkeypatch):
    monkeypatch.setattr(session_registry, "summarize_sessions", lambda path=None: _summary())
    monkeypatch.setattr(session_registry, "pid_is_live", lambda pid: True)


def _no_process(monkeypatch):
    monkeypatch.setattr(agents, "discover_primary_feature_pipeline_agents", lambda paths, **kw: {})


def test_live_independent_owner_paths_registry_idle_session(tmp_path, monkeypatch):
    """A registry session that is alive (even idle, no CPU gate) and not us marks
    its worktree as independently owned; a path with no signal does not."""
    owned = tmp_path / "owned"
    free = tmp_path / "free"
    owned.mkdir()
    free.mkdir()

    monkeypatch.setattr(mc, "_self_pid_chain", lambda: {555})
    _no_process(monkeypatch)
    monkeypatch.setattr(session_registry, "pid_is_live", lambda pid: True)
    monkeypatch.setattr(session_registry, "summarize_sessions", lambda path=None: _summary(_state(999, str(owned))))

    owners = _worktrees_mod._live_independent_owner_sessions(
        [str(owned), str(free)], "sess-self"
    )
    assert owners == {
        str(owned): (
            _worktrees_mod.IndependentOwnerIdentity(
                session_id="gaia-other", registry_backed=True
            ),
        )
    }
    assert mc._live_independent_owner_paths(
        [str(owned), str(free)], "sess-self"
    ) == {str(owned)}


def test_live_independent_owner_paths_real_registry_path_smoke(tmp_path, monkeypatch):
    """End-to-end through the REAL registry-path resolution (no summarize stub):
    an empty registry must not raise (guards the str-vs-Path regression)."""
    cand = tmp_path / "cand"
    cand.mkdir()
    monkeypatch.setattr(mc, "_self_pid_chain", lambda: {555})
    _no_process(monkeypatch)
    # HOME is a temp dir (conftest), so the registry file does not exist.
    assert mc._live_independent_owner_paths([str(cand)], "sess-self") == set()


def test_cmd_list_owned_nonzero_when_cwd_not_a_worktree(tmp_path):
    """list-owned exits non-zero on a real discovery failure (cwd not a git
    worktree), so the loop falls back instead of treating empty as authoritative
    (PR #7 P2)."""
    args = types.SimpleNamespace(session_id="sess", cwd=str(tmp_path), pid=123)
    assert mc._cmd_list_owned(args) == 3  # tmp_path is not a git repo


def test_cmd_list_owned_nonzero_when_probe_times_out(tmp_path, monkeypatch):
    """A hung git probe is a discovery failure (exit 3), not an infinite stall
    (PR #7 P2)."""
    import subprocess as _sp

    def boom(*a, **k):
        raise _sp.TimeoutExpired(cmd="git", timeout=10)

    monkeypatch.setattr(mc.subprocess, "run", boom)
    args = types.SimpleNamespace(session_id="sess", cwd=str(tmp_path), pid=123)
    assert mc._cmd_list_owned(args) == 3


def test_command_cli_name_honors_supplied_discovery_names():
    """_command_cli_name recognizes a custom CLI when the target allow-list is
    supplied, not only the process-cwd default (PR #7 P2)."""
    # Default config doesn't include 'aider'.
    assert agents._command_cli_name("/usr/bin/aider --message x") is None
    # With the target repo's allow-list it is recognized.
    assert agents._command_cli_name("/usr/bin/aider --message x", {"aider"}) == "aider"


def test_redact_command_for_display_removes_secret_values():
    command = (
        "env E2E_TEST_PASSWORD=supersecret GH_TOKEN=ghp_secret "
        "codex exec --api-key sk-secret --auth-token=auth-secret --model gpt-5"
    )

    redacted = agents._redact_command_for_display(command)

    assert "supersecret" not in redacted
    assert "ghp_secret" not in redacted
    assert "sk-secret" not in redacted
    assert "auth-secret" not in redacted
    assert "E2E_TEST_PASSWORD=<redacted>" in redacted
    assert "GH_TOKEN=<redacted>" in redacted
    assert "--api-key <redacted>" in redacted
    assert "--auth-token=<redacted>" in redacted
    assert "--model gpt-5" in redacted


def test_redact_command_multiword_secret_value_swallowed_to_next_option():
    """`ps` output loses shell quoting: a quoted multi-word secret value
    arrives as separate tokens and every one of them must be redacted, not
    just the first (PR #72 P2)."""
    command = (
        "codex exec --private-key -----BEGIN PRIVATE KEY----- abc123 "
        "-----END PRIVATE KEY----- --model gpt-5"
    )

    redacted = agents._redact_command_for_display(command)

    assert "BEGIN" not in redacted
    assert "abc123" not in redacted
    assert "--private-key <redacted>" in redacted
    assert "--model gpt-5" in redacted


def test_redact_command_inline_multiword_secret_value_swallowed():
    """Multi-word secrets also leak through the inline --name=value form:
    shlex yields `--private-key=-----BEGIN` plus tail fragments, which must be
    swallowed just like the space-separated form (PR #72 P2)."""
    command = (
        "codex exec --private-key=-----BEGIN PRIVATE KEY----- abc123 "
        "-----END PRIVATE KEY----- --model gpt-5 "
        "--env=API_KEY=multi word-value --output out.txt"
    )

    redacted = agents._redact_command_for_display(command)

    assert "BEGIN" not in redacted
    assert "abc123" not in redacted
    assert "multi" not in redacted
    assert "word-value" not in redacted
    assert "--private-key=<redacted>" in redacted
    assert "--env=API_KEY=<redacted>" in redacted
    assert "--model gpt-5" in redacted
    assert "--output" in redacted


def test_redact_command_env_assignment_embedded_in_option_value():
    """Wrapper forms like --env=GH_TOKEN=... hide the secret assignment in the
    option VALUE; the outer name check alone must not let it through (PR #72 P2)."""
    command = (
        "codex exec --env=GH_TOKEN=ghp_secret --build-arg=API_KEY=sk-secret "
        "--model gpt-5"
    )

    redacted = agents._redact_command_for_display(command)

    assert "ghp_secret" not in redacted
    assert "sk-secret" not in redacted
    assert "--env=GH_TOKEN=<redacted>" in redacted
    assert "--build-arg=API_KEY=<redacted>" in redacted
    assert "--model gpt-5" in redacted


def test_redact_unparsed_command_embedded_env_assignment():
    """The unparsed fallback (unbalanced quote) must also catch secret
    assignments embedded in option values (PR #72 P2)."""
    command = "codex exec --env=GH_TOKEN=ghp_secret 'unterminated"

    redacted = agents._redact_command_for_display(command)

    assert "ghp_secret" not in redacted
    assert "--env=GH_TOKEN=<redacted>" in redacted


def test_redact_command_auth_header_style_option_names():
    """--authorization / --auth-header carry credentials; the parsed-path name
    check must cover the same auth forms as the fallback regex (PR #72 P2)."""
    command = (
        "codex exec --authorization Bearer-abc123 --auth-header=Basic-xyz789 "
        "--model gpt-5"
    )

    redacted = agents._redact_command_for_display(command)

    assert "Bearer-abc123" not in redacted
    assert "Basic-xyz789" not in redacted
    assert "--authorization <redacted>" in redacted
    assert "--auth-header=<redacted>" in redacted
    assert "--model gpt-5" in redacted


def test_discover_active_agents_returns_redacted_command(tmp_path, monkeypatch):
    command = "codex exec --token ghp_secret --prompt ok"
    process_table = f"123 1 5.0 {command}\n"

    monkeypatch.setattr(agents, "_run_process_table", lambda: process_table)
    monkeypatch.setattr(agents, "_collect_cwds", lambda: {123: str(tmp_path)})

    discovered = agents.discover_active_agents([str(tmp_path)])

    assert discovered[str(tmp_path)][0].command == "codex exec --token <redacted> --prompt ok"


def test_discover_feature_pipeline_agents_returns_redacted_command(tmp_path, monkeypatch):
    command = "codex exec /feature-pipeline --linear-api-key lin_secret"
    process_table = f"123 1 5.0 {command}\n"

    monkeypatch.setattr(agents, "_run_process_table", lambda: process_table)
    monkeypatch.setattr(agents, "_collect_cwds", lambda: {123: str(tmp_path)})

    discovered = agents.discover_primary_feature_pipeline_agents([str(tmp_path)])

    assert discovered[str(tmp_path)][0].command == "codex exec /feature-pipeline --linear-api-key <redacted>"


def test_live_independent_owner_paths_registry_honors_discovery_names(tmp_path, monkeypatch):
    """A live registry session whose cli is NOT in the target repo's discovery_names
    (default: claude, codex) is not treated as an owner (PR #7 P2)."""
    owned = tmp_path / "owned"
    owned.mkdir()
    monkeypatch.setattr(mc, "_self_pid_chain", lambda: {555})
    _no_process(monkeypatch)
    monkeypatch.setattr(session_registry, "pid_is_live", lambda pid: True)
    monkeypatch.setattr(
        session_registry, "summarize_sessions",
        lambda path=None: _summary(_state(999, str(owned), cli="aider")),  # not allowed
    )
    assert mc._live_independent_owner_paths([str(owned)], "sess-self") == set()


def test_live_independent_owner_paths_excludes_self(tmp_path, monkeypatch):
    """A live owner whose pid is in our ancestor chain is US, not a foreign owner."""
    owned = tmp_path / "owned"
    owned.mkdir()

    monkeypatch.setattr(_worktrees_mod, "_self_pid_chain", lambda: {555, 42})
    _no_process(monkeypatch)
    monkeypatch.setattr(session_registry, "pid_is_live", lambda pid: True)
    monkeypatch.setattr(session_registry, "summarize_sessions", lambda path=None: _summary(_state(42, str(owned))))
    assert mc._live_independent_owner_paths([str(owned)], "sess-self") == set()


def test_live_independent_owner_paths_uses_per_candidate_config_registry(tmp_path, monkeypatch):
    """The registry path is resolved from EACH CANDIDATE worktree's own config,
    not the caller's cwd, so a sibling that points session_registry_path elsewhere
    is still consulted (PR #7 P2)."""
    cand = tmp_path / "cand"
    cand.mkdir()
    captured = {}
    monkeypatch.setattr(mc, "_self_pid_chain", lambda: {555})
    _no_process(monkeypatch)
    monkeypatch.setattr(session_registry, "pid_is_live", lambda pid: True)
    monkeypatch.setattr(session_registry, "registry_path", lambda cwd=None: f"REG::{cwd}")

    def fake_summary(path=None):
        captured["path"] = path
        return _summary()

    monkeypatch.setattr(session_registry, "summarize_sessions", fake_summary)
    mc._live_independent_owner_paths([str(cand)], "sess-self")
    assert captured["path"] == f"REG::{cand}"


def test_live_independent_owner_paths_process_scan_idle(tmp_path, monkeypatch):
    """The process scan runs with min_cpu=0.0 so an idle-but-alive session counts,
    and a foreign pid marks the path owned."""
    owned = tmp_path / "owned"
    owned.mkdir()
    captured = {}

    def fake_discover(paths, *, min_cpu=1.0, discovery_names=None):
        captured["min_cpu"] = min_cpu
        return {str(owned): [types.SimpleNamespace(pid=999, cli_name="claude")]}

    monkeypatch.setattr(mc, "_self_pid_chain", lambda: {555})
    _no_registry(monkeypatch)
    monkeypatch.setattr(agents, "discover_primary_feature_pipeline_agents", fake_discover)

    result = _worktrees_mod._live_independent_owner_sessions(
        [str(owned)], "sess-self"
    )
    assert result == {
        str(owned): (
            _worktrees_mod.IndependentOwnerIdentity(
                session_id="pid:999", registry_backed=False
            ),
        )
    }
    assert captured["min_cpu"] == 0.0  # liveness, not activity


def test_live_independent_owner_paths_ignores_marker_only(tmp_path, monkeypatch):
    """A pr-watch marker alone (live pid, but no registry/process session) is NOT
    treated as an owner here: an armed-but-not-looping marker is intentionally
    takeover-able, and an armed-and-looping one is already handled by
    `_live_foreign_owner` / `_marker_live_foreign_pid` elsewhere (PR #7 P2 #Y)."""
    armed = tmp_path / "armed"
    armed.mkdir()
    mc._write_arm_marker(str(armed), "some-foreign-id", 9999, 2)  # live-ish marker only

    monkeypatch.setattr(mc, "_self_pid_chain", lambda: {555})
    _no_registry(monkeypatch)
    _no_process(monkeypatch)

    assert mc._live_independent_owner_paths([str(armed)], "sess-self") == set()


def test_collect_owned_skips_and_heals_independent_owner(tmp_path, monkeypatch, legacy_marker_writes):
    """Reconciliation does not adopt an independently-owned sibling, AND an
    already-stolen worktree (our marker on it) is not re-emitted — it is healed
    because the live-independent-owner gate runs before the 'already ours' emit
    (PR #7 review, P1 #4)."""
    orphan = tmp_path / "orphan"
    stolen = tmp_path / "stolen"
    orphan.mkdir()
    stolen.mkdir()

    # 'stolen' already carries OUR id from a prior bad adoption.
    mc._write_arm_marker(str(stolen), "claude-uuid-X", 555, 102)
    assert mc._marker_session_id(str(stolen)) == "claude-uuid-X"

    monkeypatch.setattr(
        _worktrees_mod,
        "_iter_worktrees_with_branch",
        lambda cwd: [(str(orphan), "br-orphan"), (str(stolen), "br-stolen")],
    )
    monkeypatch.setattr(
        _worktrees_mod, "_list_my_open_prs", lambda cwd, timeout=15: {"br-orphan": (101, False), "br-stolen": (102, False)}
    )
    # The stolen worktree has a live INDEPENDENT owner now; the orphan has none.
    monkeypatch.setattr(
        _worktrees_mod,
        "_live_independent_owner_paths",
        lambda paths, sid, config_cwd=None: {os.path.abspath(str(stolen))},
    )

    result = mc._collect_owned_worktrees("claude-uuid-X", str(tmp_path), 555)
    assert str(orphan) in result          # genuinely orphaned → adopted (BOU-1442)
    assert str(stolen) not in result      # contested/stolen → not serviced (healed)


def test_collect_owned_adopts_orphan_when_no_independent_owner(tmp_path, monkeypatch, legacy_marker_writes):
    """No independent owner → a genuinely-orphaned PR worktree is still adopted,
    preserving BOU-1442 sub-agent pickup / crash recovery."""
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    monkeypatch.setattr(
        _worktrees_mod, "_iter_worktrees_with_branch", lambda cwd: [(str(orphan), "br-orphan")]
    )
    monkeypatch.setattr(_worktrees_mod, "_list_my_open_prs", lambda cwd, timeout=15: {"br-orphan": (101, False)})
    monkeypatch.setattr(_worktrees_mod, "_live_independent_owner_paths", lambda paths, sid, config_cwd=None: set())

    result = mc._collect_owned_worktrees("claude-uuid-X", str(tmp_path), 555)
    assert str(orphan) in result
    assert mc._marker_session_id(str(orphan)) == "claude-uuid-X"


def test_collect_owned_skips_foreign_owner_with_fresh_heartbeat_even_dead_pid(tmp_path, monkeypatch):
    """The adoption path must use the same heartbeat liveness as _check_worktree.

    A dead PID from a short-lived arming shell is not enough to steal a marker
    while the owning session heartbeat is fresh.
    """
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    marker = config.load(str(foreign)).watch_marker_for(str(foreign))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        "\n".join(
            [
                "pr=101",
                "session_id=foreign-session",
                "pid=2147480000",
                f"last_heartbeat={datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        _worktrees_mod, "_iter_worktrees_with_branch", lambda cwd: [(str(foreign), "br-foreign")]
    )
    monkeypatch.setattr(_worktrees_mod, "_list_my_open_prs", lambda cwd, timeout=15: {"br-foreign": (101, False)})
    monkeypatch.setattr(_worktrees_mod, "_live_independent_owner_paths", lambda paths, sid, config_cwd=None: set())

    result = mc._collect_owned_worktrees("claude-uuid-X", str(tmp_path), 555)

    assert result == []
    assert mc._marker_session_id(str(foreign)) == "foreign-session"


def test_collect_owned_never_adopts_dead_foreign_marker(tmp_path, monkeypatch):
    """BOU-1953 root cause #3 regression: a worktree that already carries a
    DIFFERENT session's marker must NOT be re-stamped with our session_id, even
    when that foreign owner is genuinely dead (stale heartbeat, no active fix
    lease, dead pid) and the branch's PR happens to be one of "my" open GitHub
    PRs (the same GH login as us). ``git worktree list`` from a shared-repo
    anchor enumerates sibling worktrees from unrelated epics/branches that a
    past, now-finished session armed for itself — that marker must be left
    alone here; genuine dead-session PR recovery is `_adopt_orphan_prs`'s job,
    gated on the durable session registry, not this filesystem-marker path.
    """
    foreign = tmp_path / "interactive-scenes-p4-interact"
    foreign.mkdir()
    marker = config.load(str(foreign)).watch_marker_for(str(foreign))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        "\n".join(
            [
                "pr=202",
                "session_id=old-finished-session",
                "pid=2147480000",
                "armed_at=2026-01-01T00:00:00Z",
                "last_heartbeat=2026-01-01T00:05:00Z",
                "",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        _worktrees_mod,
        "_iter_worktrees_with_branch",
        lambda cwd: [(str(foreign), "interactive-scenes-p4-interact")],
    )
    # This branch's PR is genuinely one of "my" (same GH login) open PRs — that
    # alone must not be sufficient evidence THIS session ever armed it.
    monkeypatch.setattr(
        _worktrees_mod,
        "_list_my_open_prs",
        lambda cwd, timeout=15: {"interactive-scenes-p4-interact": (202, False)},
    )
    monkeypatch.setattr(
        _worktrees_mod, "_live_independent_owner_paths", lambda paths, sid, config_cwd=None: set()
    )

    result = mc._collect_owned_worktrees("current-session-961d5c60", str(tmp_path), 555)

    assert result == []
    assert mc._marker_session_id(str(foreign)) == "old-finished-session"


def test_collect_owned_still_refreshes_worktree_this_session_armed(tmp_path, monkeypatch, legacy_marker_writes):
    """A worktree THIS session actually armed stays owned and its heartbeat/pr
    stays refreshable — the new foreign-marker guard must not touch the
    already-ours path (regression companion to the adoption-blocking fix)."""
    own = tmp_path / "own-epic"
    own.mkdir()
    mc._write_arm_marker(str(own), "current-session-961d5c60", 555, 101)

    monkeypatch.setattr(
        _worktrees_mod, "_iter_worktrees_with_branch", lambda cwd: [(str(own), "own-branch")]
    )
    # Branch's open PR now differs (102) — the already-ours path must still
    # rewrite it in place (preflight parity), unaffected by the new guard.
    monkeypatch.setattr(
        _worktrees_mod, "_list_my_open_prs", lambda cwd, timeout=15: {"own-branch": (102, False)}
    )
    monkeypatch.setattr(
        _worktrees_mod, "_live_independent_owner_paths", lambda paths, sid, config_cwd=None: set()
    )

    result = mc._collect_owned_worktrees("current-session-961d5c60", str(tmp_path), 555)

    assert str(own) in result
    assert mc._marker_session_id(str(own)) == "current-session-961d5c60"
    assert _worktrees_mod._marker_pr(str(own)) == "102"


def test_check_worktree_defers_to_live_independent_owner(tmp_path, monkeypatch):
    """When work exists but a live independent session owns the worktree, check
    DEFERS (exit 0) instead of declaring work (exit 10) — the heal/safety net for
    stolen markers and unsafe loop fallbacks (PR #7 review, P1 #4/#5)."""
    pr = types.SimpleNamespace(
        number=7, is_draft=False, failing_checks=[], latest_commit_sha="abc",
    )
    heartbeats = []
    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: pr)
    monkeypatch.setattr(
        _markers_mod, "_touch_owner_heartbeat", lambda cwd, sid, work: heartbeats.append(work)
    )
    from agentic_pr_dash import maintenance as _maint
    monkeypatch.setattr(_maint, "blockers_for_pr", lambda pr: ["review_comments"])

    # Foreign owner present → defer, and DO NOT refresh the heartbeat/lease (else a
    # stolen marker would pin the PR; PR #7 review, P2).
    monkeypatch.setattr(
        _worktrees_mod,
        "_live_independent_owner_sessions",
        lambda paths, sid: {
            os.path.abspath(str(tmp_path)): (
                _worktrees_mod.IndependentOwnerIdentity(
                    session_id="sess-independent", registry_backed=True
                ),
            )
        },
    )
    code, text = mc._check_worktree(str(tmp_path), "sess-self")
    assert code == 0
    assert "independent owner" in text
    assert heartbeats == []  # no heartbeat/lease write while deferring

    # No foreign owner → work is surfaced AND the fix lease is stamped.
    monkeypatch.setattr(
        _worktrees_mod, "_live_independent_owner_sessions", lambda paths, sid: {}
    )
    monkeypatch.setattr(_maint, "build_maintenance_prompt", lambda pr, failed_logs=None: "PROMPT")
    monkeypatch.setattr(_maint, "build_maintenance_summary", lambda pr, **kw: "SUMMARY")
    code2, text2 = mc._check_worktree(str(tmp_path), "sess-self")
    assert code2 == 10
    assert "PR_NUMBER=7" in text2
    assert heartbeats == [True]  # heartbeat refreshed with work=True before servicing


def test_check_worktree_clean_tick_does_not_refresh_stolen_marker(tmp_path, monkeypatch):
    """On a CLEAN tick, a marker that is ours but has a live independent owner is
    stolen — we must NOT refresh its heartbeat (which would pin it via
    `_live_foreign_owner` until TTL); we defer and let it go stale (PR #7 P2 #X).
    A clean tick on a marker that is genuinely ours still refreshes."""
    pr = types.SimpleNamespace(number=7, is_draft=False, failing_checks=[], latest_commit_sha="abc")
    heartbeats = []
    monkeypatch.setattr(_markers_mod, "_live_foreign_owner", lambda cwd, sid: None)
    monkeypatch.setattr(_pr_state_mod, "_resolve_pr_for_branch", lambda cwd: pr)
    monkeypatch.setattr(_markers_mod, "_touch_owner_heartbeat", lambda cwd, sid, work: heartbeats.append(work))
    from agentic_pr_dash import maintenance as _maint
    monkeypatch.setattr(_maint, "blockers_for_pr", lambda pr: [])  # CLEAN
    monkeypatch.setattr(_markers_mod, "_marker_session_id", lambda cwd: "sess-self")  # marker is ours

    # Stolen: a live independent owner is present → defer, no heartbeat refresh.
    monkeypatch.setattr(_worktrees_mod, "_live_independent_owner_paths", lambda paths, sid, config_cwd=None: {os.path.abspath(str(tmp_path))})
    code, text = mc._check_worktree(str(tmp_path), "sess-self")
    assert code == 0
    assert "stale stolen marker" in text
    assert heartbeats == []

    # Genuinely ours (no independent owner) → refresh the alive heartbeat.
    monkeypatch.setattr(_worktrees_mod, "_live_independent_owner_paths", lambda paths, sid, config_cwd=None: set())
    code2, text2 = mc._check_worktree(str(tmp_path), "sess-self")
    assert code2 == 0
    assert text2 == "nothing pending"
    assert heartbeats == [False]
