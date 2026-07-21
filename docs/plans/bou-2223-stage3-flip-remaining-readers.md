# BOU-2223 Stage 3 — flip the remaining ownership readers onto claims

Stage 1 (#91) added `ownership.py` over `agent-coordinator` and dual-wrote a claim
from every marker write. Stage 2 (#92) flipped **the stop gate** onto claim-derived
reads via `_maintenance/ownership_resolution.resolve_owned`, with the marker as
fallback and every disagreement logged to `<state_dir>/ownership-parity.jsonl`.

Stage 3 flips the **remaining five readers**. Markers stay the write authority —
retiring the writers is Stage 4.

## Bake result from Stage 2 (the gate on this stage)

Seven `stop_gate_divergence` records, all `marker_only_worktree`, across four
sessions. Diagnosed, and **none is a defect**:

| worktree | marker `armed_at` | first ownership claim | verdict |
| --- | --- | --- | --- |
| `bou-2248` | 01:29:10Z | 01:29:11Z | dual-write OK (1s) |
| `bou-2254` | 01:42:43Z | 01:42:44Z | dual-write OK (1s) |
| `bou-2255` | 01:03:14Z | *never* | armed by a pre-Stage-1 install |
| `verify-2195` | (pruned, PR merged) | *never* | armed by a pre-Stage-1 install |

Every worktree armed *after* the Stage-2 install dual-writes correctly. The
divergences are migration decay from markers written before the pin bump, and they
self-clear on the next arm. The claim side is sound; Stage 3 proceeds.

One observability gap this leaves, accepted for now: a marker written by a
pre-Stage-1 install is indistinguishable from a dual-write that silently failed.
`dual_write_failed` covers the latter only when `record_ownership` returns
not-ok, so a genuinely absent writer logs nothing but `marker_only_worktree`.

## Contract every reader in this stage must preserve

Carried forward verbatim from Stage 2 — these are not negotiable:

1. **Union, never intersection.** Ownership from *either* source is ownership.
   The stop gate fails CLOSED (BOU-1953); dropping a worktree means a session
   stops while it still owns unaddressed work.
2. **Provenance resolves toward `armed`.** A false `adopted` un-blocks a gate
   that should still block (BOU-2221). When the sources disagree, the stricter
   answer wins.
3. **`snapshot.known() is False` means "could not look", not "no claims."** Fall
   back entirely to markers and log one divergence explaining why.
4. **One snapshot per call path.** Never one `ownership.snapshot()` per worktree.
   The Stop hook has a hard ~108s deadline and fails closed past it.
5. **A stale claim must not resurrect a deleted worktree.** A claim-only path
   must still be a directory *and* still appear in a live `git worktree list`
   from one of the maintenance roots.
6. **`AGENTIC_PR_DASH_OWNERSHIP_CLAIM_READS=0` reverts every reader**, Stage 3's
   included, without a package rollback.
7. **The existing tests are the specification.** Where one genuinely disagrees,
   that is a design signal — do not rewrite it to fit the new model.

## New shared helper

`resolve_owned` answers the *set* question ("which worktrees do I own"). Stage 3
needs the *single-worktree* question, so add to `ownership_resolution.py`:

```python
def resolve_worktree(worktree_path, *, session_id=None, snap=None) -> WorktreeOwnership
```

returning `session_id`, `pr_number`, `provenance`, `source` (`"claim"` /
`"marker"` / `"both"`), plus the divergence record if the two disagreed. It
consults the claim first and falls back to a fresh per-worktree marker read,
matching `resolve_owned`'s documented fallback contract. Callers resolving many
worktrees pass one shared `snap`.

## Per-reader work

| File | Current marker read | Flip to |
| --- | --- | --- |
| `_maintenance/worktrees.py:208` `_marker_pr` | `_read_marker(...)["pr"]` | `resolve_worktree(...).pr_number` |
| `_maintenance/worktrees.py:468` `_collect_stop_gate_worktrees` | `_marker_session_id(wt) != session_id` | claim-or-marker session match |
| `_maintenance/worktrees.py:494` `_worktree_is_for_entry` | marker `pr` vs entry | claim-first PR match, marker fallback |
| `_maintenance/worktrees.py:270` `_collect_owned_worktrees` | `_read_marker`, `_marker_session_id`, `_live_foreign_owner`, `_live_pr_owner_record`, `_session_is_live` | claim-first **reads** only; the adoption *write* logic is unchanged (Stage 4 owns that) |
| `_maintenance/reconcile.py:8` | same five helpers | claim-first via one shared snapshot per reconcile pass |
| `_maintenance/waiter.py:166` `_request_waiter_coverage` | `markers._marker_session_id(cwd) != session_id` | claim-first ownership check |
| `app.py:632` `_ownership_for_card` | marker `session_id`/`pid`/`armed_at`/`heartbeat` | prefer the claim (`owner_session_id`, `owner_pid`, `lease_epoch`, `state`), fall back to the marker; display-only, must never raise |
| `codex_hooks/run_post_push_watch.py:240` | `_read_session_marker` + `_read_marker` | claim-first session + PR |

## Divergence logging

Each reader logs under its own `kind` so the Stage 4 bake is attributable per
call path: `reconcile_divergence`, `waiter_divergence`, `card_divergence`,
`post_push_divergence`, `worktree_divergence`. Same `log_divergence` sink and
record shape as Stage 2.

## Definition of done

- [ ] All five readers resolve ownership claim-first with a marker fallback
- [ ] One `ownership.snapshot()` per call path, never per worktree
- [ ] Kill switch reverts every Stage-3 reader to pure marker reads
- [ ] Existing tests pass unmodified
- [ ] New tests cover: kill switch off, unreadable snapshot, claim-only worktree,
      marker-only worktree, and claim/marker disagreement per reader
- [ ] Paired gaia `APD_REF` pin bump so the stage actually goes live
