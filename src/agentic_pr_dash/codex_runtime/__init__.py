"""Codex hook RUNNER FRAMEWORK shipped by agentic-pr-dash (BOU-1672).

This subpackage holds the repo-agnostic machinery that lets a host repo run its
shared (Claude/Codex) hooks under Codex's hook protocol:

* :mod:`agentic_pr_dash.codex_runtime.payload` — Codex→Claude payload
  normalization (tool-name mapping, ``exec_command`` → Bash ``command``, cwd
  resolution) and the upstream-hook loader (``load_upstream_hook_main``).
* :mod:`agentic_pr_dash.codex_runtime.settings` — layered hook/skill settings
  resolution (defaults + local override + profile + env) and its CLI.
* :mod:`agentic_pr_dash.codex_runtime.runners` — the generic dispatch loops:
  the shared-hook forwarder, the Stop-checks fan-out (run-all + JSON collapse +
  output condensing), and the best-effort SessionStart command runner.

A host repo (gaia) keeps ONLY its config — *which* hooks, the hook command
lists, the manifest contents, default-settings values — and thin shims that
import these framework entrypoints. No framework logic is duplicated downstream.
"""

from agentic_pr_dash.codex_runtime.payload import (
    CODEX_TO_CLAUDE_TOOL,
    apply_shared_env,
    load_payload,
    load_upstream_hook_main,
    normalized_cwd,
    normalized_payload,
    normalized_tool_input,
    normalized_tool_name,
)
from agentic_pr_dash.codex_runtime.runners import (
    HOOK_FAILURE_OUTPUT_LIMIT,
    HookFailure,
    HumanOutputBuffer,
    StopChecksReport,
    StopChecksRunner,
    run_session_commands,
    run_shared_hook,
)
from agentic_pr_dash.codex_runtime.settings import (
    SettingsResolver,
    default_settings_fragment,
    hook_enabled,
    load_settings,
    skill_enabled,
)

__all__ = [
    "CODEX_TO_CLAUDE_TOOL",
    "apply_shared_env",
    "load_payload",
    "load_upstream_hook_main",
    "normalized_cwd",
    "normalized_payload",
    "normalized_tool_input",
    "normalized_tool_name",
    "HOOK_FAILURE_OUTPUT_LIMIT",
    "HookFailure",
    "HumanOutputBuffer",
    "StopChecksReport",
    "StopChecksRunner",
    "run_session_commands",
    "run_shared_hook",
    "SettingsResolver",
    "default_settings_fragment",
    "hook_enabled",
    "load_settings",
    "skill_enabled",
]
