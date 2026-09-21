import type { APIRoute } from "astro";
import { byRecency, plain, meta, categories } from "../lib/data";

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

export const GET: APIRoute = () => {
  const site = meta.siteUrl;
  const items = byRecency.slice(0, 60).map((s) => {
    const link = `${site}/story/${s.slug}`;
    const sources = s.articles.map((a) => `<a href="${esc(a.url)}">${esc(a.source || a.domain)}</a>`).join(", ");
    return `<item>
  <title>${esc(s.headline)}</title>
  <link>${link}</link>
  <guid isPermaLink="true">${link}</guid>
  <pubDate>${new Date(s.firstPublishedAt || s.updatedAt || Date.now()).toUTCString()}</pubDate>
  <category>${esc(categories[s.category || ""] || "AI")}</category>
  <description>${esc(plain(s.summaryMd, 400))}</description>
  <content:encoded><![CDATA[${s.keyPoints.length ? `<ul>${s.keyPoints.map((k) => `<li>${esc(k)}</li>`).join("")}</ul>` : ""}<p>${esc(plain(s.summaryMd, 1200))}</p><p>Sources: ${sources}</p>]]></content:encoded>
</item>`;
  });
  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>Digest AI</title>
  <link>${site}</link>
  <atom:link href="${site}/rss.xml" rel="self" type="application/rss+xml" />
  <description>Cut through the AI noise: every important AI story with its sources, updated every hour.</description>
  <language>en</language>
  <lastBuildDate>${new Date(meta.generatedAt).toUTCString()}</lastBuildDate>
  ${items.join("\n")}
</channel>
</rss>`;
  return new Response(xml, { headers: { "Content-Type": "application/rss+xml; charset=utf-8" } });
};
