import { defineConfig } from "astro/config";
import sitemap from "@astrojs/sitemap";
import fs from "node:fs";
import path from "node:path";
import { published, noindexPaths } from "./src/lib/indexing.mjs";
import { isRedirectPage, loadRedirects } from "./src/lib/redirects.mjs";

// Old addresses of merged stories are redirect pages: never listed for search engines.
const redirects = loadRedirects(path.resolve("src/data"));

// lastmod per URL from the pipeline export, so search engines re-crawl what actually changed, and
// the pages the templates mark noindex (indexing.mjs), which stay out of the sitemap as well.
const lastmod = new Map();
let noindex = new Set();
try {
  const dataDir = path.resolve("src/data");
  const read = (name) => JSON.parse(fs.readFileSync(path.join(dataDir, name), "utf-8"));
  const stories = published(read("stories.json"));
  for (const s of stories) lastmod.set(`/story/${s.slug}`, s.updatedAt);
  for (const t of read("threads.json")) lastmod.set(`/thread/${t.slug}`, t.updatedAt);
  noindex = noindexPaths(stories, read("entities.json"));
} catch {}

/* Addresses from the previous site that still get search impressions. Astro writes each as a page
   with a meta refresh, a canonical link and a plain link to the new address, which is the only kind
   of redirect GitHub Pages can serve. Old article URLs (/article/<slug>) are handled by the 404
   page, which searches the site for the words in the slug. */
const legacyRedirects = {
  "/category/generative-ai-model-breakthroughs": "/category/models",
  "/category/agentic-ai-autonomous-systems": "/category/agents",
  "/category/regulation-policy-ethics": "/category/policy",
  "/category/hardware-compute-infrastructure": "/category/hardware",
  "/category/enterprise-industry-adoption": "/category/enterprise",
  "/category/robotics-physical-ai": "/category/robotics",
  "/category/future-of-work-society": "/category/society",
  "/auth": "/",
  "/bookmarks": "/saved",
  "/profile": "/",
};

export default defineConfig({
  site: process.env.SITE_URL || "https://digestai.news",
  output: "static",
  trailingSlash: "never",
  build: { format: "file" },
  redirects: legacyRedirects,
  integrations: [
    sitemap({
      filter: (page) => {
        const p = new URL(page).pathname.replace(/\/$/, "");
        return !/\/(search|admin|saved|river|subscribe|offline)$/.test(p) && !noindex.has(p) && !(p in legacyRedirects) && !isRedirectPage(page, redirects);
      },
      serialize(item) {
        const p = new URL(item.url).pathname.replace(/\/$/, "");
        const mod = lastmod.get(p);
        if (mod) item.lastmod = mod;
        if (p === "" || p === "/today") { item.changefreq = "hourly"; item.priority = 1.0; }
        else if (p.startsWith("/story/")) { item.changefreq = "daily"; item.priority = 0.8; }
        else if (p.startsWith("/topic/") || p.startsWith("/thread/") || p === "/models" || p === "/funding") { item.changefreq = "daily"; item.priority = 0.7; }
        else if (p.startsWith("/category/")) { item.changefreq = "hourly"; item.priority = 0.7; }
        else if (p.startsWith("/daily/") || p.startsWith("/week/")) { item.changefreq = "weekly"; item.priority = 0.5; }
        else { item.changefreq = "weekly"; item.priority = 0.4; }
        return item;
      },
    }),
  ],
});
