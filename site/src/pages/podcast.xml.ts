import type { APIRoute } from "astro";
import { episodes, meta } from "../lib/data";

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const hms = (s: number) => `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;

export const GET: APIRoute = () => {
  const site = meta.siteUrl;
  const items = episodes.map((e) => `<item>
  <title>${esc(e.title)}</title>
  <link>${site}/listen#${e.date}</link>
  <guid isPermaLink="false">digestai-briefing-${e.date}</guid>
  <pubDate>${new Date(e.publishedAt).toUTCString()}</pubDate>
  <description>${esc(e.description)}</description>
  <content:encoded><![CDATA[<p>${esc(e.description)}</p><ul>${e.stories.map((s) => `<li><a href="${site}/story/${s.slug}">${esc(s.headline)}</a></li>`).join("")}</ul>]]></content:encoded>
  <enclosure url="${e.url}" length="${e.bytes}" type="audio/mpeg" />
  <itunes:duration>${hms(e.seconds)}</itunes:duration>
  <itunes:episodeType>full</itunes:episodeType>
  <itunes:explicit>false</itunes:explicit>
</item>`);
  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>Digest AI briefing</title>
  <link>${site}/listen</link>
  <atom:link href="${site}/podcast.xml" rel="self" type="application/rss+xml" />
  <language>en</language>
  <description>The five AI stories that matter today, read in about five minutes every morning. Every story is sourced and linked on digestai.news, which is updated every 30 minutes.</description>
  <itunes:author>Digest AI</itunes:author>
  <itunes:summary>The five AI stories that matter today, read in about five minutes every morning.</itunes:summary>
  <itunes:owner><itunes:name>Digest AI</itunes:name></itunes:owner>
  <itunes:image href="${site}/podcast-cover.png" />
  <image><url>${site}/podcast-cover.png</url><title>Digest AI briefing</title><link>${site}/listen</link></image>
  <itunes:category text="News"><itunes:category text="Tech News" /></itunes:category>
  <itunes:category text="Technology" />
  <itunes:type>episodic</itunes:type>
  <itunes:explicit>false</itunes:explicit>
  ${items.join("\n  ")}
</channel>
</rss>`;
  return new Response(xml, { headers: { "Content-Type": "application/rss+xml; charset=utf-8" } });
};
