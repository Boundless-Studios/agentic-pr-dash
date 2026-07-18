# BOU-2184 — Result

**Status**: complete — PR ready for review (not merged, per instructions)

- **Ticket**: BOU-2184 (agentic-pr-dash: MAINTENANCE_HANDOFF.md write dirties the worktree and self-wedges claim reclaim; cross-repo adoption gap)
- **Branch**: `bou-2184-handoff-wedge-and-adoption` (base `origin/main` @ 69ce7a8)
- **Commit**: 8486470 (7 files, +441/-16)
- **PR**: https://github.com/Boundless-Studios/agentic-pr-dash/pull/82 (ready, five-heading body, "Fixes BOU-2184")
- **Tests**: full suite `960 passed` with `/Users/ilya/code/pr-agent-ops/.venv/bin/python -m pytest -q`; 7 new regression tests in `tests/test_bou2184_handoff_wedge_and_adoption.py`

## Fix 1 — handoff self-wedge

- `src/agentic_pr_dash/maintenance.py`: `handoff_path` now resolves to `<wt>/<state_dir>/MAINTENANCE_HANDOFF.md` (state-dir write); writer mkdirs parent.
- `src/agentic_pr_dash/coordinator.py`: reclaim dirty-check (`worktree_has_dirty_or_unpushed_changes`) excludes loop artifacts — legacy root `MAINTENANCE_HANDOFF.md` + anything under the configured state dir (`_porcelain_path` handles renames/quoted paths). A claim whose only dirt is loop artifacts is reclaimable (tested at claim level: released claim + artifact-only-dirty owner worktree → `should_dispatch=True`, not `manual_intervention`).

## Fix 2 — cross-repo adoption gap

- `src/agentic_pr_dash/_maintenance/worktrees.py`: `_collect_owned_worktrees(adopt_dead_markered=...)` — dead-owner marker takeover (no fresh heartbeat, no fix-lease, pid dead, registry-dead, open non-draft @me PR). Enabled for sibling `maintenance_repo_roots` in `list-owned`, `reconcile-prs`, stop-gate cross-root scan. Anchor roots keep BOU-1953 (ledger path owns same-repo orphan recovery). Live foreign owners always block takeover (BOU-1814 preserved).
- `src/agentic_pr_dash/_maintenance/reconcile.py`: `_adopt_orphan_prs` no longer skips present worktrees; adopts when marker owner is dead and re-stamps the arm marker (durable adoption, `worktree_present: true` in records).

## Notes for reviewer

- Two stub lambdas in `tests/test_reconcile_multi_repo.py` widened to `**kw` for the new kwarg.
- Out of scope, deliberately: `AGENT_RESULT.md` is an *executor* artifact and is NOT excluded from the reclaim dirty-check; `worktrees.py:_worktree_is_dirty` (conservative cleanup gate) left untouched.

---

# PR #82 Maintenance Run — 2026-07-18

**Status**: complete — Codex thread resolved, branch green and pushed

- **Baseline**: `8486470e9a57fea6ba681c434141f5cfbd346c51`
- **Pushed HEAD**: `f2d856daefacaa842c02a370953672581e84ee3b` (merge of origin/main + fix commit)
- **Thread addressed**: comment 3608944192 (`src/agentic_pr_dash/coordinator.py:94`, P2 "Preserve dirty renames into the state directory") — resolved 1/1
- **Fix**: `_porcelain_path` → `_porcelain_paths`; rename/copy porcelain lines now yield BOTH sides, and a line counts as loop-artifact dirt only when *every* side is a loop artifact. A staged rename of a real tracked file into the state dir (or onto the legacy handoff filename) now keeps the worktree dirty, so reclaim cannot dispatch over agent-staged work. Invariants preserved: live owners still block takeover; loop artifacts still never count as dirt; state-dir handoff placement unchanged.
- **Tests**: 2 new regression tests (`test_rename_of_real_file_into_state_dir_counts_as_dirt`, `test_rename_between_loop_artifacts_is_not_dirt`); full suite `984 passed` via `/Users/ilya/code/pr-agent-ops/.venv/bin/python -m pytest -q`
- **maintenance_check complete**: ran with the baseline above — "completed (bead closed; no blockers remain)"
- **CI at handoff**: setup / test 3.11 / test 3.13 SUCCESS; test 3.12 in progress; detached maintenance loop covers any late failure.
