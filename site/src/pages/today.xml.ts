import type { APIRoute } from "astro";
import { briefing, briefingStories, episodes, meta, storyForSlug, storiesByDay, noindex, plain, formatDate, type Story } from "../lib/data";
import { fromSource } from "../lib/passon";

/* The daily briefing as a feed: one item a day with the day's top stories, for readers who want the
   day's few stories rather than every story (/rss.xml). Today's item is the live briefing from
   06:00 UTC (the hour the README uses: by then the briefing has settled on the day's news); earlier
   days come from the spoken briefings' story lists (episodes.json). Each story carries our own
   headline, why it matters and its number of sources, never publishers' text. Links carry
   utm_source=rss so a visit from a feed reader is not lost among direct ones. */
const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const DAYS = 14;
const SETTLED_HOUR_UTC = 6;

type Day = { date: string; stories: { slug: string; headline: string; story?: Story }[] };

export const GET: APIRoute = () => {
  const site = meta.siteUrl;
  const days: Day[] = [];
  const built = new Date(meta.generatedAt);
  if (briefingStories.length >= 3 && (briefing.date < built.toISOString().slice(0, 10) || built.getUTCHours() >= SETTLED_HOUR_UTC)) {
    days.push({ date: briefing.date, stories: briefingStories.map((s) => ({ slug: s.slug, headline: s.headline, story: s })) });
  }
  for (const e of episodes) {
    if (days.length >= DAYS) break;
    if (!e.date || days.some((d) => d.date === e.date) || e.stories.length < 3) continue;
    days.push({ date: e.date, stories: e.stories.map((s) => ({ slug: s.slug, headline: s.headline, story: storyForSlug(s.slug) })) });
  }
  days.sort((a, b) => (a.date < b.date ? 1 : -1));
  const byDay = storiesByDay();
  const items = days.map((d) => {
    const label = formatDate(d.date + "T12:00:00Z", { weekday: "long", day: "numeric", month: "long" });
    const page = byDay.has(d.date) && !noindex.has(`/daily/${d.date}`) ? `${site}/daily/${d.date}` : d.date === briefing.date ? `${site}/today` : `${site}/listen#${d.date}`;
    const list = d.stories.map(({ slug, headline, story }) => {
      const why = story?.whyItMatters ? `<br />${esc(plain(story.whyItMatters, 300))}` : "";
      const n = story ? ` <small>(${story.articleCount} ${story.articleCount === 1 ? "source" : "sources"})</small>` : "";
      return `<li><a href="${esc(fromSource(`${site}/story/${slug}`, "rss"))}">${esc(headline)}</a>${n}${why}</li>`;
    }).join("");
    return `<item>
  <title>${esc(`AI news, ${label}: ${d.stories[0].headline}`)}</title>
  <link>${esc(fromSource(page, "rss"))}</link>
  <guid isPermaLink="false">digestai-daily-${d.date}</guid>
  <pubDate>${new Date(`${d.date}T${String(SETTLED_HOUR_UTC).padStart(2, "0")}:00:00Z`).toUTCString()}</pubDate>
  <description>${esc(d.stories.map((s) => s.headline).join(" · "))}</description>
  <content:encoded><![CDATA[<p>The AI stories that mattered on ${esc(label)}, each with every source linked on Digest AI.</p><ol>${list}</ol>]]></content:encoded>
</item>`;
  });
  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>Digest AI: the daily briefing</title>
  <link>${site}/today</link>
  <atom:link href="${site}/today.xml" rel="self" type="application/rss+xml" />
  <description>One post a day: the AI stories that matter, each with every source linked. Every story as it is published is in ${site}/rss.xml.</description>
  <language>en</language>
  <ttl>60</ttl>
  <lastBuildDate>${built.toUTCString()}</lastBuildDate>
  ${items.join("\n  ")}
</channel>
</rss>`;
  return new Response(xml, { headers: { "Content-Type": "application/rss+xml; charset=utf-8" } });
};
