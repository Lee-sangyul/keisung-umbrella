/* Keisung Umbrella PWA service worker.
   Intentionally network-only: no student, admin, session or API responses are cached. */
self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});
self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});
