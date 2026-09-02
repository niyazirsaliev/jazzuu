// Jazzuu app-shell service worker.
// Caches the static shell for offline open. NEVER caches /api or /audio.
// v24 adds cursor paging and the persisted RU/EN application shell.
// `skipWaiting` + `clients.claim` below means an
// installed PWA receives this replacement on its ordinary next open/refresh.
const CACHE = 'zapisi-shell-v24';
const SHELL = ['/', '/index.html', '/app.js', '/asr-progress.js', '/identity-selector.js', '/manifest.json', '/icon-192.png', '/icon-512.png'];
const SHELL_PATHS = new Set(SHELL);

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;
  // Intercept only the exact, credential-free app shell. Everything else,
  // including encoded variants of /api, /audio and /login, goes straight to
  // the network and can never enter Cache API.
  if (url.origin !== self.location.origin || url.search || !SHELL_PATHS.has(url.pathname)) return;
  // App shell: network-first, cache only as an offline fallback. Cache-first
  // kept serving a stale app.js after deploys, so the phone never saw new code.
  e.respondWith(
    fetch(e.request).then((resp) => {
      if (resp && resp.status === 200 && resp.type === 'basic') {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
      }
      return resp;
    }).catch(() => caches.match(e.request))
  );
});
