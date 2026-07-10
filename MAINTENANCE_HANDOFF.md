# PR Dashboard Maintenance Handoff

PR: [PR #66](https://github.com/Boundless-Studios/gaia-free/pull/66)
Branch: `bou-1923b-snapshot-pacing`
Bead: `not-created`
State: `queued`
Blockers: review_comments

## Resume Instructions
Handle this PR's maintenance: resolve review comments, CI failures, and merge conflicts before pushing.

## Dashboard Prompt
[PR #66](https://github.com/Boundless-Studios/agentic-pr-dash/pull/66) (bou-1923b-snapshot-pacing) needs PR maintenance.

Use the project feature-pipeline conventions for PR maintenance, but do not start a new feature pipeline.
This is delegated focused work on an existing PR.
Do NOT create a new branch or PR. Commit and push to the existing branch.

## Review Comments
Address each review comment below, commit, and push.
After pushing, run `agentic-pr-dash complete` to post completion replies and resolve the threads.

### Comment 3555885535 by @chatgpt-codex-connector on `src/agentic_pr_dash/_maintenance/pr_state.py:46`
**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Preserve gh-unavailable failures on cache hits**

When stop-gate/check calls this with a warm snapshot, we no longer execute the only call that used to turn a current gh/rate-limit outage into `_GH_UNAVAILABLE`. The subsequent detail calls (`get_latest_commit`, `get_ci_checks`, `get_unaddressed_comments`) all fail open to empty values, so a warm cache followed by GitHub becoming unavailable can make a blocked PR look clean and `_check_worktree` returns “nothing pending” instead of code 2. Please keep a live availability signal or propagate detail failures when serving cached PR lists.

Useful? React with 👍 / 👎.

### Comment 3555885539 by @chatgpt-codex-connector on `src/agentic_pr_dash/github_api.py:519`
**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Serialize snapshot misses before fetching**

On a cold or expired cache, each process reads the missing/stale file and immediately calls `list_open_prs` before any writer's `os.replace` is visible. In the Stop-hook/detached-loop/waiter burst this cache is meant to collapse, simultaneous processes still all execute `gh pr list`, so the first burst after expiry can hit the same quota/timeouts as before. Add a per-snapshot lock/recheck around the refresh path.

Useful? React with 👍 / 👎.

### Comment 3555885544 by @chatgpt-codex-connector on `src/agentic_pr_dash/github_api.py:1153`
**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Reset pacing after the Retry-After retry**

If the Retry-After branch succeeds, `_LAST_MUTATION_MONOTONIC` still points to the pre-sleep attempt, not to the retry here. A completion run that continues with the next thread/comment will see the interval as already elapsed and can fire the next mutation immediately after that retry, reintroducing the secondary-limit burst this pacing is supposed to prevent; update the timestamp after the retry or pace the retry attempt itself before returning.

Useful? React with 👍 / 👎.

