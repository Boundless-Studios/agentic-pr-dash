/* PR Dashboard — minimal JS for HTMX enhancements */

function normalizeSearchText(value) {
    return (value || '').toLowerCase().replace(/\s+/g, ' ').trim();
}

function searchableCardText(card) {
    return normalizeSearchText(`${card.dataset.searchText || ''} ${card.textContent || ''}`);
}

// Everything the search box filters and a click can focus. The Worktrees tab
// (BOU-2431) renders rows rather than board cards, and they carry the same
// data-search-text / data-worktree-path affordances — matching only `.card`
// left the search box and the click-to-focus silently dead on that tab.
const FILTERABLE_SELECTOR = '.board .card, .worktrees-panel .worktree-row';
const FOCUSABLE_SELECTOR = '.card[data-worktree-path], .worktree-row[data-worktree-path]';

function applyCardFilter() {
    const input = document.getElementById('pr-card-filter');
    const query = normalizeSearchText(input ? input.value : '');

    // Filter every card in the board and every row on the Worktrees tab.
    document.querySelectorAll(FILTERABLE_SELECTOR).forEach(function(card) {
        const hidden = query !== '' && !searchableCardText(card).includes(query);
        card.classList.toggle('card-hidden', hidden);
    });

    // Worktrees tab: mirror the board's visible/total behaviour in its header.
    const worktreeRows = document.querySelectorAll('.worktrees-panel .worktree-row');
    const worktreeCount = document.querySelector('.worktrees-panel .worktrees-count');
    if (worktreeCount) {
        const total = worktreeRows.length;
        let visible = 0;
        worktreeRows.forEach(function(row) {
            if (!row.classList.contains('card-hidden')) {
                visible += 1;
            }
        });
        const noun = total === 1 ? 'worktree' : 'worktrees';
        worktreeCount.textContent = query === ''
            ? `${total} ${noun}`
            : `${visible}/${total} ${noun}`;

        const emptySearch = document.querySelector('.worktrees-panel .worktrees-empty-search');
        if (emptySearch) {
            emptySearch.hidden = query === '' || visible > 0 || total === 0;
        }
    }

    // Update per-column count + empty-state after filtering.
    document.querySelectorAll('.kanban-column').forEach(function(column) {
        const cards = column.querySelectorAll('.card');
        const total = cards.length;
        let visible = 0;
        cards.forEach(function(card) {
            if (!card.classList.contains('card-hidden')) {
                visible += 1;
            }
        });

        const count = column.querySelector('.column-count');
        if (count) {
            count.textContent = query === '' ? String(total) : `${visible}/${total}`;
        }

        const emptySearch = column.querySelector('.column-empty-search');
        if (emptySearch) {
            emptySearch.hidden = query === '' || visible > 0 || total === 0;
        }
    });
}

document.addEventListener('input', function(event) {
    if (event.target && event.target.id === 'pr-card-filter') {
        applyCardFilter();
    }
});

// --- Scroll state preservation across HTMX swaps ---
//
// The board refreshes every 5s and the event log every 3s via hx-swap=innerHTML,
// which destroys and recreates the children. Without intervention, any scroll
// position inside the per-column lists or the event log is lost on each refresh.
// Strategy: continuously track scrollTop on the (always-present) scrollable
// elements via a document-level capture listener, then restore after each swap.

const scrollSnapshots = {
    columns: new Map(),  // column id (e.g. "needs_attention") -> scrollTop
    eventList: 0,
};

function columnId(column) {
    // Column class is "kanban-column col-<id>"; pull the id out.
    const match = Array.from(column.classList).find(function(cls) {
        return cls.startsWith('col-');
    });
    return match ? match.slice(4) : null;
}

// Scroll events don't bubble, so we capture them at the document root.
document.addEventListener('scroll', function(event) {
    const el = event.target;
    if (!el || el.nodeType !== 1) {
        return;
    }
    if (el.classList && el.classList.contains('column-cards')) {
        const column = el.closest('.kanban-column');
        const id = column ? columnId(column) : null;
        if (id) {
            scrollSnapshots.columns.set(id, el.scrollTop);
        }
    } else if (el.classList && el.classList.contains('event-list')) {
        scrollSnapshots.eventList = el.scrollTop;
    }
}, true);

function restoreBoardScroll(boardEl) {
    boardEl.querySelectorAll('.kanban-column').forEach(function(column) {
        const id = columnId(column);
        const cards = column.querySelector('.column-cards');
        if (id && cards && scrollSnapshots.columns.has(id)) {
            const top = scrollSnapshots.columns.get(id);
            // Double rAF: first frame layout completes, second frame guarantees
            // scrollHeight is final so scrollTop assignment isn't clamped to 0.
            requestAnimationFrame(function() {
                requestAnimationFrame(function() {
                    cards.scrollTop = top;
                });
            });
        }
    });
}

// ─── Board freshness (BOU-2193) ──────────────────────────────────────────────
// htmx skips the swap when a poll returns an error, so a failing /partials/board
// leaves the last-good board on screen indefinitely. Previously the "Live" chip
// stayed green throughout, so a dashboard that had been frozen for hours looked
// current. Track poll outcomes here — in the browser, where the missing response
// is actually observable — and degrade the chip accordingly.

let lastGoodBoardSwap = Date.now();
let lastBoardError = null;

function formatStaleAge(ms) {
    const seconds = Math.floor(ms / 1000);
    if (seconds < 60) {
        return seconds + 's';
    }
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) {
        return minutes + 'm';
    }
    return Math.floor(minutes / 60) + 'h' + (minutes % 60) + 'm';
}

function renderBoardFreshness() {
    // A request that began before blur can finish after polling is paused.
    // Keep the inactive lifecycle state authoritative over late responses.
    if (!dashboardPollingActive) {
        renderDashboardPaused();
        return;
    }

    const dot = document.getElementById('live-dot');
    const label = document.getElementById('live-label');
    if (!dot || !label) {
        return;
    }

    if (!lastBoardError) {
        dot.classList.remove('live-dot-stale');
        label.classList.remove('live-label-stale');
        label.textContent = 'Live';
        label.title = 'Board is polling normally';
        return;
    }

    const age = formatStaleAge(Date.now() - lastGoodBoardSwap);
    dot.classList.add('live-dot-stale');
    label.classList.add('live-label-stale');
    label.textContent = 'stale ' + age;
    label.title =
        'Board poll failing (' + lastBoardError + '). Showing the last successful render, ' +
        age + ' old.';
}

// --- Standalone-app polling lifecycle ---
// Chrome keeps an installed app alive when it is covered or unfocused. HTMX's
// interval triggers otherwise continue hitting every partial indefinitely.
// Cancel only dashboard-owned periodic requests; manual forms and other HTMX
// interactions retain their normal behaviour. A transition back to active
// emits exactly one refresh per poller, irrespective of whether Chrome reports
// focus and visibility changes together.

function canPollDashboard() {
    return !document.hidden && document.hasFocus();
}

let dashboardPollingActive = canPollDashboard();

function renderDashboardPaused() {
    const dot = document.getElementById('live-dot');
    const label = document.getElementById('live-label');
    if (!dot || !label) {
        return;
    }

    dot.classList.add('live-dot-stale');
    label.classList.add('live-label-stale');
    label.textContent = 'Paused';
    label.title = 'Dashboard polling is paused while this window is inactive';
}

function syncDashboardPolling() {
    const wasActive = dashboardPollingActive;
    dashboardPollingActive = canPollDashboard();
    if (dashboardPollingActive) {
        if (wasActive) {
            return;
        }
        renderBoardFreshness();
        document.querySelectorAll('[data-dashboard-poller]').forEach(function(poller) {
            htmx.trigger(poller, 'dashboardRefresh');
        });
    } else if (wasActive) {
        renderDashboardPaused();
    }
}

document.addEventListener('visibilitychange', syncDashboardPolling);
window.addEventListener('focus', syncDashboardPolling);
window.addEventListener('blur', syncDashboardPolling);

document.addEventListener('htmx:beforeRequest', function(event) {
    const source = event.detail && event.detail.elt;
    if (source && source.matches('[data-dashboard-poller]') && !dashboardPollingActive) {
        event.preventDefault();
    }
});

if (!dashboardPollingActive) {
    renderDashboardPaused();
}

// Re-render on an interval so the age keeps ticking while the board sits stale,
// rather than freezing at whatever it read when the first poll failed.
setInterval(function() {
    if (dashboardPollingActive) {
        renderBoardFreshness();
    }
}, 1000);

function isBoardRequest(detail) {
    const target = detail && detail.target;
    if (target && target.classList && target.classList.contains('board')) {
        return true;
    }
    // On an error htmx may not resolve the swap target, so fall back to the path.
    const path = detail && detail.pathInfo && detail.pathInfo.requestPath;
    return Boolean(path && path.indexOf('/partials/board') !== -1);
}

document.addEventListener('htmx:responseError', function(event) {
    // Scoped to the board: a failing event-log or runner-fleet poll says nothing
    // about whether the PR board itself is current.
    if (!isBoardRequest(event.detail)) {
        return;
    }
    const status = event.detail.xhr ? event.detail.xhr.status : 'error';
    lastBoardError = 'HTTP ' + status;
    renderBoardFreshness();
});

document.addEventListener('htmx:sendError', function(event) {
    if (!isBoardRequest(event.detail)) {
        return;
    }
    lastBoardError = 'network error';
    renderBoardFreshness();
});

document.addEventListener('htmx:afterSwap', function(event) {
    const target = event.detail.target;
    if (!target) {
        return;
    }
    if (target.classList.contains('board')) {
        lastGoodBoardSwap = Date.now();
        lastBoardError = null;
        renderBoardFreshness();
        applyCardFilter();
        restoreBoardScroll(target);
        // Cards have no entry animation. Do not synchronously read layout for
        // every card after every five-second swap.
    } else if (target.classList.contains('worktrees-panel')) {
        // The tab's 5s poll replaces every row; without this the search box
        // stops filtering the moment the first swap lands.
        applyCardFilter();
    } else if (target.classList.contains('event-list')) {
        const top = scrollSnapshots.eventList;
        requestAnimationFrame(function() {
            requestAnimationFrame(function() {
                target.scrollTop = top;
            });
        });
    }
});

function isInteractiveTarget(target) {
    return Boolean(target.closest('a, button, input, textarea, select, summary, details, form, label'));
}

async function focusWorktree(card) {
    const worktreePath = card.dataset.worktreePath;
    if (!worktreePath) {
        return;
    }

    card.classList.add('card-nav-pending');
    try {
        const response = await fetch('/api/focus-worktree', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
            },
            body: new URLSearchParams({ path: worktreePath }),
        });

        if (response.ok) {
            card.classList.add('card-nav-success');
            window.setTimeout(function() {
                card.classList.remove('card-nav-success');
            }, 900);
        }
    } finally {
        card.classList.remove('card-nav-pending');
    }
}

document.addEventListener('click', function(event) {
    const card = event.target.closest(FOCUSABLE_SELECTOR);
    if (!card || isInteractiveTarget(event.target)) {
        return;
    }
    event.preventDefault();
    focusWorktree(card);
});

document.addEventListener('keydown', function(event) {
    if (event.key !== 'Enter' && event.key !== ' ') {
        return;
    }

    const card = event.target.closest(FOCUSABLE_SELECTOR);
    if (!card || isInteractiveTarget(event.target)) {
        return;
    }

    event.preventDefault();
    focusWorktree(card);
});

document.addEventListener('DOMContentLoaded', applyCardFilter);
