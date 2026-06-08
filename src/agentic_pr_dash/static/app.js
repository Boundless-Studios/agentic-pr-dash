/* PR Dashboard — minimal JS for HTMX enhancements */

function normalizeSearchText(value) {
    return (value || '').toLowerCase().replace(/\s+/g, ' ').trim();
}

function searchableCardText(card) {
    return normalizeSearchText(`${card.dataset.searchText || ''} ${card.textContent || ''}`);
}

function applyCardFilter() {
    const input = document.getElementById('pr-card-filter');
    const query = normalizeSearchText(input ? input.value : '');

    // Filter every card in the board, regardless of column.
    document.querySelectorAll('.board .card').forEach(function(card) {
        const hidden = query !== '' && !searchableCardText(card).includes(query);
        card.classList.toggle('card-hidden', hidden);
    });

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

document.addEventListener('htmx:afterSwap', function(event) {
    const target = event.detail.target;
    if (!target) {
        return;
    }
    if (target.classList.contains('board')) {
        applyCardFilter();
        restoreBoardScroll(target);
        target.querySelectorAll('.card').forEach(function(card) {
            card.style.animation = 'none';
            card.offsetHeight; // trigger reflow
            card.style.animation = '';
        });
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
    const card = event.target.closest('.card[data-worktree-path]');
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

    const card = event.target.closest('.card[data-worktree-path]');
    if (!card || isInteractiveTarget(event.target)) {
        return;
    }

    event.preventDefault();
    focusWorktree(card);
});

document.addEventListener('DOMContentLoaded', applyCardFilter);
