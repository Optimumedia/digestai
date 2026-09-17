import { defineConfig } from "astro/config";
import sitemap from "@astrojs/sitemap";
import fs from "node:fs";
import path from "node:path";

// lastmod per URL from the pipeline export, so search engines re-crawl what actually changed.
const lastmod = new Map();
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
  for (const s of JSON.parse(fs.readFileSync(path.join(dataDir, "stories.json"), "utf-8"))) { lastmod.set(`/story/${s.slug}`, s.updatedAt); liveStories.add(`/story/${s.slug}`); }
  for (const t of JSON.parse(fs.readFileSync(path.join(dataDir, "threads.json"), "utf-8"))) lastmod.set(`/thread/${t.slug}`, t.updatedAt);
} catch {}

export default defineConfig({
  site: process.env.SITE_URL || "https://digestai.news",
  output: "static",
  trailingSlash: "never",
  build: { format: "file" },
  integrations: [
    sitemap({
      filter: (page) => !/\/(search|admin|saved|river|subscribe|offline)$/.test(page.replace(/\/$/, "")),
      serialize(item) {
        const p = new URL(item.url).pathname.replace(/\/$/, "");
        const mod = lastmod.get(p);
        if (mod) item.lastmod = mod;
        if (p === "" || p === "/today") { item.changefreq = "hourly"; item.priority = 1.0; }
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
