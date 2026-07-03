/* CyberNewsHub service worker — app-shell cache so the reader opens offline.
   Bump CACHE when the shell changes to force an update. */
const CACHE = 'cybernewshub-v1';
const SHELL = ['/', '/static/style.css', '/static/icon.svg', '/static/favicon.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;     // leave cross-origin (favicons, thumbnails) alone
  if (url.pathname.startsWith('/api/')) return;    // live data always comes from the network

  if (req.mode === 'navigate') {
    // Network-first for pages, fall back to the cached shell when offline.
    e.respondWith(fetch(req).catch(() => caches.match('/')));
  } else {
    // Stale-while-revalidate for static assets: serve the cached copy instantly
    // (fast, works offline) while refreshing the cache in the background, so an
    // edited style.css/icon shows up on the next load without bumping CACHE.
    e.respondWith(
      caches.open(CACHE).then(c => c.match(req).then(cached => {
        const network = fetch(req).then(resp => {
          if (resp && resp.ok) c.put(req, resp.clone());
          return resp;
        }).catch(() => cached);
        return cached || network;
      }))
    );
  }
});
