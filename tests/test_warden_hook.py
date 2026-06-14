"""Tests for agentic_pr_dash.codex_hooks.run_warden."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agentic_pr_dash.codex_hooks import run_warden


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bash_payload(command: str, cwd: str = "/repo") -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def _make_codex_payload(cmd: str, cwd: str = "/repo") -> dict:
    return {"tool_name": "exec_command", "tool_input": {"cmd": cmd}, "cwd": cwd}


def _run_main(monkeypatch, payload: dict, *, warden_stdout: str = "", warden_rc: int = 0,
              warden_stderr: str = "", warden_oserror: OSError | None = None,
              warden_bin: str | None = "/fake/warden-hook",
              env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run run_warden.main() and capture stdout.  Returns (returncode, captured_stdout)."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.delenv("AGENTIC_PR_DASH_HOOK_WARDEN", raising=False)
    monkeypatch.delenv("WARDEN_HOOK_BIN", raising=False)
    monkeypatch.delenv("WARDEN_HOOK_PATH", raising=False)
    if env:
        for k, v in env.items():
            monkeypatch.setenv(k, v)

    # Stub resolve_warden_hook (accepts the optional normalised-payload arg)
    monkeypatch.setattr(run_warden, "resolve_warden_hook", lambda *a, **kw: warden_bin)

    captured: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: captured.append(
        " ".join(str(a) for a in args)
    ))

    if warden_bin is not None and warden_oserror is None:
        fake_result = MagicMock()
        fake_result.returncode = warden_rc
        fake_result.stdout = warden_stdout
        fake_result.stderr = warden_stderr
        monkeypatch.setattr(
            run_warden.subprocess, "run",
            lambda *args, **kwargs: fake_result,
        )
    elif warden_oserror is not None:
        monkeypatch.setattr(
            run_warden.subprocess, "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(warden_oserror),
        )

    rc = run_warden.main()
    return rc, "\n".join(captured)


# ---------------------------------------------------------------------------
# Allow cases
# ---------------------------------------------------------------------------


def test_allow_empty_warden_stdout(monkeypatch):
    """Empty warden stdout → clean allow, no output."""
    payload = _make_bash_payload("ls -la")
    rc, out = _run_main(monkeypatch, payload, warden_stdout="")
    assert rc == 0
    assert out == ""


def test_allow_warden_output_permission_allow(monkeypatch):
    """permissionDecision:allow → clean allow."""
    warden_out = json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    })
    rc, out = _run_main(monkeypatch, _make_bash_payload("echo hi"), warden_stdout=warden_out)
    assert rc == 0
    assert out == ""


def test_allow_legacy_approve(monkeypatch):
    """Legacy decision:approve → allow."""
    warden_out = json.dumps({"decision": "approve"})
    rc, out = _run_main(monkeypatch, _make_bash_payload("make build"), warden_stdout=warden_out)
    assert rc == 0
    assert out == ""


def test_non_bash_tool_passes_through(monkeypatch):
    """Non-Bash tool payload → exit 0 immediately, no warden call."""
    payload = {"tool_name": "Read", "tool_input": {"path": "/etc/hosts"}, "cwd": "/repo"}
    # warden_bin doesn't matter — subprocess.run should never be called
    called: list[bool] = []
    monkeypatch.setattr(run_warden.subprocess, "run", lambda *a, **kw: called.append(True))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(run_warden, "resolve_warden_hook", lambda *a, **kw: "/fake/warden-hook")
    assert run_warden.main() == 0
    assert called == []


def test_codex_exec_command_normalised_to_bash(monkeypatch):
    """Codex exec_command payload is normalised to Bash and forwarded to warden."""
    payload = _make_codex_payload("rm -rf tmp")
    warden_out = json.dumps({"hookSpecificOutput": {"permissionDecision": "deny",
                                                    "permissionDecisionReason": "blocked"}})
    rc, out = _run_main(monkeypatch, payload, warden_stdout=warden_out)
    assert rc == 0
    decoded = json.loads(out)
    assert decoded["decision"] == "block"


# ---------------------------------------------------------------------------
# Deny cases
# ---------------------------------------------------------------------------


def test_deny_permissiondecision_deny(monkeypatch):
    """permissionDecision:deny → block with the reason."""
    warden_out = json.dumps({
        "hookSpecificOutput": {
            "permissionDecision": "deny",
            "permissionDecisionReason": "this command is not allowed",
        }
    })
    rc, out = _run_main(monkeypatch, _make_bash_payload("bad-cmd"), warden_stdout=warden_out)
    assert rc == 0
    decoded = json.loads(out)
    assert decoded["decision"] == "block"
    assert "not allowed" in decoded["reason"]


def test_deny_legacy_block(monkeypatch):
    """Legacy decision:block → block with reason."""
    warden_out = json.dumps({"decision": "block", "reason": "legacy block"})
    rc, out = _run_main(monkeypatch, _make_bash_payload("bad"), warden_stdout=warden_out)
    decoded = json.loads(out)
    assert decoded["decision"] == "block"
    assert "legacy block" in decoded["reason"]


def test_deny_missing_warden_hook(monkeypatch):
    """Missing warden-hook binary → block with installation guidance."""
    rc, out = _run_main(monkeypatch, _make_bash_payload("ls"), warden_bin=None)
    assert rc == 0
    decoded = json.loads(out)
    assert decoded["decision"] == "block"
    assert "warden-hook" in decoded["reason"]


def test_deny_oserror_on_warden_exec(monkeypatch):
    """OSError executing warden-hook → block with repair guidance."""
    rc, out = _run_main(monkeypatch, _make_bash_payload("ls"),
                        warden_oserror=OSError("permission denied"))
    decoded = json.loads(out)
    assert decoded["decision"] == "block"
    assert "permission denied" in decoded["reason"].lower() or "warden-hook" in decoded["reason"]


def test_deny_malformed_warden_stdout_not_json(monkeypatch):
    """Non-JSON warden stdout → block with MALFORMED message."""
    rc, out = _run_main(monkeypatch, _make_bash_payload("ls"), warden_stdout="NOT_JSON")
    decoded = json.loads(out)
    assert decoded["decision"] == "block"
    assert "malformed" in decoded["reason"].lower() or "unsupported" in decoded["reason"].lower()


def test_deny_malformed_warden_stdout_non_dict(monkeypatch):
    """JSON-but-not-dict warden stdout → block."""
    rc, out = _run_main(monkeypatch, _make_bash_payload("ls"), warden_stdout=json.dumps([1, 2, 3]))
    decoded = json.loads(out)
    assert decoded["decision"] == "block"


def test_deny_warden_nonzero_exit(monkeypatch):
    """Warden exits non-zero → block."""
    rc, out = _run_main(monkeypatch, _make_bash_payload("ls"),
                        warden_rc=1, warden_stderr="warden error detail")
    decoded = json.loads(out)
    assert decoded["decision"] == "block"
    assert "status 1" in decoded["reason"]


# ---------------------------------------------------------------------------
# 'ask' collapse tests
# ---------------------------------------------------------------------------


def _ask_payload(command: str) -> str:
    return json.dumps({
        "hookSpecificOutput": {
            "permissionDecision": "ask",
            "permissionDecisionReason": "needs approval",
        }
    })


def _plant_probe(root: Path, rel: str) -> None:
    """Create an executable file at *root*/*rel* so the probe resolves in-repo."""
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#!/bin/sh\necho v\n")
    target.chmod(0o755)


def test_ask_version_probe_trusted_binary_allow(monkeypatch, tmp_path):
    """ask + trusted playwright --version (real in-repo probe) → allow (None)."""
    _plant_probe(tmp_path, "node_modules/.bin/playwright")
    cmd = "node_modules/.bin/playwright --version"
    warden_out = _ask_payload(cmd)
    rc, out = _run_main(monkeypatch,
                        _make_bash_payload(cmd, cwd=str(tmp_path)),
                        warden_stdout=warden_out)
    assert rc == 0
    assert out == ""


def test_ask_version_probe_frontend_path_allow(monkeypatch, tmp_path):
    """ask + frontend/node_modules/.bin/playwright --version (real probe) → allow."""
    _plant_probe(tmp_path, "frontend/node_modules/.bin/playwright")
    cmd = "frontend/node_modules/.bin/playwright --version"
    warden_out = _ask_payload(cmd)
    rc, out = _run_main(monkeypatch,
                        _make_bash_payload(cmd, cwd=str(tmp_path)),
                        warden_stdout=warden_out)
    assert rc == 0
    assert out == ""


def test_ask_version_probe_nonexistent_binary_denied(monkeypatch):
    """ask + version probe that does NOT resolve to a real in-repo file → deny.

    Finding 6 (BOU-1671): the probe must resolve to a real file under the repo
    root before being auto-allowed, so a relative node_modules/.bin/playwright
    that is absent (or planted in a writable subdir that is not the repo) is not
    treated as a trusted probe.
    """
    cmd = "node_modules/.bin/playwright --version"
    warden_out = _ask_payload(cmd)
    rc, out = _run_main(monkeypatch,
                        _make_bash_payload(cmd, cwd="/repo/does/not/exist"),
                        warden_stdout=warden_out)
    decoded = json.loads(out)
    assert decoded["decision"] == "block"


def test_ask_chmod_deny_with_git_update_index_guidance(monkeypatch):
    """ask + chmod → deny with git update-index guidance."""
    warden_out = _ask_payload("chmod +x script.sh")
    rc, out = _run_main(monkeypatch,
                        _make_bash_payload("chmod +x script.sh"),
                        warden_stdout=warden_out)
    decoded = json.loads(out)
    assert decoded["decision"] == "block"
    assert "git update-index" in decoded["reason"]


def test_ask_chained_rm_rf_deny(monkeypatch):
    """ask + chained rm -rf ... && → deny with split suggestion."""
    cmd = "rm -rf tmp && make build"
    warden_out = _ask_payload(cmd)
    rc, out = _run_main(monkeypatch, _make_bash_payload(cmd), warden_stdout=warden_out)
    decoded = json.loads(out)
    assert decoded["decision"] == "block"
    assert "rm -rf" in decoded["reason"]


def test_ask_pkill_parent_deny(monkeypatch):
    """ask + pkill -P <pid> → deny."""
    cmd = "pkill -P 12345"
    warden_out = _ask_payload(cmd)
    rc, out = _run_main(monkeypatch, _make_bash_payload(cmd), warden_stdout=warden_out)
    decoded = json.loads(out)
    assert decoded["decision"] == "block"
    assert "pkill" in decoded["reason"]


def test_ask_generic_command_deny_with_reason(monkeypatch):
    """ask + unknown command → generic deny forwarding the warden reason."""
    warden_out = json.dumps({
        "hookSpecificOutput": {
            "permissionDecision": "ask",
            "permissionDecisionReason": "custom approval message",
        }
    })
    rc, out = _run_main(monkeypatch, _make_bash_payload("some-tool --flag"),
                        warden_stdout=warden_out)
    decoded = json.loads(out)
    assert decoded["decision"] == "block"
    assert "custom approval message" in decoded["reason"]


def test_ask_generic_no_reason_generic_deny(monkeypatch):
    """ask with no permissionDecisionReason → generic deny with configuration hint."""
    warden_out = json.dumps({
        "hookSpecificOutput": {"permissionDecision": "ask"}
    })
    rc, out = _run_main(monkeypatch, _make_bash_payload("some-tool"), warden_stdout=warden_out)
    decoded = json.loads(out)
    assert decoded["decision"] == "block"
    assert "Configure Warden" in decoded["reason"]


# ---------------------------------------------------------------------------
# Unit tests for translate / deny helpers (called directly)
# ---------------------------------------------------------------------------


def test_translate_warden_stdout_empty_allows():
    assert run_warden._translate_warden_stdout("") is None


def test_translate_warden_stdout_whitespace_allows():
    assert run_warden._translate_warden_stdout("   \n  ") is None


def test_translate_warden_stdout_allow_decision():
    out = json.dumps({"hookSpecificOutput": {"permissionDecision": "allow"}})
    assert run_warden._translate_warden_stdout(out) is None


def test_translate_warden_stdout_deny_returns_reason():
    out = json.dumps({
        "hookSpecificOutput": {
            "permissionDecision": "deny",
            "permissionDecisionReason": "blocked by policy",
        }
    })
    reason = run_warden._translate_warden_stdout(out)
    assert reason == "blocked by policy"


def test_deny_reason_for_ask_non_string_command():
    """Non-string command (e.g. JSON null) never crashes — collapses to generic deny."""
    reason = run_warden._deny_reason_for_ask(None)
    assert reason is not None
    assert "Configure Warden" in reason


def test_deny_reason_for_ask_version_probe_allow(tmp_path):
    probe = tmp_path / "node_modules" / ".bin" / "playwright"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("#!/bin/sh\n")
    probe.chmod(0o755)
    assert run_warden._deny_reason_for_ask(
        "node_modules/.bin/playwright --version", cwd=str(tmp_path)
    ) is None


def test_deny_reason_for_ask_version_probe_no_cwd_denies():
    """Without a resolvable cwd the probe is not vouched for → deny."""
    assert run_warden._deny_reason_for_ask(
        "node_modules/.bin/playwright --version"
    ) is not None


def test_deny_reason_for_ask_chained_version_probe_deny():
    """A rm pre-command prevents the version probe from being auto-allowed."""
    cmd = "rm -rf tmp && node_modules/.bin/playwright --version"
    assert run_warden._deny_reason_for_ask(cmd) is not None


# ---------------------------------------------------------------------------
# behavior_enabled gate
# ---------------------------------------------------------------------------


def test_behavior_disabled_via_env_skips_warden(monkeypatch):
    """AGENTIC_PR_DASH_HOOK_WARDEN=false → early exit, no warden call."""
    monkeypatch.setenv("AGENTIC_PR_DASH_HOOK_WARDEN", "false")
    called: list[bool] = []
    monkeypatch.setattr(run_warden, "resolve_warden_hook", lambda *a, **kw: called.append(True) or "/x")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_make_bash_payload("ls"))))
    rc = run_warden.main()
    assert rc == 0
    assert called == []
