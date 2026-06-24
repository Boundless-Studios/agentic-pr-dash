# PR Dashboard Maintenance Handoff

PR: [PR #50](https://github.com/Boundless-Studios/gaia-free/pull/50)
Branch: `bou-1789-ci-terminal`
Bead: `not-created`
State: `queued`
Blockers: review_comments

## Resume Instructions
Handle this PR's maintenance: resolve review comments, CI failures, and merge conflicts before pushing.

## Dashboard Prompt
[PR #50](https://github.com/Boundless-Studios/agentic-pr-dash/pull/50) (bou-1789-ci-terminal) needs PR maintenance.

Use the project feature-pipeline conventions for PR maintenance, but do not start a new feature pipeline.
This is delegated focused work on an existing PR.
Do NOT create a new branch or PR. Commit and push to the existing branch.

## Review Comments
Address each review comment below, commit, and push.
After pushing, run `agentic-pr-dash complete` to post completion replies and resolve the threads.

### Comment 3464501399 by @chatgpt-codex-connector on `src/agentic_pr_dash/maintenance_check.py:479`
**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Include detached PRs in await CI watch**

When a session owns an open PR only through the ledger/no-worktree path, the stop-gate can still spawn this waiter via `detached_prs`, but after `--max-wait` expires this line only checks the live `owned` worktree list. If that detached PR has required CI still running and no current blockers, `owned` may be empty, so the waiter exits 0 and a later CI failure will not wake the session. Include the open `_detached_this_tick` PRs in the watch-pending check as well.

Useful? React with 👍 / 👎.

### Comment 3464501403 by @chatgpt-codex-connector on `src/agentic_pr_dash/maintenance_check.py:479`
**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Don't treat terminal failed checks as no feedback**

Here `watch_pending == False` is treated as no feedback, but it also covers the transition this waiter is supposed to catch: a required check stopped being pending because it failed. I checked the GitHub CLI docs: [`gh pr checks --json`](https://cli.github.com/manual/gh_pr_checks) exposes `bucket` values including `fail` and `pending`, and the exit-code docs say failed commands use a non-zero code; `github_api.get_ci_checks()` still returns `[]` on any non-zero rc, so when a pending required check flips to fail `_check_worktree()` can report clean, `_collect_await_watch_pending()` returns false, and this branch exits 0 instead of waking the session with exit 10. Parse the check JSON on failing rc just like `required_checks_pending()` before allowing this return.

Useful? React with 👍 / 👎.

### Comment 3464544626 by @chatgpt-codex-connector on `src/agentic_pr_dash/github_api.py:999`
**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Paginate the required-check rollup**

For PRs with more than 100 rollup contexts, this query only inspects the first page and ignores `pageInfo`/`after`. If a required `EXPECTED`/`PENDING` context is on a later page, `required_checks_pending()` returns `False`, allowing the stop-gate/await waiter to release while required CI is still running; fetch all pages or otherwise limit the query to required contexts.

Useful? React with 👍 / 👎.

### Comment 3464544631 by @chatgpt-codex-connector on `src/agentic_pr_dash/loop.py:112`
**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Handle corrupt health entries**

If the repo-scoped health JSON is a dict but a PR entry is not an object, or its `streak` is not parseable as an int, this raises while checking loop coverage or recording the next failure. In the stop-gate path that exception is swallowed by `_cmd_stop_gate` and releases the session with no waiter even though the PR may already be at the escalation threshold; coerce malformed per-PR entries to an empty/default streak like the top-level health reader does.

Useful? React with 👍 / 👎.

### Comment 3464544633 by @chatgpt-codex-connector on `src/agentic_pr_dash/templates/dashboard.html:33`
**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Keep the escalation banner live**

When an escalation is recorded after the dashboard has already loaded, this banner is outside the `hx-get="/partials/board"` section, and the `/partials/board` route renders only `partials/board.html`. The card badge can update every 5s, but the promised title-bar banner stays absent until a full page reload; put the banner in a refreshed partial or update it alongside the board swap.

Useful? React with 👍 / 👎.

### Comment 3464544638 by @chatgpt-codex-connector on `src/agentic_pr_dash/config.py:326`
**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Validate escalation threshold values**

If `AGENTIC_PR_DASH_ESCALATION_THRESHOLD` or the TOML value is mistyped, `load()` now raises during config resolution; if it is set to `0` or a negative number, `_loop_covers_pr()` treats every live loop as not covering while `_maybe_escalate()` never fires on the first failure. Clamp to a positive integer and fall back to the default on invalid input so one bad config value does not disable the loop/stop-gate flow.

Useful? React with 👍 / 👎.

### Comment 3464590663 by @chatgpt-codex-connector on `src/agentic_pr_dash/_maintenance/stop_gate.py:167`
**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Keep repo identity through coverage checks**

Fresh evidence after the repo-scoped health files change: this dict and the `_owned_open_pr_numbers` set still key owned worktrees by the bare PR number. In a multi-repo session that owns repo A PR #42 and repo B PR #42, the later pair overwrites the earlier one, so if the overwritten repo is the one whose health file reached the escalation threshold, `_loop_covers_pr` is checked against the other repo and the stop-gate can treat the failed PR as covered and release without a waiter. Keep `(worktree, pr)` pairs through the coverage/escalation checks instead of collapsing to `int` PR numbers.

Useful? React with 👍 / 👎.

### Comment 3464590670 by @chatgpt-codex-connector on `src/agentic_pr_dash/github_api.py:1018`
**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Resolve required checks from the cwd repo**

When multi-repo maintenance runs with a shared/global config or `AGENTIC_PR_DASH_REPO` pinning `repo` to the anchor repository, `resolved_repo()` ignores the `cwd` repository and this GraphQL query asks for the PR number in the pinned repo. The rest of the PR state in this path is collected from the worktree's `cwd`, so a sibling repo's required check can be missed or confused with an anchor repo PR of the same number, letting the await waiter exit while that sibling CI is still pending. Derive owner/name from the current `cwd` repo for this query.

Useful? React with 👍 / 👎.

