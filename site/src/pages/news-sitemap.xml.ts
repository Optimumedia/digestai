import type { APIRoute } from "astro";
import { byRecency, meta } from "../lib/data";

// Google News sitemap: only stories from the last 48 hours, max 1,000.
export const GET: APIRoute = () => {
  const site = meta.siteUrl;
  const cutoff = Date.now() - 48 * 3600 * 1000;
  const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const urls = byRecency
    .filter((s) => Date.parse(s.firstPublishedAt || s.updatedAt || "0") > cutoff)
    .slice(0, 1000)
    .map(
      (s) => `<url>
  <loc>${site}/story/${s.slug}</loc>
  <news:news>
    <news:publication><news:name>Digest AI</news:name><news:language>en</news:language></news:publication>
    <news:publication_date>${s.firstPublishedAt || s.updatedAt}</news:publication_date>
    <news:title>${esc(s.headline)}</news:title>
  </news:news>
</url>`,
    );
  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
${urls.join("\n")}
</urlset>`;
  return new Response(xml, { headers: { "Content-Type": "application/xml; charset=utf-8" } });
};
