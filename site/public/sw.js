/* Digest AI service worker: offline reading and push alerts.
   - Pages: network first, falling back to the last copy seen (whatever ?ref= or utm_source= it was
     opened with, so the home-screen app opens today's briefing offline), then /offline.
   - Built assets, images and fonts: cache first (they are content-addressed or rarely change), and
     /app.js?v=<hash of its content> the same way, so a page opened offline still has its script.
   - Each cache keeps a bounded number of entries (LIMITS): the least recently stored go first, never
     the three pages stored at install (PINNED). Before, every page read and every stylesheet ever
     built stayed on the phone.
   - Push: shows the story as a notification; a tap opens it. */
const VERSION = "v1";
const PAGES = `pages-${VERSION}`;
const ASSETS = `assets-${VERSION}`;
const OFFLINE_URL = "/offline";
const LIMITS = { [PAGES]: 100, [ASSETS]: 40 };
const PINNED = [OFFLINE_URL, "/", "/today"];

// Cache keys come back in the order they were stored, and storing a request again moves it to the
// end, so the first keys are the least recently stored.
async function trim(name) {
  const cache = await caches.open(name);
  const keys = await cache.keys();
  let extra = keys.length - (LIMITS[name] || Infinity);
  for (const req of keys) {
    if (extra <= 0) break;
    const u = new URL(req.url);
    if (!u.search && PINNED.includes(u.pathname)) continue;
    await cache.delete(req);
    extra -= 1;
  }
}

function keep(name, req, res) {
  caches.open(name).then((c) => c.put(req, res)).then(() => trim(name)).catch(() => {});
}

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(PAGES).then((c) => c.addAll(PINNED).catch(() => {})).then(() => self.skipWaiting()));
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
        if (res.ok) keep(PAGES, req, res.clone());
        return res;
      }).catch(() => caches.match(req, { ignoreSearch: true }).then((hit) => hit || caches.match(OFFLINE_URL)))
    );
    return;
  }

  // A story's full article (/ft/<slug>.json) is fetched by the page: network first, and the last copy
  // kept, so a story read online still shows its article offline, as it did when the text was inline.
  if (url.pathname.startsWith("/ft/")) {
    event.respondWith(
      fetch(req).then((res) => {
        if (res.ok) keep(PAGES, req, res.clone());
        return res;
      }).catch(() => caches.match(req).then((hit) => hit || Response.error()))
    );
    return;
  }

  // app.js only when it carries its version tag: without one the same address can hold any version.
  const versionedApp = url.pathname === "/app.js" && url.searchParams.has("v");
  if (versionedApp || /^\/(_astro|og|fonts)\//.test(url.pathname) || /\.(png|svg|woff2?)$/.test(url.pathname)) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        if (res.ok) keep(ASSETS, req, res.clone());
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
