import type { APIRoute } from "astro";
import { meta } from "../../lib/data";
import { workEpisodes } from "../../lib/work";

/* AI at Work's own podcast feed, separate from the daily news show at /podcast.xml.

   Apple Podcasts and Spotify both treat one feed as one show: the title, the artwork, the category
   and the cadence are set once for the whole feed, and listeners subscribe to that. The daily
   briefing is News > Tech News and arrives every morning; this is a weekly how-to for marketers and
   small teams, and belongs under Business. Putting it in the same feed would mean either mislabelling
   one of them or shipping it as a bonus episode into the inbox of people who subscribed to a news
   show. Two feeds cost nothing — /listen lists both — and each show says honestly what it is. */

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const hms = (s: number) => `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;

export const GET: APIRoute = () => {
  const site = meta.siteUrl;
  const items = workEpisodes.map((e) => `<item>
  <title>${esc(e.title)}</title>
  <link>${site}/listen#${e.week || e.date}</link>
  <guid isPermaLink="false">digestai-work-${e.week || e.date}</guid>
  <pubDate>${new Date(e.publishedAt).toUTCString()}</pubDate>
  <description>${esc(e.description)}</description>
  <content:encoded><![CDATA[<p>${esc(e.description)}</p><ul>${e.stories.map((s) => `<li><a href="${site}/story/${s.slug}">${esc(s.headline)}</a></li>`).join("")}</ul>${e.week ? `<p><a href="${site}/work/week/${e.week}">The week's playbook</a></p>` : ""}]]></content:encoded>
  <enclosure url="${e.url}" length="${e.bytes}" type="audio/mpeg" />
  <itunes:duration>${hms(e.seconds)}</itunes:duration>
  <itunes:episodeType>full</itunes:episodeType>
  <itunes:explicit>false</itunes:explicit>
</item>`);
  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>Digest AI: AI at Work</title>
  <link>${site}/work</link>
  <atom:link href="${site}/work/podcast.xml" rel="self" type="application/rss+xml" />
  <language>en</language>
  <description>A five-minute run through what changed for marketers and small teams this week: what each thing does, who it helps, what it costs, how long it takes and the one catch. Every Monday. The written version, with links, is at digestai.news/work.</description>
  <itunes:author>Digest AI</itunes:author>
  <itunes:summary>A five-minute run through what changed for marketers and small teams this week, every Monday.</itunes:summary>
  <itunes:owner><itunes:name>Digest AI</itunes:name><itunes:email>hello@digestai.news</itunes:email></itunes:owner>
  <itunes:image href="${site}/podcast-cover.png" />
  <image><url>${site}/podcast-cover.png</url><title>Digest AI: AI at Work</title><link>${site}/work</link></image>
  <itunes:category text="Business"><itunes:category text="Entrepreneurship" /></itunes:category>
  <itunes:category text="Business"><itunes:category text="Marketing" /></itunes:category>
  <itunes:category text="Technology" />
  <itunes:type>episodic</itunes:type>
  <itunes:explicit>false</itunes:explicit>
  ${items.join("\n  ")}
</channel>
</rss>`;
  return new Response(xml, { headers: { "Content-Type": "application/rss+xml; charset=utf-8" } });
};
