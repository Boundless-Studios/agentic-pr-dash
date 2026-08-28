# Open PR Review Watch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every green open PR under durable, observable review monitoring with a sparse backoff schedule until merge or closure.

**Architecture:** Add a small immutable scheduling policy and persisted watch state to the lifecycle model/store. The lifecycle workflow evaluates due watches using its existing GitHub observation and maintenance queue, while stop hooks only verify that ownership is armed. Dashboard/checklist projections expose the state without adding polling or network calls.

**Tech Stack:** Python 3.11+, Pydantic lifecycle models, pytest with fake clocks, existing lifecycle workflow/store and maintenance queue.

---

### Task 1: Define the schedule and persisted state

**Files:**
- Modify: `src/agentic_pr_dash/lifecycle_models.py`
- Test: `tests/test_lifecycle_models.py`

- [ ] **Step 1: Write failing model tests**

Add tests asserting that the default intervals are `(60, 300, 900, 1800, 3600, 7200, 14400, 28800)`, that advancing the final index repeats 28800 seconds, and that serialized watch state preserves head, timestamps, index, unresolved count, and reset reason.

- [ ] **Step 2: Verify RED**

Run: `uv run --extra dev pytest tests/test_lifecycle_models.py -k review_watch -q`

Expected: failure because the review-watch types and schedule do not exist.

- [ ] **Step 3: Implement the model contract**

Add `REVIEW_WATCH_INTERVAL_SECONDS`, a pure `review_watch_delay(index: int) -> int`, a `ReviewWatchStatusV1` enum (`armed`, `due`, `paused`), and `ReviewWatchStateV1` fields matching the design. Add the optional watch field to the persisted lifecycle snapshot with backward-compatible absence on old snapshots.

- [ ] **Step 4: Verify GREEN**

Run: `uv run --extra dev pytest tests/test_lifecycle_models.py -k review_watch -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

Run: `git add src/agentic_pr_dash/lifecycle_models.py tests/test_lifecycle_models.py && git commit -m "Add durable review watch state"`

### Task 2: Drive the watch from lifecycle observations

**Files:**
- Modify: `src/agentic_pr_dash/lifecycle_workflow.py`
- Test: `tests/test_lifecycle_workflow.py`

- [ ] **Step 1: Write failing transition tests**

Add fake-clock tests covering initial arm at green CI, exact due offsets, successful clean advancement, repeating 480-minute tail, head-change reset, actionable-feedback reset plus maintenance dispatch, failed-observation non-advance, red-CI pause, green-CI resume, and merged/closed removal.

- [ ] **Step 2: Verify RED**

Run: `uv run --extra dev pytest tests/test_lifecycle_workflow.py -k review_watch -q`

Expected: failures because lifecycle observations do not manage the watch.

- [ ] **Step 3: Implement pure transition logic**

Add a focused `_advance_review_watch(...)` helper that receives the current state, current time, head, CI/review observability, actionable count, and PR-open state. Return the next state plus whether maintenance must be queued; keep GitHub reads and sleeping outside this helper.

- [ ] **Step 4: Wire the transition into the existing drain**

Evaluate only when the watch is due or an event invalidates it. Persist through the existing lifecycle store and use the existing deduplicated maintenance dispatch path for actionable feedback.

- [ ] **Step 5: Verify GREEN**

Run: `uv run --extra dev pytest tests/test_lifecycle_workflow.py -k review_watch -q`

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

Run: `git add src/agentic_pr_dash/lifecycle_workflow.py tests/test_lifecycle_workflow.py && git commit -m "Watch open PR reviews with durable backoff"`

### Task 3: Make ownership and timing observable

**Files:**
- Modify: `src/agentic_pr_dash/delivery_checklist.py`
- Modify: `src/agentic_pr_dash/stop_hook.py`
- Modify: `src/agentic_pr_dash/app.py`
- Test: `tests/test_delivery_checklist.py`
- Test: `tests/test_stop_hook.py`
- Test: `tests/test_lifecycle_dashboard.py`

- [ ] **Step 1: Write failing projection tests**

Assert that a clean open PR without an armed watch remains incomplete, an armed
watch permits session completion, and checklist/dashboard output includes status,
next check, schedule position, last observation, and reset reason.

- [ ] **Step 2: Verify RED**

Run: `uv run --extra dev pytest tests/test_delivery_checklist.py tests/test_stop_hook.py tests/test_lifecycle_dashboard.py -k review_watch -q`

Expected: failures because the ownership requirement and projection are absent.

- [ ] **Step 3: Implement thin projections**

Project the persisted state without performing GitHub reads. Stop-hook evaluation blocks only when an otherwise-completable open PR lacks durable watch ownership; it never waits for a future interval.

- [ ] **Step 4: Verify GREEN and regression suite**

Run: `uv run --extra dev pytest tests/test_delivery_checklist.py tests/test_stop_hook.py tests/test_lifecycle_dashboard.py tests/test_lifecycle_workflow.py tests/test_lifecycle_models.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

Run: `git add src/agentic_pr_dash/delivery_checklist.py src/agentic_pr_dash/stop_hook.py src/agentic_pr_dash/app.py tests/test_delivery_checklist.py tests/test_stop_hook.py tests/test_lifecycle_dashboard.py && git commit -m "Expose durable review watch ownership"`

### Task 4: Document policy and verify delivery

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `README.md`

- [ ] **Step 1: Document lifecycle semantics**

Document the schedule, repeating eight-hour tail, reset triggers, observation-failure behavior, distinction between session completion and PR closure, and the rule that render/stop paths never poll or sleep.

- [ ] **Step 2: Run the focused and full suites**

Run: `uv run --extra dev pytest tests/test_lifecycle_models.py tests/test_lifecycle_workflow.py tests/test_delivery_checklist.py tests/test_stop_hook.py tests/test_lifecycle_dashboard.py -q`

Run: `uv run --extra dev pytest -q`

Expected: both commands pass.

- [ ] **Step 3: Commit and push**

Run: `git add docs/ARCHITECTURE.md README.md && git commit -m "Document open PR review watch policy"`

Run: `git push origin HEAD:refs/heads/fix/merged-clean-lane`

- [ ] **Step 4: Settle the PR**

Reply to every actionable review thread in place, resolve addressed threads, wait for CI, and perform at least one additional review-thread observation after CI becomes green. Completion requires green CI and zero unresolved actionable threads; the durable watch then owns later feedback until merge or closure.
