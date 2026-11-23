/* Passing a story on, and coming back to the site, without accounts.

   A link a reader passes on carries ?ref=<channel> (whatsapp, email, copy, linkedin, ...). app.js
   files a visit that arrives with it under the source "passed-on-<channel>", which the admin's
   Sources list shows as "Passed on by readers" (pipeline/digest/admin.py source_name): links sent in
   messaging apps and e-mail arrive with no referrer and would otherwise be lost among direct visits.
   The tag names only the channel, never the reader. Feeds, the embed and the home-screen app use
   utm_source the same way (rss, podcast, embed, pwa). */
import { meta, type Story } from "./data";

export const REF_CHANNELS = ["whatsapp", "telegram", "email", "copy", "linkedin", "x", "bluesky", "native", "text"] as const;
export type RefChannel = (typeof REF_CHANNELS)[number];

/** The address with the channel it was passed on through. */
export function passedOn(url: string, channel: RefChannel): string {
  return `${url}${url.includes("?") ? "&" : "?"}ref=${channel}`;
}

/** A link from one of our own feeds or widgets, so a visit from it is not lost among direct ones. */
export function fromSource(url: string, source: "rss" | "podcast" | "embed" | "pwa"): string {
  return `${url}${url.includes("?") ? "&" : "?"}utm_source=${source}`;
}

export const storyUrl = (s: { slug: string }): string => `${meta.siteUrl}/story/${s.slug}`;

/** One sentence that says why the story is worth a click, for messages: why it matters, else the first key point. */
export function shareLine(s: Pick<Story, "whyItMatters" | "keyPoints">): string {
  const text = (s.whyItMatters || s.keyPoints[0] || "").replace(/\s+/g, " ").trim();
  const first = text.match(/^.+?[.!?](?=\s|$)/);
  return (first ? first[0] : text).slice(0, 240);
}

/** The prepared links of the "Pass it on" row: they work without JavaScript. */
export function shareLinks(url: string, title: string, line: string) {
  const enc = encodeURIComponent;
  const sign = "From Digest AI: one story, every source.";
  return {
    whatsapp: `https://wa.me/?text=${enc(`${title}\n\n${line ? `${line}\n\n` : ""}${passedOn(url, "whatsapp")}`)}`,
    telegram: `https://t.me/share/url?url=${enc(passedOn(url, "telegram"))}&text=${enc(title)}`,
    email: `mailto:?subject=${enc(title)}&body=${enc(`${line ? `${line}\n\n` : ""}${passedOn(url, "email")}\n\n${sign}`)}`,
  };
}

/** The daily briefing's feed and the one-click ways to add it to a reader. */
export const dailyFeed = `${meta.siteUrl}/today.xml`;
export const feedReaders = (feed: string) => [
  { name: "Feedly", href: `https://feedly.com/i/subscription/feed/${encodeURIComponent(feed)}` },
  { name: "Inoreader", href: `https://www.inoreader.com/feed/${encodeURIComponent(feed)}` },
];

/** Podcast apps that open a show from its feed address, with no listing in their directory needed. */
export function podcastApps(feed: string, title: string) {
  const bare = feed.replace(/^https?:\/\//, "");
  return [
    { name: "Apple Podcasts", href: `podcast://${bare}` },
    { name: "Pocket Casts", href: `pktc://subscribe/${bare}` },
    { name: "Overcast", href: `overcast://x-callback-url/add?url=${encodeURIComponent(feed)}` },
    // AntennaPod's own web link: opens the app when it is installed, else a page that explains how to add the feed.
    { name: "AntennaPod", href: `https://antennapod.org/deeplink/subscribe?url=${encodeURIComponent(feed)}&title=${encodeURIComponent(title)}` },
  ];
}
