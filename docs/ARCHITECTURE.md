# agentic-pr-dash Architecture

This document is the maintainer map for `agentic-pr-dash`. The README explains
the user-facing workflow; this file explains the package structure, runtime
flows, state files, and integration boundaries.

## Purpose

`agentic-pr-dash` monitors GitHub pull requests and coordinates automated
maintenance without letting multiple agents work the same PR at the same time.
It has three independently useful layers:

- executor commands for read-only blocker detection and completion;
- a loop that dispatches fixes to a configured agent command;
- a FastAPI/HTMX dashboard that renders PR, runner, worktree, and agent state.

The package is project-agnostic. It discovers or accepts the GitHub repo, reads a
config file, shells out to `gh` and `git`, and writes local coordination state.
Project policy such as which agent to run, which task tracker to use, or which
CI runner label matters is configuration.

## Setup Model

Install the executor and loop:

```bash
pip install agentic-pr-dash
```

Install dashboard dependencies too:

```bash
pip install 'agentic-pr-dash[serve]'
```

Runtime prerequisites are:

- an authenticated GitHub CLI (`gh`);
- `git`;
- a checkout of the repository whose PRs should be managed;
- optional task tracker CLIs such as `bd` when configured;
- optional web dependencies for `agentic-pr-dash serve`.

Configuration resolution is centralized in `src/agentic_pr_dash/config.py`:

1. `AGENTIC_PR_DASH_*` environment variables;
2. repo-local `agentic-pr-dash.toml`, found by walking upward from the cwd;
3. global `~/.config/agentic-pr-dash/config.toml`;
4. project-agnostic defaults.

Legacy `GAIA_*` environment variables and `.gaia` state directories are honored
as fallbacks for existing Gaia installs, but new behavior should use the
`AGENTIC_PR_DASH_*` namespace and package config.

## Design Boundaries

The package owns generic PR maintenance:

- GitHub PR lookup, CI/check status, review-thread lookup, and completion replies;
- branch-to-PR resolution and local changed-file/new-commit inspection;
- ownership leases and session ledgers;
- maintenance state transitions;
- task tracker adapter interfaces;
- dashboard card construction and rendering;
- Codex/Claude hook helpers that are not tied to a specific application's build
  or production policy.

Downstream repos own project policy:

- executor command template and prompt wording;
- task tracker choice;
- state directory name if preserving existing markers;
- self-hosted runner label;
- session registry path;
- stop-gate waiter command;
- any build, test, production, proof, design-system, or deployment rules.

If a behavior mentions a specific app's Docker stack, database, auth setup,
proof harness, deployment target, issue labels, or local Make target, keep that
behavior in the app repo and invoke it through config or a local adapter.

## Runtime Flows

### CLI Dispatch

`src/agentic_pr_dash/cli.py` is the package entrypoint. It routes commands into
four runtime areas:

```text
agentic-pr-dash check|complete|arm|list-owned|stop-gate|reconcile-prs|await
  -> maintenance_check.main()

agentic-pr-dash record
  -> session_registry.main()

agentic-pr-dash loop
  -> loop.main()

agentic-pr-dash serve
  -> server.main()
```

Keep command registration here small and explicit. New subcommands should route
to a focused module rather than growing `cli.py`.

### Read-Only Blocker Detection

`maintenance_check.check` is the stop-gate and loop's read-only inspection path.
It:

1. resolves the current branch or explicit PR number;
2. lists open PRs through `github_api.list_open_prs()`;
3. fetches latest commit metadata, CI checks, mergeability, and unresolved
   review comments;
4. computes blockers;
5. prints a prompt and exits with a work-needed status when a fix is required.

The check path must remain read-only. Ownership markers are written by `arm`,
loop coordination, or explicit state commands, not by the inspection itself.

### Ownership and Lease Coordination

The central invariant is one agent per PR. The package enforces this with:

- per-worktree `pr-watch.armed` marker files;
- a heartbeat window for "this owner is alive";
- a longer fix lease for "this owner is actively fixing";
- pid liveness to reclaim crashed owners quickly;
- a durable session ledger under the user's home directory so owned PRs survive
  worktree deletion;
- optional coordinator claims used by the dashboard to suppress duplicate queue
  work while it hands a PR to a local agent.

Ownership checks should fail closed: if the package cannot prove a draft PR is
safe to arm or cannot resolve ownership state, it should avoid dispatching a
competing fix.

### Maintenance Loop

The loop repeatedly discovers PRs that need work, arms ownership, renders a fix
prompt, and dispatches that prompt to `Config.executor`.

The executor is a shell command template. It must include `{prompt}` where the
generated prompt should be inserted, for example:

```toml
executor = "codex exec --full-auto {prompt}"
```

The loop is deliberately generic. It does not know how the agent commits, tests,
or pushes; it only dispatches and then rechecks PR state.

### Dashboard Orchestrator

`src/agentic_pr_dash/orchestrator.py` owns the dashboard state machine. It polls
GitHub, enriches PRs, maps PR branches to worktrees, reads session state, computes
display status, and queues maintenance. It does not directly run the fix agent as
a hidden subprocess; maintenance is handed off to the local configured agent and
tracked through coordinator/session state.

The orchestrator treats GitHub API failures as "skip this poll" rather than "no
PRs exist." This prevents transient rate limits or outages from incorrectly
clearing the board.

### Web Server

`server.py` starts the web runtime. `app.py` defines the FastAPI routes and HTMX
fragments. The UI renders data from `PRData`, `WorktreeCard`, and related models
rather than reaching into GitHub directly from templates.

### Codex Hook Pack

`src/agentic_pr_dash/codex_hooks/` contains hook adapters that normalize Codex
or Claude hook payloads and call package functionality. These modules should
stay generic. Repo-specific policy can be invoked by configuration or by a local
wrapper script in the downstream repo.

## Code Map

| Path | Responsibility |
| --- | --- |
| `src/agentic_pr_dash/cli.py` | Top-level command router. |
| `src/agentic_pr_dash/config.py` | Config discovery, env fallback, resolved config dataclass, derived paths. |
| `src/agentic_pr_dash/models.py` | Pydantic models for PRs, cards, checks, maintenance state, sessions, events. |
| `src/agentic_pr_dash/github_api.py` | GitHub CLI/API access, PR metadata, CI checks, review threads, runner health. |
| `src/agentic_pr_dash/maintenance_check.py` | Stateless executor commands, stop-gate logic, arm/list/reconcile/complete flows. |
| `src/agentic_pr_dash/maintenance.py` | Durable maintenance queue/state helpers. |
| `src/agentic_pr_dash/loop.py` | Continuous check/fix/complete loop and executor dispatch. |
| `src/agentic_pr_dash/orchestrator.py` | Dashboard polling, PR state machine, queue suppression, card enrichment. |
| `src/agentic_pr_dash/app.py` | FastAPI routes and dashboard actions. |
| `src/agentic_pr_dash/server.py` | Dashboard server startup. |
| `src/agentic_pr_dash/worktrees.py` | Worktree discovery and branch-to-worktree mapping. |
| `src/agentic_pr_dash/agents.py` | Local agent process discovery. |
| `src/agentic_pr_dash/coordinator.py` | Dashboard/agent coordination claims. |
| `src/agentic_pr_dash/session_registry.py` | Session event log ingestion and summary. |
| `src/agentic_pr_dash/session_ledger.py` | Durable session-owned PR ledger. |
| `src/agentic_pr_dash/tracker.py` | Task tracker adapters. |
| `src/agentic_pr_dash/ci_watch.py` | Post-push CI watcher and results snapshots. |
| `src/agentic_pr_dash/codex_hooks/` | Hook adapters and shared hook runtime helpers. |

## Data Model

The key models are in `models.py`:

- `PRData` is the canonical enriched PR record used by the orchestrator and
  executor paths.
- `MaintenanceState` tracks queued/running/waiting/complete/failed maintenance
  for a PR.
- `WorktreeCard` is the dashboard projection. Its `agent_state` property derives
  the visible card state in priority order.
- `CICheck`, `QueuedWorkflowJob`, `RunnerPoolHealth`, and
  `RunnerExecutionSummary` describe CI and runner state.
- `ReviewComment` represents unresolved inline or review-level feedback.
- `AgentProcess` and session models describe local agent activity.

Keep GitHub response parsing in `github_api.py`. Convert external data into
models before handing it to the dashboard or maintenance flows.

## State Files

State is local and file-based:

- `<state_dir>/pr-watch.armed` marks a worktree's current PR owner;
- `<state_dir>/pr-watch.session` records the owning session;
- `<state_dir>/pr-maintenance/` stores maintenance state;
- the durable ledger under the user's home directory records PRs owned by a
  session even if the worktree disappears;
- the session registry JSONL records lifecycle events from agent launchers;
- CI watch results and pidfiles are written to configured paths.

Use `Config.state_dir_for(cwd)`, `watch_marker_for(cwd)`, and related helpers
when operating on sibling worktrees. A common bug class is accidentally reading
the config root's state directory instead of the target worktree's state.

## Gaia Integration

Gaia consumes this package as the upstream implementation and keeps its local
policy in `agentic-pr-dash.toml`, `pr_dashboard` compatibility shims, and
repo-specific hook wrappers.

Gaia-specific configuration includes:

- `state_dir = ".gaia"` to preserve existing markers;
- `tracker = "beads"`;
- `runner_label = "gaia-ci-desktop"`;
- `executor = "codex exec --full-auto {prompt}"`;
- `await_command` routed through Gaia's `scripts/pr-cli.sh`;
- `maintenance_loop_machine_wide = true` for Gaia's detached loop;
- `session_registry_path = "~/.gaia/sessions/events.jsonl"`.

The package should not import Gaia modules or hardcode Gaia paths. Gaia may
delegate to package modules from shims, but ownership points from Gaia to this
package, not the other way around.

## Testing and Maintenance

Run the package tests from the repo root:

```bash
pytest
```

Useful focused checks:

```bash
pytest tests/test_cli_dispatch.py tests/test_config.py
pytest tests/test_maintenance.py tests/test_stop_gate_scope.py
pytest tests/test_orchestrator_ownership.py
pytest tests/test_post_push_watch.py tests/test_agent_coordination_hooks.py
```

When changing GitHub parsing, prefer tests that stub command/API output at the
`github_api.py` boundary. When changing ownership, include stale owner, live
foreign owner, dead pid, detached ledger, and same-session cases. When changing
dashboard state, assert the derived `WorktreeCard.agent_state` rather than only
raw fields.

Before shipping config or hook changes, verify both package defaults and a
downstream config such as Gaia's `agentic-pr-dash.toml`.
