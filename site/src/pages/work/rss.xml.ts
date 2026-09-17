import type { APIRoute } from "astro";
import { meta } from "../../lib/data";
import { SECTION_TAGLINE, sectionStories, whoLine } from "../../lib/work";

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

/* The section's own feed: the practical card, not the news digest. A reader subscribed here gets
   what the thing does, what it costs, how long it takes and the catch, in the item itself. */
export const GET: APIRoute = () => {
  const site = meta.siteUrl;
  const items = sectionStories.slice(0, 50).map((s) => {
    const c = s.workCard;
    const link = `${site}/story/${s.slug}`;
    const facts = [
      `<p><b>${esc(c.tool)}</b>${c.maker ? ` by ${esc(c.maker)}` : ""}: ${esc(c.whatItDoes)}</p>`,
      `<p>For ${esc(whoLine(c.whoFor))}. ${esc(c.cost)}${c.effort ? `, ${esc(c.effort)}` : ""}.</p>`,
      c.useFor.length ? `<p>Use it for:</p><ul>${c.useFor.map((u) => `<li>${esc(u)}</li>`).join("")}</ul>` : "",
      `<p><b>Watch out:</b> ${esc(c.watchOut)}</p>`,
      c.link ? `<p><a href="${esc(c.link)}">${esc(c.tool)} official page</a></p>` : "",
      `<p><a href="${link}">What happened, with sources</a></p>`,
    ].join("");
    return `<item>
  <title>${esc(`${c.tool}: ${c.whatItDoes}`)}</title>
  <link>${link}</link>
  <guid isPermaLink="true">${link}</guid>
  <pubDate>${new Date(s.firstPublishedAt || s.updatedAt || Date.now()).toUTCString()}</pubDate>
  <category>AI at Work</category>
  <description>${esc(`${c.whatItDoes} For ${whoLine(c.whoFor)}. ${c.cost}${c.effort ? `, ${c.effort}` : ""}. Watch out: ${c.watchOut}`)}</description>
  <content:encoded><![CDATA[${facts}]]></content:encoded>
</item>`;
  });
  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
  <title>Digest AI: AI at Work</title>
  <link>${site}/work</link>
  <atom:link href="${site}/work/rss.xml" rel="self" type="application/rss+xml" />
  <description>${esc(SECTION_TAGLINE)} Every item with what it does, what it costs and the catch.</description>
  <language>en</language>
  <lastBuildDate>${new Date(meta.generatedAt).toUTCString()}</lastBuildDate>
  ${items.join("\n")}
</channel>
</rss>`;
  return new Response(xml, { headers: { "Content-Type": "application/rss+xml; charset=utf-8" } });
};
