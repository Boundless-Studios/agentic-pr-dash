# Plan: Split `maintenance_check.py` (BOU-1690 WS1)

## Goal

Behavior-preserving decomposition of `src/agentic_pr_dash/maintenance_check.py`
(2,690 lines, ~70 functions) into a `maintenance/` package of
responsibility-focused modules. Unify the duplicate `_iter_worktree_paths`.
No behavior changes.

## Invariant (the spec)

The existing pytest suite is the characterization spec.

- Baseline: `python3 -m pytest -q` → **562 passed** (2026-06-18, origin/main @ 3c0d946).
- The suite must stay green at **every** commit.

## Compatibility contract

External consumers of `maintenance_check`:
- `cli.py` → `maintenance_check.main(...)`
- `codex_hooks/run_arm_pr_watch.py` → `maintenance_check.main(...)`
- `loop.py` → `maintenance_check._resolve_maintenance_roots`
- tests → `from agentic_pr_dash import maintenance_check as mc`, reach ~30
  privates via `mc.X` and **patch seams** (`mc.subprocess`, `mc.os`, `mc.time`,
  `mc._read_marker`, `mc._claim_pr`, …).

Strategy: **Facade + migrate patch-seams.**
- `maintenance_check.py` keeps `main` + `_cmd_*` dispatchers + arg parsing, and
  re-exports all moved symbols so `mc.X` reads keep resolving.
- Tests that *patch* a moved seam and rely on an internal caller seeing the
  patch are repointed to the new module (`monkeypatch.setattr(markers, ...)`).
- Tests that only *read* `mc.X` are untouched.

## Target structure: `src/agentic_pr_dash/maintenance/`

| Module | Responsibility |
|---|---|
| `_common.py` | shared primitives: `_parse_iso`, `_env_int`, `_fix_lease_seconds`, `_pid_alive`, `_resolve_owner_pid`, `_current_branch`, `_repo_slug` |
| `pr_state.py` | PR resolution + GitHub reads + review threads |
| `markers.py` | ownership/session markers, heartbeats, leases, claims |
| `worktrees.py` | worktree iteration + root resolution + owned-worktree collection; **unified `_iter_worktree_paths`** |
| `stop_gate.py` | stop-state persistence, fingerprinting, prompt/waiter block rendering, `_stop_gate_impl` |
| `completion.py` | completion reply rendering + review-comment extraction |
| `reconcile.py` | orphan adoption + detached/owned PR records |
| `waiter.py` | await pidfiles + await/detached-loop liveness |
| `maintenance_check.py` (kept) | thin CLI: `main`, `_cmd_arm/_check/_list_owned/_complete/_stop_gate/_reconcile_prs/_await`, `_check_worktree`; re-export facade |

## Duplicate `_iter_worktree_paths`

- `@960` ("branch-agnostic wrapper", filters bare/locked via
  `_iter_worktrees_with_branch`) is **dead** — shadowed at runtime by `@1922`.
- `@1922` ("porcelain", yields *all* worktrees) is the live behavior for every
  caller.
- Unify to one function in `worktrees.py` preserving the **porcelain (all)**
  behavior. Audit each caller; if any genuinely needs the bare/locked-filtered
  variant, add a separately-named helper rather than silently changing behavior.

## Execution order (dependency-leaf first)

1. `_common.py` + `pr_state.py` + `markers.py` (foundation).
2. `worktrees.py` (+ unify `_iter_worktree_paths`, audit callers).
3. `stop_gate.py`, `completion.py`, `reconcile.py`, `waiter.py`.
4. Slim `maintenance_check.py` to CLI + facade; migrate patch-seam tests.
5. Add per-module focused test files. Full suite green + ruff clean.

Run `python3 -m pytest -q` after each extraction step.

## Out of scope

WS2 (`run_arm_pr_watch.py`), WS3 (`ci_watch.py`), WS4 (worktree-deck), WS5
(arch docs) — separate beads/PRs. No new feature behavior. No Gaia adapter
changes (the package API is unchanged).
