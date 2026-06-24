# PR Dashboard Maintenance Handoff

PR: [PR #50](https://github.com/Boundless-Studios/gaia-free/pull/50)
Branch: `bou-1789-ci-terminal`
Bead: `not-created`
State: `queued`
Blockers: merge_conflict, review_comments

## Resume Instructions
Handle this PR's maintenance: resolve review comments, CI failures, and merge conflicts before pushing.

## Dashboard Prompt
[PR #50](https://github.com/Boundless-Studios/agentic-pr-dash/pull/50) (bou-1789-ci-terminal) needs PR maintenance.

Use the project feature-pipeline conventions for PR maintenance, but do not start a new feature pipeline.
This is delegated focused work on an existing PR.
Do NOT create a new branch or PR. Commit and push to the existing branch.

## Merge Conflicts
This PR has merge conflicts against `main`.
Fetch `origin/main`, merge it into the current branch, resolve conflicts, test, commit, and push.
Do not use `git reset --hard` and do not discard unrelated local changes.

## Review Comments
Address each review comment below, commit, and push.
After pushing, run `agentic-pr-dash complete` to post completion replies and resolve the threads.

### Comment 3463176487 by @chatgpt-codex-connector on `src/agentic_pr_dash/github_api.py:995`
**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)</sub></sub>  Accept gh's pending exit code here**

When required checks are actually pending, `gh pr checks` exits non-zero: the GitHub CLI manual for `gh pr checks` documents additional exit code 8 for checks pending ([docs](https://cli.github.com/manual/gh_pr_checks)). In that scenario this returns `False` before parsing the JSON, so `ci_watch_pending` is never set and `await --max-wait` can exit while CI is still running instead of waking on a later failure; treat rc 8 like success when stdout is parseable.

Useful? React with 👍 / 👎.

### Comment 3463176490 by @chatgpt-codex-connector on `src/agentic_pr_dash/loop.py:91`
**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Namespace failure streaks by repository**

In multi-repo configurations (`maintenance_repo_roots`), all loops share the same daemon health file by default but this key is only the PR number. If repo A's PR #42 reaches the threshold, repo B's unrelated PR #42 reads the same streak via `executor_failure_streak(cwd, 42)`, so `_loop_covers_pr` stops counting the live loop and the stop-gate/dashboard can escalate the wrong PR. Include the repo/cwd in the key.

Useful? React with 👍 / 👎.

### Comment 3463176502 by @chatgpt-codex-connector on `src/agentic_pr_dash/loop.py:510`
**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Clear escalations when PRs recover externally**

After a PR reaches the escalation threshold, the path that clears the health/escalation marker is only this executor-success path. If the user follows the escalation prompt and fixes the PR manually (or CI/comments are resolved externally), the next loop's `check` returns 0 and the tick continues before `reset_executor_failure` runs, leaving the marker in place and causing the stop-gate/dashboard to keep treating the now-clean open PR as escalated until another executor dispatch happens. Clear the streak/marker on a clean result for the marked PR as well.

Useful? React with 👍 / 👎.

### Comment 3463176510 by @chatgpt-codex-connector on `src/agentic_pr_dash/loop.py:196`
**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow?style=flat)</sub></sub>  Handle non-object escalation markers**

If an existing escalation marker contains valid JSON that is not an object (for example `[]` from a partial manual edit or bad previous write), `_json.loads` succeeds here and the subsequent string-key assignment raises `TypeError`, which is not caught by this `except OSError` block. That aborts the loop exactly when a PR hits the escalation threshold, before the coordinator claim is released; treat non-dict marker contents as empty like the reader does.

Useful? React with 👍 / 👎.

