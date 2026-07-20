# BOU-2223 Stage 1 — ownership adapter + dual-write + parity harness

Epic: [BOU-2223](https://linear.app/boundless-studios/issue/BOU-2223/epic-consolidate-pr-maintenance-ownership-onto-agent-coordinator)

## Goal

Introduce one façade — `agentic_pr_dash/ownership.py` — over `agent-coordinator`'s
fenced claims that models **the PR itself** as the claimed task. Dual-write a claim
from every existing ownership-marker write. Add a parity harness that reports where
marker-derived and claim-derived ownership disagree.

**Markers stay authoritative. No reader is flipped. No behaviour changes.**
The full existing suite passing unchanged *is* the acceptance signal.

## Why a new module rather than extending `coordinator.py`

`coordinator.py` already wraps `agent-coordinator`, but for a different question:
"should the loop dispatch an executor at this PR's *current blocker set*?" Its
`TaskIdentity.fingerprint` is a hash of the blockers, so its claims churn every time
CI or the comment set changes — and it needed `_best_active_claim_for_pr` to answer
anything fingerprint-agnostically.

Ownership is blocker-independent: a session owns PR #N across every blocker
transition. That needs a *stable* identity, so it gets its own `task_type` in the
same store.

## Design

### 1. Task identity — `(repo_slug, pr_number)`

```python
OWNERSHIP_TASK_TYPE = "pr-ownership"      # distinct from coordinator.TASK_TYPE
OWNERSHIP_FINGERPRINT = "ownership"       # constant: ownership ignores blockers
task_id = f"github:{repo_slug}#{pr_number}"
```

A constant fingerprint makes `TaskCoordinator.status()` an exact hit, so ownership
needs none of `coordinator.py`'s fingerprint-agnostic scanning. The repo slug in
`task_id` keeps the same PR number in two `maintenance_repo_roots` repos distinct —
a collision the marker scheme handles with a separate strict-repo ledger match.

### 2. Owner identity — provenance becomes intrinsic

`OwnerIdentity` already carries `session_id`, `pid`, `worktree_path`, and a free-form
`metadata: dict[str, str]`. BOU-2221's bespoke `provenance` marker field moves into
`metadata["provenance"]`, alongside `branch`/`armed_at`. Nothing bespoke survives.

### 3. Liveness — reproduce marker semantics exactly

This is the one place the two models genuinely differ, and Stage 1 must encode the
difference rather than paper over it.

`markers._live_foreign_owner` grants ownership when **any** of:

1. heartbeat within `heartbeat_ttl_seconds` (default 600s), or
2. `fix_lease_until` still in the future (default 1800s), or
3. **the owner pid is still alive** — regardless of how stale the heartbeat is.

`TaskCoordinator.status()` returns `EXPIRED` (reclaimable) the moment
`lease_expires_at` passes, *even when the owner pid is live*. Rule (3) exists on
purpose: a session that owns a PR but has no waiter running stops heartbeating while
remaining very much alive, and the machine-wide loop must not claim the PR out from
under it.

So the façade defines liveness as:

```
live  ==  lease not expired                       # tiers 1 & 2, unconditionally
      or  pid positively probes as alive          # tier 3
```

Two details the first cut got wrong, both pinned by existing tests that are the
specification:

* **Within the lease, the pid is never consulted.** `test_dead_pid_with_fresh_heartbeat_still_defers`
  requires a fresh heartbeat to grant ownership even with a dead recorded pid — the
  marker's `pid` is stamped once at arm time by `_resolve_owner_pid` and never
  rewritten, while a detached waiter or the maintenance loop keeps heartbeating
  under the same session id. `TaskCoordinator.status()` would answer `OWNER_DEAD`
  here; the façade must not.
* **A bare arm gets no time-based tier at all.** `_write_arm_marker` writes
  `armed_at` but neither `heartbeat` nor `fix_lease_until`, so a freshly-armed
  marker satisfies neither tier 1 nor 2 and rests entirely on the owner pid. The
  arm's mirrored claim therefore uses `LEASE_PID_TIER_ONLY` (a zero lease) rather
  than a full TTL — otherwise a session that arms and then dies before its first
  heartbeat would read as owned on the claim side and reclaimable on the marker
  side.

Lease seconds otherwise map to the marker's two time tiers, written by
`_touch_owner_heartbeat`: `heartbeat_ttl_seconds` normally, `_fix_lease_seconds()`
when a fix is in flight (`work_found=True`).

One further asymmetry the parity harness has to respect: `_fix_lease_active` treats
an **unparseable** `fix_lease_until` as ACTIVE (it guards a dispatch race, where
deferring is the safe error), but the ownership reader `_live_foreign_owner` falls
through to the pid tier instead — pinned by
`test_corrupt_fix_lease_without_fresh_heartbeat_does_not_defer_forever`. The
marker-side view mirrors the ownership reader, not the helper.

### 4. Bounded, fail-closed reads

`JsonlClaimStore.read_events()` reads the entire file under an exclusive `flock` on
every call — O(events) per `status()`. The stop gate resolves ownership for N owned
worktrees on a hook with a hard ~108s deadline, so per-PR `status()` calls are not
acceptable.

`OwnershipSnapshot` reads the store **once**, indexes `task_id -> latest ClaimRecord`,
and answers many queries. It carries an explicit `ok` flag: on read failure or lock
timeout it returns a snapshot that answers `unknown` — never a false "unowned" — so
every future reader can fail closed.

The lock itself is the sharper hazard. `JsonlClaimStore` takes `fcntl.flock(LOCK_EX)`
with **no timeout**, and marker writes were previously lock-free (`mkstemp` +
`os.replace`) — so the dual-write would be the first blocking lock the stop gate ever
takes, on a file shared with the `pr-maintenance-loop` daemon and every other
session. The surrounding `time.monotonic()` budgets are cooperative and cannot
interrupt a thread parked in a syscall. `BoundedLockClaimStore` therefore acquires
with `LOCK_NB` against a deadline (`AGENTIC_PR_DASH_OWNERSHIP_LOCK_TIMEOUT_SECONDS`,
default 2s) and raises `TimeoutError` rather than waiting; both callers already treat
a store error as fail-closed.

### 5. Dual-write — exactly three points, all in `markers.py`

| marker write | claim op |
| --- | --- |
| `_write_arm_marker` | `record_arm` → `claim_task(lease=heartbeat_ttl)` |
| `_touch_owner_heartbeat` | `record_heartbeat` → `claim_task` again (same session ⇒ heartbeat), lease = fix-lease when `work_found` |
| `_prune_stale_marker` | `record_release(reason="pr_closed")` |

`claim_task` is idempotent for the *same* session on an active claim (it emits a
heartbeat event), so the adapter is stateless — no `claim_id`/`lease_epoch` has to be
persisted next to the marker. Release resolves both from a snapshot.

Every claim write is best-effort: a claim-store failure must never break a marker
write while markers are authoritative. Failures and `ClaimConflictError` are recorded
as divergences, not raised.

### 6. Parity harness

`ownership.parity.compare_worktree(cwd, session_id) -> ParityResult` computes both
views and reports agreement. Divergences append to
`<state_dir>/ownership-parity.jsonl` for the Stage-2 bake period.

The marker side reads through a new read-only `markers.marker_owner_view(cwd)` that
returns `(session_id, pid, provenance, live)` using the existing three-tier rule —
extracted, not changed.

### 7. Tests — `tests/test_ownership_parity.py`

Scenarios drawn from the behaviours the existing suite already encodes:

1. armed marker → claim exists, same session, `provenance=armed`, both live
2. adopted marker → `provenance=adopted` preserved in claim metadata (BOU-2221)
3. foreign live session (fresh heartbeat) → both report foreign-owned
4. foreign dead (stale heartbeat + dead pid) → both report reclaimable
5. **stale heartbeat + live pid** → both still owned (the `EXPIRED`-but-pid-live case)
6. fix-lease active → both owned past the heartbeat TTL
7. `_prune_stale_marker` on a merged PR → marker gone, claim released, both unowned
8. same PR number in two repos → distinct `task_id`s, no cross-talk
9. foreign session holds an active claim → marker write still succeeds, divergence recorded
10. store read failure → snapshot `unknown` (fail closed), not "unowned"

## Out of scope for this PR

- Flipping any reader (`stop_gate.py` is Stage 2)
- Deleting or shimming `pr-watch.armed` (Stages 4–5)
- `session_registry.py`'s harness `StatusReport` ingest (never in scope)
- The gaia `APD_REF` pin bump — Stage 1 is inert until a separate gaia PR pins it
