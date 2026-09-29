/* Hors ligne : l'appli s'ouvre même sans réseau, avec la dernière liste connue. */
const CACHE = "radar-v1";
const SHELL = ["./", "index.html", "style.css", "app.js", "manifest.webmanifest", "icons/icon.svg", "icons/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  // Données et page : réseau d'abord, copie locale si pas de réseau
  if (url.origin === location.origin) {
    const key = url.pathname.endsWith("events.json") ? new Request(url.origin + url.pathname) : e.request;
    e.respondWith(
      fetch(e.request).then((r) => {
        if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(key, copy)); }
        return r;
      }).catch(() => caches.match(key).then((r) => r || caches.match("index.html")))
    );
  }
});
