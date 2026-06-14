"""Upstream codex hook: delegate Bash PreToolUse decisions to ``warden-hook``.

Resolves ``warden-hook`` from env / PATH / venv / repo / home-dir fallbacks,
feeds it the normalised hook payload, and translates its stdout into either a
clean allow (no output, exit 0) or a Codex ``{"decision":"block","reason":...}``
JSON response.

Supports both Claude PreToolUse JSON hooks and Codex exec_command hooks via the
shared ``normalized_payload`` adapter.

Repo-agnostic: no gaia-specific policy is hard-coded here.  The gaia shim
supplies policy via warden's own allow-list configuration.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from agentic_pr_dash.codex_hooks._payload import (
    behavior_enabled,
    load_payload,
    normalized_payload,
    repo_root,
)


MALFORMED_WARDEN_OUTPUT = (
    "Warden returned malformed or unsupported PreToolUse output for Bash. "
    "Expected a JSON object with hookSpecificOutput.permissionDecision of deny or no "
    "hook output for allow."
)


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _candidate_from_env() -> str | None:
    env_candidates = [
        os.environ.get("WARDEN_HOOK_BIN", "").strip(),
        os.environ.get("WARDEN_HOOK_PATH", "").strip(),
    ]
    for configured in env_candidates:
        if not configured:
            continue

        expanded = Path(configured).expanduser()
        if _is_executable_file(expanded):
            return str(expanded)

        resolved = shutil.which(configured)
        if resolved:
            return resolved

    return None


def _candidate_from_login_shell() -> str | None:
    shell = os.environ.get("SHELL", "").strip()
    if not shell:
        return None

    try:
        result = subprocess.run(
            [shell, "-lic", "command -v warden-hook"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None

    if result.returncode != 0:
        return None

    candidates = result.stdout.strip().splitlines()
    return candidates[0] if candidates else None


def resolve_warden_hook(payload: dict | None = None) -> str | None:
    """Locate the ``warden-hook`` binary.

    Search order:
    1. ``WARDEN_HOOK_BIN`` / ``WARDEN_HOOK_PATH`` env vars.
    2. ``warden-hook`` on ``PATH`` (via ``shutil.which``).
    3. Fixed fallback paths: venv next to this Python, repo ``.venv``,
       repo ``scripts/``, ``~/.local/bin``, ``~/bin``, ``~/.cargo/bin``,
       ``/opt/homebrew/bin``, ``/usr/local/bin``.
    4. Login-shell ``command -v warden-hook`` (last resort, spawns a subshell).

    *payload* (the normalised hook payload) is used to resolve the repo-local
    ``.venv``/``scripts`` fallbacks against the actual checkout (its ``cwd``)
    rather than the hook process cwd — so repo-local installs where
    ``warden-hook`` lives in the target worktree are found.
    """
    env_candidate = _candidate_from_env()
    if env_candidate:
        return env_candidate

    path_candidate = shutil.which("warden-hook")
    if path_candidate:
        return path_candidate

    python_bin = Path(sys.executable).resolve().parent
    repo = repo_root(payload)
    fallback_candidates = [
        python_bin / "warden-hook",
        repo / ".venv" / "bin" / "warden-hook",
        repo / "scripts" / "warden-hook",
        Path.home() / ".local" / "bin" / "warden-hook",
        Path.home() / "bin" / "warden-hook",
        Path.home() / ".cargo" / "bin" / "warden-hook",
        Path("/opt/homebrew/bin/warden-hook"),
        Path("/usr/local/bin/warden-hook"),
    ]
    for candidate in fallback_candidates:
        if _is_executable_file(candidate):
            return str(candidate)

    return _candidate_from_login_shell()


# Only this explicit allowlist of trusted, dependency-provided probe binaries is
# auto-allowed when Warden returns 'ask'. Their --version/--help output is inert.
# Any other local bin (a project-specific or compromised tool in node_modules/.bin)
# falls through to the deny path rather than executing without a human gate.
_TRUSTED_PROBE_BINARIES = ("playwright",)

# The probe must live in one of these exact, repo-local node_modules/.bin directories.
# An explicit allowlist (rather than a general path pattern) blocks both directory
# traversal (`../tmp/node_modules/.bin/...`) and arbitrary repo-relative directories
# (`tmp/evil/node_modules/.bin/...`) that could hold an attacker-planted binary.
_PROBE_DIRS = ("node_modules/.bin/", "frontend/node_modules/.bin/")

_PROBE_DIR_ALT = "|".join(re.escape(d) for d in _PROBE_DIRS)
_TRUSTED_PROBE_ALT = "|".join(re.escape(name) for name in _TRUSTED_PROBE_BINARIES)

# Anchored to the WHOLE command (used with fullmatch) so only a standalone read-only
# probe is allowed. The probe directory must be one of the known repo-local dirs and
# the binary one of the trusted probes — so a chained pre-command (`true&&...`), a
# command substitution (`$(id)/...`), a traversal path (`../tmp/...`) or any other
# directory all fail to match and fall through to the destructive/deny checks below.
# Token separators are horizontal whitespace only ([ \t]); a newline is a shell command
# separator, so `playwright\n--version` must not pass as a single read-only probe.
_VERSION_PROBE_RE = re.compile(
    r"(?:" + _PROBE_DIR_ALT + r")"              # repo-local probe dir, e.g. frontend/node_modules/.bin/
    r"(?:" + _TRUSTED_PROBE_ALT + r")"          # trusted probe binary only
    r"[ \t]+(?:--version|-V|version|--help|-h)" # read-only flag (horizontal ws only)
    r"[ \t]*",                                  # trailing horizontal whitespace only
)

_DESTRUCTIVE_CHAIN_RE = re.compile(
    r"""
    (?:
        # rm -rf anything followed by && (chained destructive op)
        rm\s+-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+\S+.*?&&
        |
        rm\s+-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+\S+.*?&&
    )
    """,
    re.VERBOSE,
)

_CHMOD_SCRIPT_RE = re.compile(
    r"""
    ^chmod\s+         # starts with chmod
    (?:               # mode — octal or symbolic (with optional ugoa who-prefix)
        [0-7]{3,4}
        |
        [ugoa]*[-+=][rwxXst]+(?:,[ugoa]*[-+=][rwxXst]+)*
    )
    \s+               # space
    \S+               # file/path argument
    """,
    re.VERBOSE,
)

_PKILL_PARENT_CLEANUP_RE = re.compile(
    r"""
    ^\s*
    pkill\b
    (?=.*(?:^|\s)(?:-P\s*\d+|--parent(?:=|\s+)\d+)\b)
    .*
    $
    """,
    re.VERBOSE,
)


def _trimmed_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _emit_codex_block(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def _append_detail(message: str, detail: str | None) -> str:
    trimmed = _trimmed_string(detail)
    if trimmed is None:
        return message
    return f"{message} Details: {trimmed}"


def _probe_resolves_under_repo(command: str, cwd: object) -> bool:
    """Return True iff the version-probe binary resolves to a real file under *cwd*.

    The probe path is repo-relative (e.g. ``node_modules/.bin/playwright``). We
    resolve it against the payload cwd/repo root and confirm the fully-resolved
    path (following symlinks) stays inside that root and is an existing regular
    file. This prevents auto-allowing a probe that, when the Bash command runs
    from a writable subdirectory, points at an attacker-planted binary —
    a relative ``node_modules/.bin/playwright`` that escapes the repo (via a
    symlink) or does not exist is NOT treated as a trusted probe.
    """
    if not isinstance(cwd, str) or not cwd:
        # No reliable root to resolve against — do not vouch for the probe.
        return False

    # The matched probe path is the leading token of the command.
    probe_rel = command.strip().split()[0]
    try:
        root = Path(cwd).resolve(strict=False)
        resolved = (root / probe_rel).resolve(strict=False)
    except OSError:
        return False

    if not _is_executable_file(resolved):
        return False

    try:
        resolved.relative_to(root)
    except ValueError:
        return False
    return True


def _deny_reason_for_ask(
    command: object,
    permission_reason: str | None = None,
    *,
    cwd: object = None,
) -> str | None:
    """Return a deterministic deny reason for commands that triggered warden 'ask'.

    Returns None to signal that the command should be allowed (no blocking output).
    Returns a non-empty string to signal a deny with that actionable reason.

    Patterns handled:
    - Read-only version/help probes (e.g. node_modules/.bin/playwright --version) -> allow
    - chmod on a file -> deny with safe alternative (git update-index or apply_patch)
    - Chained destructive commands (rm -rf ... && ...) -> deny with split suggestion
    - pkill -P <pid> parent cleanup -> deny
    - Anything else -> generic deny with configuration hint

    A non-string command (e.g. a JSON null that normalizes to None) never matches an
    allow/specific pattern and collapses to the generic deny — never a crash.
    """
    cmd = command.strip() if isinstance(command, str) else ""

    # Read-only --version/--help probes from a trusted binary in a known repo-local
    # node_modules/.bin dir are safe ops -> allow. fullmatch requires the entire command
    # to be the probe and the dir/binary come from explicit allowlists, so a destructive
    # prefix (`rm -rf tmp && .../playwright --version`), a space-free chain
    # (`true&&frontend/.../playwright --version`), a command substitution
    # (`$(id)/.../playwright --version`), or a non-repo path (`../tmp/.../playwright
    # --version`) all fail to match and fall through to the deny path below.
    # The regex shape (fullmatch, explicit dir/binary allowlists) already blocks
    # chains, substitutions and `../` traversal. We additionally require the
    # probe to resolve to a real file inside the repo root so a relative
    # node_modules/.bin/playwright running from a writable subdir (or via a
    # symlink escaping the repo) is not auto-allowed.
    if _VERSION_PROBE_RE.fullmatch(cmd) and _probe_resolves_under_repo(cmd, cwd):
        return None

    # chmod — suggest safe alternatives
    if _CHMOD_SCRIPT_RE.match(cmd):
        return (
            "Direct chmod is not allowed. "
            "To mark a script as executable, use one of these safe alternatives:\n"
            "  • git update-index --chmod=+x <path>  (sets the executable bit in git)\n"
            "  • apply_patch with a mode change (for non-git files)\n"
            "Run each command separately without chaining with &&."
        )

    # Chained destructive rm -rf ... && ... -> suggest splitting
    if _DESTRUCTIVE_CHAIN_RE.search(cmd):
        return (
            "Chained commands containing rm -rf are not allowed. "
            "Run each command separately:\n"
            "  1. rm -rf <target>\n"
            "  2. env VAR=value scripts/<script>.sh\n"
            "Avoid && chains when any component is destructive."
        )

    if _PKILL_PARENT_CLEANUP_RE.match(cmd):
        return (
            "`pkill -P <pid>` process cleanup is not allowlisted for non-interactive "
            "agent recovery. Prefer polling the existing tool session to completion, "
            "or use a documented repo recovery wrapper when one exists. If direct "
            "process cleanup is still required, ask the user for explicit approval "
            "and keep the command narrowly scoped."
        )

    # Fallback: generic actionable deny (never forward 'ask' to the caller)
    if _trimmed_string(permission_reason):
        return _append_detail(
            "Warden requires approval for this Bash command.",
            permission_reason,
        )
    return (
        "Warden returned unsupported PreToolUse permissionDecision 'ask' for Bash. "
        "Configure Warden to allow by emitting no hook output, or to block with "
        "permissionDecision:'deny' and a non-empty permissionDecisionReason."
    )


def _translate_warden_stdout(
    stdout: str, command: object = "", *, cwd: object = None
) -> str | None:
    """Translate warden stdout into a block reason, or None to allow.

    Returns None → allow (no output).
    Returns a str → block with that reason.
    """
    trimmed = stdout.strip()
    if not trimmed:
        return None

    try:
        payload = json.loads(trimmed)
    except json.JSONDecodeError:
        return MALFORMED_WARDEN_OUTPUT

    if not isinstance(payload, dict):
        return MALFORMED_WARDEN_OUTPUT

    legacy_decision = payload.get("decision")
    if legacy_decision == "approve":
        return None
    if legacy_decision == "block":
        reason = _trimmed_string(payload.get("reason"))
        return reason or MALFORMED_WARDEN_OUTPUT

    hook_specific = payload.get("hookSpecificOutput")
    if not isinstance(hook_specific, dict):
        return MALFORMED_WARDEN_OUTPUT

    hook_event_name = hook_specific.get("hookEventName")
    if hook_event_name not in (None, "PreToolUse"):
        return MALFORMED_WARDEN_OUTPUT

    permission_decision = hook_specific.get("permissionDecision")
    permission_reason = _trimmed_string(hook_specific.get("permissionDecisionReason"))

    if permission_decision == "allow":
        return None
    if permission_decision == "deny":
        return permission_reason or MALFORMED_WARDEN_OUTPUT
    if permission_decision == "ask":
        # 'ask' is unsupported by Codex/Claude hooks — collapse to allow or deny
        # deterministically based on command pattern analysis.
        return _deny_reason_for_ask(command, permission_reason, cwd=cwd)
    return MALFORMED_WARDEN_OUTPUT


def main() -> int:
    if not behavior_enabled("warden"):
        return 0

    payload = load_payload()
    normalized = normalized_payload(payload)
    # apply_shared_env omitted upstream: gaia-specific env (CLAUDE_PROJECT_DIR /
    # GAIA_PROJECT_DIR) is not set here; the gaia shim handles that if needed.

    if normalized["tool_name"] != "Bash":
        return 0

    warden_hook = resolve_warden_hook(normalized)
    if warden_hook is None:
        return _emit_codex_block(
            "Warden is required for Bash PreToolUse safety, but `warden-hook` was not found. "
            "install `warden-hook` or set WARDEN_HOOK_BIN/WARDEN_HOOK_PATH to an "
            "executable path."
        )

    try:
        result = subprocess.run(
            [warden_hook],
            input=json.dumps(normalized),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return _emit_codex_block(
            f"Failed to execute warden-hook for Bash safety: {exc}. "
            "Install or repair `warden-hook`, or set WARDEN_HOOK_BIN/WARDEN_HOOK_PATH "
            "to a working executable."
        )

    if result.returncode != 0:
        return _emit_codex_block(
            _append_detail(
                f"Warden exited with status {result.returncode} while checking a Bash command. "
                "Fix the Warden installation or policy configuration before retrying.",
                result.stderr or result.stdout,
            )
        )

    command = normalized.get("tool_input", {}).get("command", "")
    translated_block_reason = _translate_warden_stdout(
        result.stdout, command, cwd=normalized.get("cwd")
    )
    if translated_block_reason is None:
        return 0
    return _emit_codex_block(translated_block_reason)


if __name__ == "__main__":
    raise SystemExit(main())
