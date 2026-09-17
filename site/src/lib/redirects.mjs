// Stories merged into another one (pipeline/digest/merge.py) keep their address: the export writes
// redirects.json ([{from, to}] story slugs), the story route builds a small redirect page at each old
// slug, and the sitemap leaves them out. Shared by astro.config.mjs and src/lib/data.ts.
import fs from "node:fs";
import path from "node:path";

/** Redirects whose old slug is not also a live story (a live page always wins). */
export function loadRedirects(dataDir) {
  const read = (name, fallback) => {
    try {
      return JSON.parse(fs.readFileSync(path.join(dataDir, name), "utf-8"));
    } catch {
      return fallback;
    }
  };
  const live = new Set(read("stories.json", []).map((s) => s.slug));
  return read("redirects.json", []).filter((r) => r && r.from && r.to && r.from !== r.to && !live.has(r.from) && live.has(r.to));
}

/** Whether a sitemap URL is one of the redirect pages. */
export function isRedirectPage(url, redirects) {
  const p = new URL(url).pathname.replace(/\.html$/, "").replace(/\/$/, "");
  return p.startsWith("/story/") && redirects.some((r) => `/story/${r.from}` === p);
}
