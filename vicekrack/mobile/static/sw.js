/* ViceKrack Mobile service worker: keeps the app's own files so it opens fast and can say "PC offline".
 * Only the app shell is stored. Live data (/m/api/...) is never stored: it always comes from the PC. */
"use strict";
const CACHE = "vicekrack-shell-v2";
const SHELL = ["/", "/static/app.css", "/static/app.js", "/manifest.webmanifest",
               "/static/icon-180.png", "/static/icon-192.png", "/static/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((names) => Promise.all(names.filter((n) => n !== CACHE).map((n) => caches.delete(n))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin || !SHELL.includes(url.pathname)) {
    return;                                               // live data and everything else: straight to the PC
  }
  event.respondWith(fetch(event.request)                  // newest version first, stored copy when offline
    .then((response) => {
      if (response.ok) { const copy = response.clone(); caches.open(CACHE).then((c) => c.put(url.pathname, copy)); }
      return response;
    })
    .catch(() => caches.match(url.pathname)));
});
