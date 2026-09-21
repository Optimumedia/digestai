/* Digest AI service worker: offline reading and push alerts.
   - Pages: network first, falling back to the last copy seen, then /offline.
   - Built assets, images and fonts: cache first (they are content-addressed or rarely change).
   - Push: shows the story as a notification; a tap opens it. */
const VERSION = "v1";
const PAGES = `pages-${VERSION}`;
const ASSETS = `assets-${VERSION}`;
const OFFLINE_URL = "/offline";

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(PAGES).then((c) => c.addAll([OFFLINE_URL, "/"]).catch(() => {})).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => ![PAGES, ASSETS].includes(k)).map((k) => caches.delete(k)))).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).then((res) => {
        if (res.ok) caches.open(PAGES).then((c) => c.put(req, res.clone())).catch(() => {});
        return res;
      }).catch(() => caches.match(req).then((hit) => hit || caches.match(OFFLINE_URL)))
    );
    return;
  }

  // A story's full article (/ft/<slug>.json) is fetched by the page: network first, and the last copy
  // kept, so a story read online still shows its article offline, as it did when the text was inline.
  if (url.pathname.startsWith("/ft/")) {
    event.respondWith(
      fetch(req).then((res) => {
        if (res.ok) caches.open(PAGES).then((c) => c.put(req, res.clone())).catch(() => {});
        return res;
      }).catch(() => caches.match(req).then((hit) => hit || Response.error()))
    );
    return;
  }

  if (/^\/(_astro|og|fonts)\//.test(url.pathname) || /\.(png|svg|woff2?|webmanifest)$/.test(url.pathname)) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        if (res.ok) caches.open(ASSETS).then((c) => c.put(req, res.clone())).catch(() => {});
        return res;
      }))
    );
  }
});

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { title: "Digest AI", body: event.data ? event.data.text() : "" }; }
  const title = data.title || "Digest AI";
  const options = {
    body: data.body || "",
    icon: data.icon || "/logo-192.png",
    badge: "/logo-192.png",
    image: data.image,
    tag: data.tag || "digestai",
    data: { url: data.url || "/" },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ("focus" in client) { client.navigate(url); return client.focus(); }
      }
      return self.clients.openWindow(url);
    })
  );
});
