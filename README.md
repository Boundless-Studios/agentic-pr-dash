# pr-agent-ops

Watch your open GitHub PRs, detect what's blocking each one — failing CI,
unaddressed review comments, merge conflicts — and let an agent resolve them,
with a **one-agent-per-PR ownership lease** so two runners never fight over the
same PR.

It comes in three layers you can adopt independently:

| Layer | Command | What it does |
|-------|---------|--------------|
| **Executor** | `pr-agent-ops check` / `complete` / `arm` / `list-owned` | Stateless, read-only blocker detection + review-thread resolution. Shells out to `gh` + `git`; almost zero dependencies. |
| **Loop** | `pr-agent-ops loop` | Continuously checks your PRs and dispatches the fix to a **configurable agent** (Claude Code, Codex, aider, any prompt-taking CLI). |
| **Dashboard** | `pr-agent-ops serve` | A web view (FastAPI + HTMX) of every open PR's status, queued maintenance, and — optionally — your self-hosted CI runner fleet. |

It is **project-agnostic**: the GitHub repo, on-disk state directory, task
tracker, agent executor, and CI runner label are all configuration, not
hardcoded.

## Install

```bash
pip install pr-agent-ops            # executor + loop
pip install 'pr-agent-ops[serve]'   # + the web dashboard
```

Requires the [GitHub CLI](https://cli.github.com/) (`gh`) authenticated, and `git`.

## Quick start

```bash
# In a checkout with an open PR on the current branch:
pr-agent-ops check            # exit 10 + a fix prompt on stdout if work is needed

# Run the loop, dispatching fixes to your agent of choice:
PR_AGENT_OPS_EXECUTOR='claude --dangerously-skip-permissions -p {prompt}' \
  pr-agent-ops loop --once

# Or the dashboard:
pr-agent-ops serve            # http://127.0.0.1:9000
```

## Configuration

Drop a `pr-agent-ops.toml` at your repo root (see `pr-agent-ops.example.toml`).
Everything is optional and has a project-agnostic default; any value can be
overridden with a `PR_AGENT_OPS_*` environment variable.

```toml
[project]
# repo = "owner/name"          # auto-detected from the git remote if omitted
state_dir = ".pr-agent-ops"     # where ownership markers + maintenance state live
tracker = "none"                # "none" | "beads" | "github-issues"
executor = "claude --dangerously-skip-permissions -p {prompt}"
discovery_names = ["claude", "codex"]   # process names treated as live agents
# runner_label = "my-ci-fleet"  # self-hosted runner label; omit to hide the runner panel
```

### Task tracker (optional)

When a PR needs work, the loop can open a tracked task and close it when the PR
is clean. This is **off by default** — the flow works purely off PR state.
Built-in adapters:

- `none` — track nothing (default).
- `github-issues` — open/close a GitHub Issue (de-duped by a hidden marker).
- `beads` — the [`bd`](https://github.com/steveyegge/beads) CLI.

### Agent executor

`loop` dispatches the fix prompt to whatever CLI you configure. Use `{prompt}`
as the injection point:

```toml
executor = "claude --dangerously-skip-permissions -p {prompt}"
executor = "codex exec --full-auto {prompt}"
executor = "aider --message {prompt} --yes"
```

## Ownership lease

When more than one runner is active (e.g. an in-editor session **and** a
detached `loop`), an `.pr-agent-ops/pr-watch.armed` marker carries the owning
session id, pid, a short-lived **heartbeat**, and a longer **fix lease**. A
runner defers while another owner's heartbeat or fix lease is fresh, so a PR is
only ever worked by one agent at a time.

## License

MIT
