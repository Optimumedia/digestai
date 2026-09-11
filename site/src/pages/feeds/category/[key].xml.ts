import type { APIRoute } from "astro";
import { categories, storiesInCategory, plain, meta } from "../../../lib/data";

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

export function getStaticPaths() {
  return Object.keys(categories).map((key) => ({ params: { key } }));
}

export const GET: APIRoute = ({ params }) => {
  const key = params.key!;
  const site = meta.siteUrl;
  const items = storiesInCategory(key).slice(0, 40).map((s) => {
    const link = `${site}/story/${s.slug}`;
    return `<item><title>${esc(s.headline)}</title><link>${link}</link><guid isPermaLink="true">${link}</guid><pubDate>${new Date(s.firstPublishedAt || s.updatedAt || Date.now()).toUTCString()}</pubDate><description>${esc(plain(s.summaryMd, 400))}</description></item>`;
  });
  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel><title>Digest AI: ${esc(categories[key])}</title><link>${site}/category/${key}</link><atom:link href="${site}/feeds/category/${key}.xml" rel="self" type="application/rss+xml" /><description>${esc(categories[key])} stories from Digest AI, with sources.</description><language>en</language>${items.join("")}</channel></rss>`;
  return new Response(xml, { headers: { "Content-Type": "application/rss+xml; charset=utf-8" } });
};
