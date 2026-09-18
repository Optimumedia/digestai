import type { APIRoute } from "astro";
import { byRecency, meta, storyIndexable, storyImages } from "../lib/data";

// Google News sitemap: only indexable stories from the last 48 hours, max 1,000. Each entry carries
// the story's own share cards as image:image, the same absolute URLs as its og:image and NewsArticle
// image (data.ts storyImages); the site-wide default card is left out, since it says nothing about
// the story.
export const GET: APIRoute = () => {
  const site = meta.siteUrl;
  const cutoff = Date.now() - 48 * 3600 * 1000;
  const esc = (s: string) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&apos;");
  const urls = byRecency
    .filter((s) => storyIndexable(s) && Date.parse(s.firstPublishedAt || s.updatedAt || "0") > cutoff)
    .slice(0, 1000)
    .map((s) => {
      const images = storyImages(s)
        .map((i) => `\n  <image:image><image:loc>${esc(i.url)}</image:loc></image:image>`)
        .join("");
      return `<url>
  <loc>${esc(`${site}/story/${s.slug}`)}</loc>
  <news:news>
    <news:publication><news:name>Digest AI</news:name><news:language>en</news:language></news:publication>
    <news:publication_date>${s.firstPublishedAt || s.updatedAt}</news:publication_date>
    <news:title>${esc(s.headline)}</news:title>
  </news:news>${images}
</url>`;
    });
  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9" xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
${urls.join("\n")}
</urlset>`;
  return new Response(xml, { headers: { "Content-Type": "application/xml; charset=utf-8" } });
};
