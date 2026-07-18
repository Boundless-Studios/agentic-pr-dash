# PR Dashboard Maintenance Handoff

PR: [PR #82](https://github.com/Boundless-Studios/gaia-free/pull/82)
Branch: `bou-2184-handoff-wedge-and-adoption`
Bead: `not-created`
State: `queued`
Blockers: review_comments

## Resume Instructions
Handle this PR's maintenance: resolve review comments, CI failures, and merge conflicts before pushing.

## Dashboard Prompt
[PR #82](https://github.com/Boundless-Studios/agentic-pr-dash/pull/82) (bou-2184-handoff-wedge-and-adoption) needs PR maintenance.

Use the project feature-pipeline conventions for PR maintenance, but do not start a new feature pipeline.
This is delegated focused work on an existing PR.
Do NOT create a new branch or PR. Commit and push to the existing branch.

## Review Comments
Address each review comment below, commit, and push.
After pushing, run `agentic-pr-dash complete` to post completion replies and resolve the threads.

### Comment 3608944192 by @chatgpt-codex-connector on `src/agentic_pr_dash/coordinator.py:94`
**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Preserve dirty renames into the state directory**

When a reclaimable worktree has a staged rename from a real tracked source file into `.agentic-pr-dash/` (or into the legacy handoff filename), `git status --porcelain` emits `R  old -> new`; this selects only `new`, so the following artifact filter treats the entire rename as loop-generated dirt. The coordinator can then reclaim and dispatch work over a worktree where an agent has actually staged a source-file removal/rename, rather than returning `manual_intervention`. Only ignore a rename when both sides are loop artifacts (or otherwise retain the source path when deciding dirtiness).

Useful? React with 👍 / 👎.

