# Verification Report: bou-1551-too-much-detail-in-pr-dash

Linear: [BOU-1551](https://linear.app/boundless-studios/issue/BOU-1551) — Too much detail in PR dash.

## Test Strategy

1. Contract-first: 19 failing tests (`tests/test_card_presentation.py`) pin the new
   card presentation before any implementation (RED commit `e9980d8`).
2. Implementation makes the contract green without regressing the pre-existing 70 tests.
3. Live three-layer E2E via Playwright against the real gaia-free worktree/PR data.

## Results

### Backend Tests

- `tests/test_card_presentation.py`: 19/19 passed (was 19 failed at RED baseline)
- Full suite `python3 -m pytest tests/ -q`: **89 passed, 0 failed**

### E2E Verification (live data, port 9311, branch code via PYTHONPATH)

| Step | Expected | Actual | Status |
|------|----------|--------|--------|
| Initial load | 200, board container present | 200, 5 columns rendered | PASS |
| Column counts | High-contrast, legible | 7/10/9/0/0 — `rgb(230,233,239)` w600 on solid chip | PASS |
| Single state badge | Exactly 1 per card | 26/26 cards have exactly one `.card-state` | PASS |
| Comment display | Count only, no bodies | Chips ("3 comments", "1 comment"); 0 body/author/path leakage | PASS |
| Field order | Started < Updated < PR link < Worktree link | DOM indexes strictly increasing | PASS |
| PR link href | github.com PR URL | `https://github.com/Boundless-Studios/gaia-free/pull/2068` | PASS |
| Search filter | Narrows cards | "bou-1551" → 26 cards → 1 | PASS |
| Details expander | Diagnostics inside only | maintenance state/bead/blockers/heartbeat/progress/session id/runner/head sha inside `<details>`; none outside | PASS |
| Network (≥5s htmx cycles) | All 200 | `/partials/board|event-log|runner-fleet|bug-bash-banner` all 200 | PASS |
| Backend log | No ERROR/Traceback | 0 occurrences | PASS |

### Issues caught during E2E and fixed (commit `19f9a9c`)

- Unscoped `.state-*` CSS rules tinted entire cards (card div carries `state-*` for JS hooks) — scoped to `.card-state`.
- "1 comments" → "1 comment" pluralization.
- Branch line duplicated worktree name when identical — now suppressed.

### Screenshots

- `bou1551-board-final.png` — final board, live data, all fixes applied
- `bou1551-board-after-full.png` — pre-polish capture (whole-card tint visible on Needs Attention column)

## Verdict: PASS
