# BOU-2223 Stage 4 — retire the marker writers, keep `pr-watch.armed` as a read-only shim

Stage 4 of 5. Stages 1–3 consolidated the ownership **reads** onto agent-coordinator
claims. Writes were still going to `pr-watch.armed`, which is what kept the epic's
premise — "two independent ownership systems" — true. This stage makes the claim the
write authority and demotes the marker to a read-only compatibility shim for one
release. Stage 5 deletes the shim.

## What changes

`markers.marker_writes_enabled()` gates every ownership-marker write and defaults
**OFF**. `AGENTIC_PR_DASH_MARKER_WRITES=1` restores the Stage 1–3 dual-write without a
package rollback. Its parsing is deliberately the inverse of `dual_write_enabled` /
`claim_reads_enabled` (they default ON and accept off-values; this defaults OFF and
accepts on-values) because re-enabling a retired writer must be explicit.

Gated writers: `_write_arm_marker`, `_touch_owner_heartbeat`. Not gated:
`pr-watch.session`, which records *which session owns a worktree's terminal* — session
identity, not PR ownership, and it has no claim equivalent.

Two consequences fall out of the flip:

* **Arming becomes genuinely fenced.** `_write_arm_marker` now returns the claim
  outcome, so when another live session already holds `(repo, pr)`, `record_ownership`
  refuses and the arm declines. Under the dual-write the marker write always
  "succeeded" and the losing session went on believing it owned the PR — the BOU-2221
  bug class.
* **Heartbeats need a source.** With no file on disk `_touch_owner_heartbeat` has
  nothing to read, so `_heartbeat_fields_from_claim` rebuilds the marker's exact field
  shape from the live claim. Returning the marker shape keeps the coalescing and
  lease-tier logic untouched rather than forking it per source.

## The part that is not just "stop writing"

Retiring the writer **blinds every reader Stage 3 did not flip**. A marker-only check
silently degrades to "nobody owns this" — it does not fail, it returns the wrong
answer. Stage 3 flipped five readers; these were left, and each is unioned here
(claim check *alongside* the marker check, never replacing it — the precedent Stage 3
set in `worktrees._collect_owned_worktrees`):

| Site | Decision it drives | Failure mode if left blind |
|---|---|---|
| `worktree_check.py:371` | the primary cross-session ownership gate | two sessions dispatch on one PR; the coordinator claim is a different store with its own lease and does not implement "live in-session owner wins" |
| `loop.py:641` | is the detached loop *coverage* for this PR | loop reports phantom coverage for a PR a live session holds, **and** suppresses that session's waiter |
| `orchestrator.py:698` | dashboard session-precedence | headless maintenance dispatches into a worktree a live session is editing; **no other guard on this path** |
| `stop_gate.py:359` | which owned PRs demand a feedback waiter | fail-**open**: a session owning a live open PR is never told to start a waiter, so review comments and red CI stop waking it |

New shared helpers in `ownership_resolution`: `live_foreign_claim` (bool) and
`live_foreign_claim_owner` (owner id, mirroring `_live_foreign_owner`'s return shape so
callers can union with a plain `marker_owner or claim_owner`). Both fail open on error,
matching the marker helpers — a claim-store problem must not wedge maintenance.

`stop_gate.py:359` unions rather than replaces `_owned_open_pr_numbers`, because that
function is a documented test seam several tests monkeypatch with a one-argument stub.

## Cleanup: claims must be released, not just stop being written

`release_ownership` had exactly **one** call site — inside `_prune_stale_marker`, and
only when the PR was already merged/closed. Under Stage 3 that was cosmetic because the
marker was authority. Under Stage 4 a leaked claim **is** live ownership of a dead PR
that nothing ever clears.

Observed live during the entry-gate triage: worktree `2223-done=done` held an active
claim on gaia-free#2697, which was **merged**, with its marker already deleted.

So `release_ownership_claims` is extracted and additionally called from
`reconcile._detached_pr_records`, which retires merged/closed PRs that have no live
worktree — precisely the ones `_prune_stale_marker` can never see, since it is driven
by a marker in a worktree that path has already established is gone. It is deliberately
*not* gated on `dual_write_enabled()`: that switch governs whether we still mirror
marker writes, and turning the mirror off must not strand claims written while it was on.

## Entry gate

Not "zero divergences" — that will never pass. Triage each `marker_only_worktree`
record against `~/.agent-coordinator/claims.jsonl` (fields nest under
`claim.owner.*` and `claim.lease_expires_at`, not at the top level):

1. **Pre-Stage-1 marker** — session has zero `pr-ownership` claims ever. Migration decay.
2. **Lease-lapse window** — session has claims, but the covering claim is past
   `lease_expires_at` and its pid isn't probeable. Correct behaviour, permanent noise.
3. **Dual-write ordering race** *(new — not in the original ticket)*. The divergence is
   logged 150–400 ms **before** the mirrored claim lands. Measured: gaia-free#2702 at
   Δ239 ms, bou-2248#2690 at Δ359 ms. Also permanent steady-state noise.
4. **Genuine defect** — anything else. The gate is "none of these".

Measured 2026-07-21 over 7844 records: 6485 population-1, 1351 population-2, the rest
resolving to population-3 or the 5 `claim_only_worktree` records covered by the leak fix
above.

## Verification

Absence of divergences only proves the sources never disagreed. Get **positive**
evidence, and run it with the **base interpreter** so it imports the INSTALLED package:

* resolve every worktree and compare the marker view against the claim view, in both
  directions — `MARKER_LIVE_CLAIM_DEAD` (retiring the writer loses ownership) and
  `CLAIM_LIVE_MARKER_DEAD` (a dead session's ownership sticks forever, the
  constraint-1 violation);
* flip `AGENTIC_PR_DASH_OWNERSHIP_CLAIM_READS=0` and assert every reader reverts.

**Expected, and intended:** most worktrees resolve `source=marker` at any given moment,
because markers persist after session death and claims do not. Once the writer retires,
those dead-session markers stop being created and the stale ownership they assert goes
away. That garbage collection is the point of the stage, not a regression — but it is
why the DoD's "every claim-named worktree resolves `source in (claim,both)`" is a weak
gate on its own: only live sessions have claims, so it is near-trivially satisfiable.

**Merging the gaia pin does NOT start the bake.** The hooks run the *installed* package.
Run `bash scripts/install-agent-ops-tools.sh`, then grep the installed site-packages for
this stage's symbol (`marker_writes_enabled`) before believing an empty divergence log —
an empty log from uninstalled code reads exactly like a clean bake.

## Kill-switch interaction (must be documented, and is a Stage 5 cleanup)

`AGENTIC_PR_DASH_OWNERSHIP_CLAIM_READS=0` makes `resolve_owned` return an empty
`provenance_for`, and `stop_gate.py:284` then falls back to `_marker_provenance`, which
returns `"armed"` when no marker exists. So **claim-reads-off + marker-writes-off
reinstates BOU-2221**: every adopted worktree blocks the stop again. The two switches are
not composable; reverting reads must be paired with `AGENTIC_PR_DASH_MARKER_WRITES=1`.

## Known follow-ups (not this stage)

* `session_ledger` is a **third** ownership store, still written unconditionally by
  `_write_arm_marker` and still read as an independent authority via
  `_live_pr_owner_record`. Its liveness model (registry non-terminal + pid alive)
  differs from the claim's three-tier rule, so the two can disagree about one session.
  It is currently the only surviving cross-session guard on some paths, which makes
  retiring it later more dangerous, not less. Needs its own stage.
* `ownership_parity.compare_worktree` goes dark once the marker stops being written —
  decide whether parity reporting retires with the writer before Stage 5.
* `worktree_check._owner_progress_state` still reads the marker to classify an owner as
  `fixing`/`idle`; the claim carries the equivalent signal (`OwnerView.state` plus the
  lease-vs-heartbeat delta, exactly as `_heartbeat_fields_from_claim` reconstructs it).

## Paired gaia pin bump

`APD_REF` in `config/agent-ops-pins.env` is the source of truth, but `pyproject.toml` and
`uv.lock` pin the same sha independently — edit the pins file, edit pyproject, `uv lock`,
then grep the old sha to confirm zero leftovers. Verify apd's own `uv.lock` is unchanged
across the bump to keep the shared agent-coordinator revision constraint with
`HARNESS_REF`. Run the guard test **from the worktree holding the change**; running it
from `~/code/gaia-free` is a false green against the old pin — check the printed `rootdir`.
