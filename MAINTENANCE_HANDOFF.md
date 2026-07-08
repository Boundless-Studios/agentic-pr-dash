# PR Dashboard Maintenance Handoff

PR: [PR #62](https://github.com/Boundless-Studios/gaia-free/pull/62)
Branch: `bou-1877-1905-waiter-resilience`
Bead: `not-created`
State: `queued`
Blockers: review_comments

## Resume Instructions
Handle this PR's maintenance: resolve review comments, CI failures, and merge conflicts before pushing.

## Dashboard Prompt
[PR #62](https://github.com/Boundless-Studios/agentic-pr-dash/pull/62) (bou-1877-1905-waiter-resilience) needs PR maintenance.

Use the project feature-pipeline conventions for PR maintenance, but do not start a new feature pipeline.
This is delegated focused work on an existing PR.
Do NOT create a new branch or PR. Commit and push to the existing branch.

## Review Comments
Address each review comment below, commit, and push.
After pushing, run `agentic-pr-dash complete` to post completion replies and resolve the threads.

### Comment 3547352541 by @chatgpt-codex-connector on `src/agentic_pr_dash/config.py:264`
**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Keep repo autodetect from reusing the vanished cwd**

When the ambient cwd has been deleted and no repo is pinned in env/TOML, this fallback lets `load()` return a `Config`, but callers that then do `load_config().resolved_repo()` still hit `Config.resolved_repo()`'s `Path.cwd()` call and raise the same `FileNotFoundError`. Ambient callers such as `coordinator._repo_slug_for_pr` and `maintenance.pr_url(cwd=None)` use that pattern, so the deleted-cwd scenario can still crash after config load succeeds; pass the safe fallback base through repo detection or make `resolved_repo()` handle the same OSError.

Useful? React with 👍 / 👎.

