import type { APIRoute } from "astro";
import { meta } from "../lib/data";

/* /sitemap.xml is registered in Search Console from the previous site (1,009 old URLs, 404 since
   the rebuild). Serving a sitemap index at the same address turns that registration into a path
   to the current sitemaps instead of a dead end. */
export const GET: APIRoute = () => {
  const site = meta.siteUrl;
  const now = new Date().toISOString();
  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>${site}/sitemap-0.xml</loc><lastmod>${now}</lastmod></sitemap>
  <sitemap><loc>${site}/news-sitemap.xml</loc><lastmod>${now}</lastmod></sitemap>
</sitemapindex>`;
  return new Response(xml, { headers: { "Content-Type": "application/xml; charset=utf-8" } });
};
