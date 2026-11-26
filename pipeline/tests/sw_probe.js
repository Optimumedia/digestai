/* Runs site/public/sw.js in Node against a stand-in for the service worker's world (Cache Storage,
   fetch, the fetch event) and prints what ends up in each cache as JSON, for test_site_speed.py.
   Usage: node sw_probe.js <path to sw.js> */
const fs = require("fs");
const vm = require("vm");

const ORIGIN = "https://digestai.news";
const code = fs.readFileSync(process.argv[2], "utf8");

// Cache Storage as the spec has it: keys in the order stored; storing a request again moves it last.
class FakeCache {
  constructor() { this.entries = []; }
  async put(req, res) {
    const url = typeof req === "string" ? new URL(req, ORIGIN).href : req.url;
    this.entries = this.entries.filter((e) => e.url !== url);
    this.entries.push({ url, res });
  }
  async addAll(urls) { for (const u of urls) await this.put(u, { ok: true, clone() { return this; } }); }
  async keys() { return this.entries.map((e) => ({ url: e.url })); }
  async delete(req) { const n = this.entries.length; this.entries = this.entries.filter((e) => e.url !== req.url); return this.entries.length < n; }
  async match(req) { const url = typeof req === "string" ? new URL(req, ORIGIN).href : req.url; const e = this.entries.find((x) => x.url === url); return e ? e.res : undefined; }
}
const stores = new Map();
const caches = {
  async open(name) { if (!stores.has(name)) stores.set(name, new FakeCache()); return stores.get(name); },
  async keys() { return [...stores.keys()]; },
  async delete(name) { return stores.delete(name); },
  async match(req) { for (const c of stores.values()) { const hit = await c.match(req); if (hit) return hit; } return undefined; },
};
let fetched = [];
const response = (url) => ({ ok: true, url, clone() { return response(url); } });
const listeners = {};
const self = {
  addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
  skipWaiting: async () => {},
  clients: { claim: async () => {} },
  registration: {},
};
const ctx = {
  self, caches, URL, Response: { error: () => ({ ok: false }) }, console, setTimeout, Promise,
  location: new URL(ORIGIN + "/sw.js"),
  fetch: async (req) => { const url = typeof req === "string" ? req : req.url; fetched.push(url); return response(url); },
};
ctx.addEventListener = self.addEventListener;
vm.runInNewContext(code, ctx, { filename: "sw.js" });

const settle = () => new Promise((r) => setImmediate(r));
async function dispatch(type, extra) {
  const waits = [];
  let responded = null;
  const ev = Object.assign({ waitUntil: (p) => waits.push(p), respondWith: (p) => { responded = p; } }, extra);
  for (const fn of listeners[type] || []) fn(ev);
  await Promise.all(waits);
  const res = responded ? await responded : null;
  for (let i = 0; i < 5; i++) await settle();
  return { handled: Boolean(responded), res };
}
const request = (path, mode = "no-cors") => ({ url: new URL(path, ORIGIN).href, method: "GET", mode });

(async () => {
  await dispatch("install");
  await dispatch("activate");
  const out = {};
  // 130 different pages read: the pages cache keeps the latest ones and the offline page.
  for (let i = 0; i < 130; i++) await dispatch("fetch", { request: request(`/story/s${i}`, "navigate") });
  const pageKeys = (await (await caches.open("pages-v1")).keys()).map((k) => new URL(k.url).pathname);
  out.pages = { count: pageKeys.length, hasOffline: pageKeys.includes("/offline"), hasPinned: ["/offline", "/", "/today"].every((p) => pageKeys.includes(p)), hasNewest: pageKeys.includes("/story/s129"), hasOldest: pageKeys.includes("/story/s0") };
  // app.js: cached when versioned (the second request is served from the cache), left alone otherwise.
  fetched = [];
  const a1 = await dispatch("fetch", { request: request("/app.js?v=abc123") });
  const a2 = await dispatch("fetch", { request: request("/app.js?v=abc123") });
  const bare = await dispatch("fetch", { request: request("/app.js") });
  out.app = { handled: a1.handled && a2.handled, networkFetches: fetched.filter((u) => u.includes("/app.js?v=")).length, bareHandled: bare.handled };
  // 60 different stylesheets over time: the assets cache stays bounded.
  for (let i = 0; i < 60; i++) await dispatch("fetch", { request: request(`/_astro/page.${i}.css`) });
  const assetKeys = (await (await caches.open("assets-v1")).keys()).map((k) => new URL(k.url).pathname);
  out.assets = { count: assetKeys.length, hasNewest: assetKeys.includes("/_astro/page.59.css") };
  // Another site's request is never touched.
  out.foreignHandled = (await dispatch("fetch", { request: { url: "https://giscus.app/client.js", method: "GET", mode: "no-cors" } })).handled;
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
