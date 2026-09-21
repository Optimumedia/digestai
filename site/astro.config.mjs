import { defineConfig } from "astro/config";
import sitemap from "@astrojs/sitemap";
import fs from "node:fs";
import path from "node:path";
import { published, noindexPaths, modelPages, JOB_SLUGS } from "./src/lib/indexing.mjs";
import { isRedirectPage, loadRedirects } from "./src/lib/redirects.mjs";

// Old addresses of merged stories are redirect pages: never listed for search engines.
const redirects = loadRedirects(path.resolve("src/data"));

// lastmod per URL from the pipeline export, so search engines re-crawl what actually changed, and
// the pages the templates mark noindex (indexing.mjs), which stay out of the sitemap as well.
const lastmod = new Map();
let noindex = new Set();
// Archive pages (stories older than the export window): listed with low priority.
const archivedPaths = new Set();
const liveStories = new Set();
try {
  for (const a of JSON.parse(fs.readFileSync(path.resolve("src/data/archive.json"), "utf-8"))) {
    archivedPaths.add(`/story/${a.slug}`);
    lastmod.set(`/story/${a.slug}`, a.updatedAt);
  }
} catch {}
try {
  const dataDir = path.resolve("src/data");
  const read = (name) => JSON.parse(fs.readFileSync(path.join(dataDir, name), "utf-8"));
  const stories = published(read("stories.json"));
  for (const s of stories) { lastmod.set(`/story/${s.slug}`, s.updatedAt); liveStories.add(`/story/${s.slug}`); }
  for (const t of read("threads.json")) lastmod.set(`/thread/${t.slug}`, t.updatedAt);
  let models = [];
  try { models = read("trackers.json").models || []; } catch {}
  const entities = read("entities.json");
  noindex = noindexPaths(stories, entities, models);
  // Model pages and job pages change when a story on them does.
  const updatedById = new Map(stories.map((s) => [s.id, s.updatedAt || ""]));
  const newest = (values) => values.filter(Boolean).sort().pop();
  for (const page of modelPages(stories, entities, models).values()) {
    const mod = newest([...page.storyIds].map((id) => updatedById.get(id)));
    if (mod) lastmod.set(`/models/${page.slug}`, mod);
  }
  for (const [key, slug] of Object.entries(JOB_SLUGS)) {
    const mod = newest(stories.filter((s) => s.workCard?.jobs?.includes(key)).map((s) => s.updatedAt));
    if (mod) lastmod.set(`/work/${slug}`, mod);
  }
} catch {}

/* Addresses from the previous site that still get search impressions. Astro writes each as a page
   with a meta refresh, a canonical link and a plain link to the new address, which is the only kind
   of redirect GitHub Pages can serve. Old article URLs (/article/<slug>) are handled by the 404
   page, which searches the site for the words in the slug. */
// The previous site's /article/ addresses still get Google impressions (14,659 since June 2025);
// each goes to the current page on the same subject. The list is generated from Search Console.
const legacyArticles = JSON.parse(fs.readFileSync(path.resolve("src/lib/legacy-article-redirects.json"), "utf-8"));
const legacyRedirects = {
  ...legacyArticles,
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
        // /subscribe is noindex until the Kit form is connected (subscribe.astro), and a real page after.
        if (p.endsWith("/subscribe") && !process.env.PUBLIC_KIT_FORM_URL) return false;
        return !/\/(search|admin|saved|river|offline)$/.test(p) && !noindex.has(p) && !(p in legacyRedirects) && !isRedirectPage(page, redirects);
      },
      serialize(item) {
        const p = new URL(item.url).pathname.replace(/\/$/, "");
        const mod = lastmod.get(p);
        if (mod) item.lastmod = mod;
        if (p === "" || p === "/today") { item.changefreq = "hourly"; item.priority = 1.0; }
        else if (p === "/work") { item.changefreq = "hourly"; item.priority = 0.9; }
        else if (p === "/work/tools") { item.changefreq = "daily"; item.priority = 0.8; }
        else if (Object.values(JOB_SLUGS).some((slug) => p === `/work/${slug}`)) { item.changefreq = "daily"; item.priority = 0.7; }
        else if (p.startsWith("/models/")) { item.changefreq = "daily"; item.priority = 0.6; }
        else if (p.startsWith("/work/week/")) { item.changefreq = "weekly"; item.priority = 0.5; }
        else if (archivedPaths.has(p) && !liveStories.has(p)) { item.changefreq = "yearly"; item.priority = 0.2; }
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
