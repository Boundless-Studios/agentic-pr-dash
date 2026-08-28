# agentic-pr-dash Architecture

This document explains `agentic-pr-dash` from the outside in. Start here if you
need a mental model before reading the Python modules.

## ELI5 Mental Model

Imagine your GitHub PRs are patients in a waiting room.

Each PR might be healthy, waiting on CI, failing tests, blocked by review
comments, or already being treated by an agent. `agentic-pr-dash` is the nurse's
station:

- it checks each PR's chart;
- it decides which PRs need attention;
- it makes sure only one agent works on a PR at a time;
- it records who is working;
- it shows the whole room on a dashboard;
- it can hand a fix prompt to whatever agent command the project configured.

It is not the doctor. It does not know how to fix Gaia, how to run Gaia tests, or
how to satisfy a specific reviewer. It gathers the evidence, writes the work
order, gives that work order to an agent, and later checks whether the PR became
healthy.

The simplest responsibility split is:

```text
agentic-pr-dash:
  "What is blocking this PR, who owns the fix, and what prompt should be handed
   to an agent?"

the project using it:
  "Which agent command should run, which task tracker should record the work,
   which CI runner pool matters, and which local policies must be enforced?"
```

For example, Gaia is a downstream application repo that uses
`agentic-pr-dash`. In Gaia, `agentic-pr-dash` owns generic PR maintenance and
coordination. Gaia owns beads policy, proof gates, production-command blocking,
test-wrapper rules, Docker/database details, and the exact Codex command used to
fix a PR.

## One-Sentence Summary

`agentic-pr-dash` watches GitHub PRs, classifies blockers, coordinates ownership
so agents do not collide, and exposes the result through CLI commands, a loop,
hooks, and a dashboard.

## What It Owns

`agentic-pr-dash` owns generic PR maintenance:

- resolve the current branch or explicit number to a GitHub PR;
- fetch CI, review-comment, mergeability, and latest-commit state;
- decide whether a PR is clean, pending, failing, conflicted, or awaiting review
  fixes;
- create and read ownership markers so two agents do not fix the same PR;
- keep a durable ledger so PR responsibility can survive worktree deletion;
- build fix prompts from the live blockers;
- dispatch those prompts through a configured executor;
- record session activity for dashboard visibility;
- render PR and runner state in the web dashboard;
- provide generic Codex/Claude hook helpers.

## What It Does Not Own

`agentic-pr-dash` does not own project policy:

- no Gaia test command policy;
- no Gaia proof-bundle policy;
- no Gaia production-command blocking;
- no Gaia Docker or database details;
- no hardcoded issue labels or runner names;
- no assumption that Codex, Claude, beads, or any one tracker is mandatory.

Those choices belong in the consuming repo's config or local hook wrappers.

## How To Read The Code

Read the code as four cooperating layers:

1. `cli.py` chooses which subsystem should handle a command.
2. `github_api.py`, `maintenance_check.py` (a thin CLI over the `_maintenance/`
   behavior package), and `models.py` answer: "what is true about this PR right
   now?"
3. `loop.py`, `coordinator.py`, `session_ledger.py`, and `session_registry.py`
   answer: "who is allowed to work on it?"
4. `orchestrator.py`, `app.py`, and `server.py` answer: "what should humans see
   on the dashboard?"

Most confusing behavior becomes easier if you ask two questions:

- Is this about generic PR state and ownership? If yes, it probably belongs
  here.
- Is this about one project's local safety rules or build/test/proof workflow?
  If yes, it belongs in that project and should enter through config.

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

## Responsibility Boundaries

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

The rule of thumb: the package should understand PRs and ownership, not one
repo's local rituals.

For example, `agentic-pr-dash` can provide an `executor` template. Gaia can set
that template to `codex exec --full-auto {prompt}`. The package should not
hardcode Codex, Gaia's `bd` labels, Gaia's proof bundle, or Gaia's test wrapper.

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

In plain terms: every command starts at the same front door, then immediately
gets sent to the smaller part of the package that owns that job.

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

Think of `check` as reading the PR's chart. It should not claim ownership, close
tasks, or mutate local state just because it looked.

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

Green open PRs remain under a durable late-review watch. Successful review
observations follow `1m, 5m, 15m, 30m, 1h, 2h, 4h, 8h`, then repeat every eight
hours until merge or closure. New heads and actionable feedback reset the watch
to one minute; failed observations do not advance it. The lifecycle worker owns
this schedule—rendering and Stop hooks only read or rearm persisted state and
never sleep or query GitHub themselves.

### Web Server

`server.py` starts the web runtime. `app.py` defines the FastAPI routes and HTMX
fragments. The UI renders data from `PRData`, `WorktreeCard`, and related models
rather than reaching into GitHub directly from templates.

### Codex Hook Pack

`src/agentic_pr_dash/codex_hooks/` contains hook adapters that normalize Codex
or Claude hook payloads and call package functionality. These modules should
stay generic. Repo-specific policy can be invoked by configuration or by a local
wrapper script in the downstream repo.

The hook entrypoints stay small and declarative: pure shell-command parsing
(segment splitting, `cd`/`gh pr` target/`git push` detection, effective-git-cwd
resolution) lives in `command_parser.py`. `run_pr_convergence.py` normalizes
Claude, Codex, and Pi payloads, resolves local Git identity, and writes only
durable lifecycle intents; it never observes GitHub or starts a worker.

## Code Map

| Path | Responsibility |
| --- | --- |
| `src/agentic_pr_dash/cli.py` | Top-level command router. |
| `src/agentic_pr_dash/config.py` | Config discovery, env fallback, resolved config dataclass, derived paths. |
| `src/agentic_pr_dash/models.py` | Pydantic models for PRs, cards, checks, maintenance state, sessions, events. |
| `src/agentic_pr_dash/github_api.py` | GitHub CLI/API access, PR metadata, CI checks, review threads, runner health. |
| `src/agentic_pr_dash/maintenance_check.py` | Thin CLI layer: `main`, the `_cmd_*` subcommand dispatchers, arg parsing, and a re-export facade over `_maintenance/` (so `maintenance_check.X` keeps resolving for consumers and tests). |
| `src/agentic_pr_dash/_maintenance/` | Behavior package the CLI delegates to (split out of `maintenance_check.py`): `pr_state.py` (PR resolution + GitHub reads + review threads), `markers.py` (ownership/session markers, heartbeats, leases, claims), `worktrees.py` (worktree iteration + maintenance-root resolution), `worktree_check.py` (the shared per-worktree blocker engine used by both `check` and `stop-gate`), `stop_gate.py` (stop-state, fingerprinting, prompt/waiter rendering), `completion.py` (completion replies + review-comment extraction), `reconcile.py` (orphan adoption + PR records), `waiter.py` (await pidfiles + liveness), `_common.py` (shared primitives). Cross-module calls are module-qualified so the owning module is the single monkeypatch seam. |
| `src/agentic_pr_dash/stop_hook.py` | Snapshot-only advisory Stop adapter. It reads the current exact-head lifecycle snapshot, enqueues stale/missing/invalid or unarmed clean state, renders blockers/actions/review-watch ownership, and always allows Stop even when state is unavailable. |
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
| `src/agentic_pr_dash/ci_watch.py` | Thin module kept for the `python -m agentic_pr_dash.ci_watch` background-watcher entrypoint: `arm_post_push_watch`, `spawn_background_watcher`, `main`, plus a re-export facade over `_ci_watch/`. |
| `src/agentic_pr_dash/_ci_watch/` | CI-watch behavior package: `config.py` (constants + `CIWatchConfig`/`from_env` + `eprint`), `repo.py` (git/PR helpers), `checks.py` (check snapshot/classification), `adapter.py` (status/complete adapter rendering+invocation), `results.py` (results-file serialization), `watcher.py` (background poll lifecycle). |
| `src/agentic_pr_dash/codex_hooks/` | Hook adapters and shared runtime helpers, including `run_pr_convergence.py` (unified local lifecycle adapter) and `command_parser.py` (pure segment, `cd`, `gh pr`, `git push`, and effective-cwd parsing). |

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

### Card State Precedence

**Liveness is not work.** A session process being alive, or a heartbeat still
arriving, never by itself means the agent is doing something. The board keeps
three separate activity states so a merged PR whose chat window is still open
does not sit in `Agent Working` forever (BOU-2365).

`app._resolve_agent_activity` produces a tri-state used by `app._card_status`:

| activity | Meaning |
|---|---|
| `working` | Real work: an open turn/tool/subagent/critical section, `quiescence == busy`, or rotation machinery mid-flight (`checkpointing`, `checkpointed`, `fencing`, `fenced`, `claiming`, `launching`, `awaiting_ack`). |
| `waiting` | A live session that is not working: `quiescence == idle`, a wind-down or blocked supervisor phase (`draining`, `stopping`, `stopped`, `blocked`), or a bare live process with no activity signal at all. |
| `none` | No session. |

Sessions with no activity hook that genuinely *are* working still read as
`working`, because the loop sets `MaintenanceStatus.RUNNING` when it dispatches
an executor and `_card_status` treats an active maintenance state as work.

**Column and chip are decoupled.** The card's `status` (which column it lands
in) answers *what does this PR need*; `session_activity` + `agent_state` (the
state chip) answer *what is the session doing*. A clean PR with an idle session
stays in `Clean` — so the bug-bash "ready to merge" count keeps working — while
its chip reads `Waiting · user input`. Only a worktree with **no** PR is routed
to a column by activity alone: that is the `Waiting` column, and it is exactly
the bucket that used to be mislabelled `Agent Working`.

`WorktreeCard.agent_state` resolves in this order:

```
failed > ready_cleanup > queued > working > awaiting_fixes
       > ci_failing > merge_conflict > waiting > ci_pending > no_pr > clean
```

Two rules carry most of the weight:

- `ready_cleanup` outranks `waiting` but **not** `working`. A merged/closed
  branch with a reclaimable worktree is terminal even if the conversation is
  still alive; an agent genuinely mid-turn on a stale branch is not.
- `waiting` never masks an actionable PR. When the PR is CI-failing, has review
  comments, or conflicts, the card keeps that state — only the passive states
  (`ci_pending`, `no_pr`, `clean`) are overridden, with a `waiting_reason` of
  `user input`, `external checks`, or `winding down`.

Reclaimability for *display* is evaluated with an empty agent list so a
lingering process cannot hide a finished worktree; the `cleanup_candidate` flag
that arms the destructive cleanup button keeps its conservative "no agents
present" requirement.

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
