// Minimal service worker — required for PWA installability.
// Network-first for the app shell, with a cache fallback so the dashboard still
// opens offline.
//
// The cache MUST NOT cover live data (BOU-2193). It previously cached every GET,
// including /partials/board, and fell back to the cached copy whenever the network
// failed. So when the dashboard server died, the board kept rendering a stale
// cached response as a 200: htmx swapped in old content, no error event ever fired,
// and the "Live" chip stayed green. The dashboard looked current while showing data
// from hours earlier — the "stuck in view" symptom. Live endpoints must be allowed
// to fail loudly so the freshness indicator in app.js can report them.

const CACHE = "pr-dash-v3"; // v3: purges the v2 cache, which contains stale partials
const SHELL = ["/", "/static/style.css", "/static/app.js", "/static/icon.svg"];

// Live data — never cached, never served from cache.
function isLiveData(url) {
  return url.pathname.startsWith("/partials/") || url.pathname.startsWith("/api/");
}

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;

  const url = new URL(e.request.url);
  // Pass live-data requests straight through, untouched: no cache write, and no
  // cache fallback on failure. A failed board poll has to reach htmx as a failure.
  if (isLiveData(url)) return;

  e.respondWith(
    fetch(e.request)
      .then((r) => {
        const clone = r.clone();
        caches.open(CACHE).then((c) => c.put(e.request, clone));
        return r;
      })
      .catch(() => caches.match(e.request))
  );
});
