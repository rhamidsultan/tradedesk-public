// Minimal service worker: caches the app shell so it opens offline; API calls always go to the network.
const CACHE = 'td-shell-v2';
self.addEventListener('install', (e) => { self.skipWaiting(); e.waitUntil(caches.open(CACHE).then((c) => c.addAll(['/', '/manifest.webmanifest', '/icon.svg']))); });
self.addEventListener('activate', (e) => { e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))); self.clients.claim(); });
self.addEventListener('fetch', (e) => {
  const u = new URL(e.request.url);
  if (u.pathname.startsWith('/api/') || e.request.method !== 'GET') return;
  e.respondWith(fetch(e.request).then((r) => { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); return r; })
    .catch(() => caches.match(e.request).then((r) => r || caches.match('/'))));
});
self.addEventListener('push', (e) => {
  let d = {}; try { d = e.data.json() } catch { d = { title: 'TradeDesk', body: e.data ? e.data.text() : '' } }
  e.waitUntil(self.registration.showNotification(d.title || 'TradeDesk', { body: d.body, data: { url: d.url || '/' }, icon: '/icon-192.png', badge: '/icon-192.png' }));
});
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: 'window' }).then((cs) => { const u = e.notification.data?.url || '/'; for (const c of cs) { if ('focus' in c) { c.navigate(u); return c.focus() } } return self.clients.openWindow(u) }));
});
