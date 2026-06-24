# Defer points must not hide a blocked owned PR behind a clean-looking no-op

**Tickets:** BOU-1785 (agent-coordinator claim hides unresolved review threads) +
BOU-1788 (live foreign owner hides red CI) — unified because both are silent
owner-defer points inside the same function, `_maintenance/worktree_check.py::_check_worktree`.

**Repo:** `agentic-pr-dash` (`~/code/pr-agent-ops`), branch
`bou-1785-defer-hides-blockers` off `origin/main`. Host-pytest TDD. No gaia code
change; gaia consumes via a reinstalled snapshot.

## Problem

`_check_worktree` is the shared engine for the `check` CLI (loop dispatch,
`claim=True`) and the `stop-gate` Stop hook (`claim=False`). It has **three**
points where it defers to an owner/claim and returns `(0, "<clean-looking
text>")`. In each, a deferral can occur while the owned PR has real blockers,
and the returned text is indistinguishable from a genuine clean no-op — so the
calling agent concludes "no actionable work."

Defer points in current `origin/main` `worktree_check.py`:

1. **Marker foreign-owner** (line 48-50): `markers._live_foreign_owner(cwd, sid)`
   returns a foreign owner → `return 0, "deferring to live PR-watch owner
   session {owner}"`. This returns **before** `_resolve_pr_for_branch`, so
   blockers are never computed. This is the **BOU-1788** path (red CI hidden).
2. **Live independent owner** (line 86-87): after blockers are computed, a
   process-scan finds a live independent owner → `return 0, "deferring to live
   independent owner of this worktree"`. Blockers are known but not surfaced.
3. **Agent-coordinator claim** (line 110-122): an active claim exists →
   `return 0, "deferring to agent-coordinator {state}: {reason}"` (and the
   race-lost variant `"deferring to active agent-coordinator claim"`). Blockers
   are known. This is the **BOU-1785** path (unresolved review threads hidden).

### Why the existing `new_feedback` mitigation is insufficient (BOU-1785)

`origin/main` already bypasses the claim-defer when `new_feedback` is true
(lines 104-111): it compares the active claim's fingerprint to the live PR
fingerprint and services the PR only when they **differ** (i.e. *additional*
feedback arrived after the claim). The BOU-1785 repro slips through because the
active claim was made **for the same still-unresolved review comments** — the
live fingerprint equals the claimed fingerprint, so `new_feedback=False` and the
check defers for the whole lease window. The most common shape is a session
deferring to its **own** active claim: once the loop/check claims PR #N under the
session's pid, `agent_coordinator.service.status()` reports `ACTIVE`/not-
reclaimable as long as that pid is live and the lease is unexpired, and
`dispatch_decision_for_pr` does not consider that the active claim is owned by
the caller itself.

### Root-cause evidence

- BOU-1785: PR #2315, `.gaia/pr-maintenance/pr-2315.json` had
  `blockers:["review_comments"]`, `review_comment_ids:[3462557613, 3462557622,
  3462557625]`; `check` returned `0, "deferring to agent-coordinator active:
  claim is active"`; thread-aware GraphQL confirmed 3 unresolved non-outdated
  threads.
- BOU-1788: PR #2317, a live foreign owner marker caused `_check_worktree` to
  return at the line-48 gate before CI was evaluated, so `Integration Tests =
  FAILURE` was never surfaced by the detached loop or sibling checkers.

## Decisions (locked with user)

1. **Scope:** fix the agent-coordinator-claim defer with self-owned → service,
   foreign → clear message; **unify** with the marker/independent-owner defers
   since they are the same function and the same bug family.
2. **Deliverable:** single `agentic-pr-dash` PR. No new CLI subcommand, so gaia
   needs only a snapshot reinstall.
3. **Non-owner policy (the BOU-1788 "block vs warn-only" fork):** a checker that
   does **not** own the PR and sees blockers while deferring to a live owner is
   **warn-only** — it emits an explicit message naming PR #, the blockers, and
   the owner (session/pid), but returns exit 0 and does **not** claim or
   dispatch. This preserves the don't-double-fix invariant. The owner's own
   checks (self path) still service/block.
4. **Self-owned claim → service:** when the active claim is owned by the calling
   session, do not defer — refresh the claim and surface the known blockers
   (exit 10 + maintenance prompt).

### The invariant this establishes

> No defer path in `_check_worktree` may return a clean-looking no-op when the
> owned PR has known blockers. Self/owner → service (exit 10). Foreign live
> owner → warn-only (exit 0) with an explicit, non-clean message that names PR
> #, blockers, and owner.

## Out of scope (explicit)

- **Reclaiming a stale/idle owner's claim** (owner present but not progressing)
  → BOU-1740 (`bou-1740-pr-watch-claim-liveness`) owns claim-liveness/TTL.
  Warn-only is the deliberate choice here; escalation-on-stale is not added.
- **Owner Stop-gate cadence / cross-session notification** (rate-limit + timing
  that delays the owner surfacing its own red PR) — separate concern, untouched.

## Design

### Change 1 — `coordinator.py`: expose the active claim's owner

- Extend `DispatchDecision` with `owner_session_id: str | None = None` and
  `owner_pid: int | None = None`. Populate both from `claim.owner` in
  `dispatch_decision_for_pr` (both the dirty-worktree branch and the normal
  return).
- Add `active_claim_owner_for_pr(pr, *, now=None) -> OwnerIdentity | None`,
  mirroring `active_claim_fingerprint_for_pr`'s by-`task_id`, liveness-filtered
  best-active-claim selection. Refactor the shared selection into a private
  `_best_active_claim_for_pr(pr, *, now)` used by both to avoid duplicating the
  loop. (Used by the claim-defer branch to detect self-ownership independent of
  the `status()`-by-full-identity lookup, which can miss a same-task claim whose
  fingerprint differs.)

### Change 2 — `worktree_check.py`: apply the invariant at all three defers

A small module-local helper renders the warn-only text consistently:

```
def _blocked_defer_text(kind, *, pr_number, blockers, owner_desc) -> str:
    return (
        f"owned PR #{pr_number} has blockers {sorted(blockers)}; "
        f"deferring to {owner_desc} — NOT clean (no fix dispatched)"
    )
```

**(a) Marker foreign-owner (line 48-50):** when `owner is not None`, do not
return immediately. Resolve the PR and compute blockers (same resolve/blocker
logic used below — factor the resolve+blockers prelude so both paths share it).
- gh unavailable / no PR / draft → keep current clean returns.
- blockers present → `return 0, _blocked_defer_text("ci", pr_number=pr.number,
  blockers=blockers, owner_desc=f"live PR-watch owner session {owner}")`.
- no blockers → `return 0, f"deferring to live PR-watch owner session {owner}"`
  (unchanged clean text).
Never claim/dispatch on this path. Cost: one PR-resolve per deferred tick when a
foreign owner is present — accepted per decision 3.

**(b) Live independent owner (line 86-87):** blockers are already in scope.
- blockers present → `return 0, _blocked_defer_text(..., owner_desc="live
  independent owner of this worktree")`.
- (this branch only runs when blockers is truthy, so the no-blocker case does
  not reach here.)

**(c) Agent-coordinator claim (line 110-122):** compute
`active_owner = coordinator.active_claim_owner_for_pr(pr)` and
`self_owned = active_owner is not None and active_owner.session_id ==
owner_session_id`.
- Replace the line-111 guard: defer only when
  `not coord_decision.should_dispatch and not new_feedback and not self_owned`.
  When `self_owned` (or `new_feedback`) is true, fall through to `claim_pr`
  (a same-session `claim_pr` refreshes and returns non-None) and service.
- The defer return (still-foreign active claim, same fingerprint) becomes the
  explicit warn-only text naming the claim id + owner +
  blockers: `return 0, _blocked_defer_text("claim", pr_number=pr.number,
  blockers=blockers, owner_desc=f"active agent-coordinator claim
  {coord_decision.claim_id} (owner session {coord_decision.owner_session_id},
  pid {coord_decision.owner_pid})")`.
- The race-lost branch (line 121-122, `claimed is None and not new_feedback`)
  gets the same explicit text (it is the same "foreign active claim" condition).

The servicing tail (heartbeat refresh, fix-lease, CI logs, prompt, exit 10) is
unchanged.

## Testing strategy (integration-test-first, RED → GREEN)

New `tests/test_defer_hides_blockers.py`, driving the real engine against a real
`JsonlClaimStore` via `AGENTIC_PR_DASH_COORDINATOR_STORE` (tmp path), mirroring
`tests/test_stop_gate_scope.py` (`_stub_check_worktree_to_blockers`, `_blocked_pr`).
No mocking of the subject under test; gh/PR-resolve and process-liveness are the
mocked boundaries.

1. **Self-owned claim services (BOU-1785 core).** Seed an active claim owned by
   `SID` for a PR whose blockers (and fingerprint) are unchanged. RED: pre-fix
   `_check_worktree(wt, SID, claim=True)` returns `(0, "...claim is active")`.
   GREEN: returns exit `10`, text contains `PR_NUMBER=` and
   `COORDINATOR_CLAIM_ID=`.
2. **Foreign-owned claim, same fingerprint → warn-only, explicit text.** Seed an
   active claim owned by a *different* session, pid alive, lease unexpired, same
   fingerprint. Assert exit `0`, text contains `NOT clean`, the blocker name,
   and the owner session id; assert it is **not** byte-equal to a clean
   `"nothing pending"` and does not contain bare `"claim is active"` as the
   whole message.
3. **Marker foreign-owner with red CI → warn-only names blockers (BOU-1788).**
   `_live_foreign_owner` returns a foreign owner; `_resolve_pr_for_branch`
   yields a PR with `failing_checks`. RED: pre-fix returns
   `(0, "deferring to live PR-watch owner session <id>")` with no blocker
   mention. GREEN: exit `0`, text contains `NOT clean` and the blocker, and no
   claim was written (`dispatch_decision_for_pr(pr).should_dispatch` semantics
   unchanged; store has no new claim).
4. **Marker foreign-owner, clean PR → unchanged clean defer.** Foreign owner,
   PR with no blockers → exit `0`, text == `deferring to live PR-watch owner
   session <id>` (regression guard that we did not make every deferral noisy).
5. **Live independent owner with blockers → warn-only names blockers.** Force
   `_live_independent_owner_paths` truthy with a blockered PR → exit `0`, text
   contains `NOT clean` + blocker.
6. **Regression:** existing `test_stop_gate_scope.py`,
   `test_coordinator.py`, `test_orchestrator_ownership.py`,
   `test_bou1637_hardening.py` (the `new_feedback` path) stay green.

## Verification

- `cd ~/code/pr-agent-ops-bou-1785 && python3 -m pytest -q` (full suite green;
  `agentic_pr_dash.__file__` must resolve under the worktree `src`).
- `python3 -m pytest -q tests/test_defer_hides_blockers.py` (the new file).
- Paste RED first-run and GREEN final-run output into the int-test bead.
