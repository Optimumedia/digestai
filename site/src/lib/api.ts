/* Machine-readable copies of the site for AI assistants, search tools and apps (/api documents them).
   Everything here is built from the exported JSON at build time: no database reads.

   Only our own text (headline, digest, key points, why it matters, source comparison) and links go
   in these files. Publishers' article text (contentMd) and their descriptions never do: the story
   page shows the lead article with attribution, but a copy meant for bulk reuse must not. */
import { categories, meta, stories, briefingStories, briefingAlso, storiesInCategory, storyModified, leadArticle, threadFor, firstSentence, formatDate, archived, type Story, type ArchivedStory } from "./data";

export const SITE = meta.siteUrl.replace(/\/$/, "");
export const API_VERSION = 1;
export const LICENSE =
  "Headlines, digests and key points are written by Digest AI and may be quoted with a link to the story page. Linked articles belong to their publishers. Terms: " +
  `${SITE}/terms#reuse`;
export const DOCS = `${SITE}/api`;

/** Fields every index file starts with. */
export function envelope(extra: Record<string, unknown> = {}) {
  return { version: API_VERSION, generatedAt: meta.generatedAt, docs: DOCS, license: LICENSE, ...extra };
}

export const storyUrl = (slug: string) => `${SITE}/story/${slug}`;
export const storyJsonUrl = (slug: string) => `${SITE}/story/${slug}.json`;
export const storyMdUrl = (slug: string) => `${SITE}/story/${slug}.md`;

/** "Digest AI, <headline>, <date>, <url>". */
export function citeText(headline: string, date: string | null | undefined, url: string): string {
  return `Digest AI, "${headline}", ${formatDate(date) || "undated"}, ${url}`;
}

function cite(headline: string, date: string | null | undefined, url: string) {
  return { text: citeText(headline, date, url), publisher: "Digest AI", title: headline, datePublished: date || null, url };
}

/** The lead article first, then the others in the order the story page lists them. */
function orderedSources(story: Story) {
  const lead = leadArticle(story);
  return [lead, ...story.articles.filter((a) => a.id !== lead.id)].filter(Boolean);
}

/** The full machine-readable story (/story/<slug>.json). */
export function storyJson(story: Story) {
  const url = storyUrl(story.slug);
  const thread = threadFor(story.threadId);
  const notes = story.sourceNotes && (story.sourceNotes.agree || story.sourceNotes.differ?.length) ? {
    agree: story.sourceNotes.agree || null,
    differ: story.sourceNotes.differ || [],
  } : null;
  return {
    version: API_VERSION,
    type: "story",
    url,
    json: storyJsonUrl(story.slug),
    markdown: storyMdUrl(story.slug),
    slug: story.slug,
    headline: story.headline,
    summary: story.summaryMd || null,
    keyPoints: story.keyPoints || [],
    whyItMatters: story.whyItMatters || null,
    category: story.category ? { slug: story.category, name: categories[story.category] || story.categoryName, url: `${SITE}/category/${story.category}` } : null,
    entities: story.entities || {},
    firstPublishedAt: story.firstPublishedAt,
    updatedAt: storyModified(story),
    sourceCount: story.articleCount,
    hasPrimarySource: story.hasPrimary,
    sources: orderedSources(story).map((a) => ({
      outlet: a.source || a.domain,
      title: a.title,
      url: a.url,
      publishedAt: a.publishedAt,
      type: a.sourceType,
      primary: a.sourceType === "primary",
      lead: a.id === story.leadArticleId,
    })),
    sourceNotes: notes,
    discussions: (story.discussions || []).map((d) => ({ site: d.site === "hn" ? "Hacker News" : "Reddit", url: d.url, points: d.points })),
    thread: thread ? { title: thread.title, url: `${SITE}/thread/${thread.slug}`, storyCount: thread.storyCount } : null,
    cite: cite(story.headline, story.firstPublishedAt, url),
    generatedBy: "Written by Digest AI's editorial model from the linked sources; the sources are the record.",
    license: LICENSE,
  };
}

/** An archived story (older than the export window): the fields its small page carries. */
export function archivedJson(a: ArchivedStory) {
  const url = storyUrl(a.slug);
  return {
    version: API_VERSION,
    type: "story",
    archived: true,
    url,
    json: storyJsonUrl(a.slug),
    markdown: storyMdUrl(a.slug),
    slug: a.slug,
    headline: a.headline,
    summary: a.summary || null,
    keyPoints: a.keyPoints || [],
    category: a.category ? { slug: a.category, name: categories[a.category] || a.categoryName, url: `${SITE}/category/${a.category}` } : null,
    firstPublishedAt: a.firstPublishedAt,
    updatedAt: a.updatedAt,
    sources: (a.sources || []).map((s) => ({ outlet: s.source, title: s.title, url: s.url })),
    cite: cite(a.headline, a.firstPublishedAt, url),
    license: LICENSE,
  };
}

type StoryDoc = ReturnType<typeof storyJson> | ReturnType<typeof archivedJson>;

/** The same story as plain Markdown (/story/<slug>.md), which many assistants read best. */
export function storyMarkdown(doc: StoryDoc): string {
  const out: string[] = [`# ${doc.headline}`, ""];
  const line = [`Digest AI`, doc.category?.name, doc.firstPublishedAt && `published ${doc.firstPublishedAt}`, doc.updatedAt && doc.updatedAt !== doc.firstPublishedAt && `updated ${doc.updatedAt}`].filter(Boolean);
  out.push(line.join(" · "), "", `Canonical: ${doc.url}`, "");
  if (doc.summary) out.push("## Summary", "", doc.summary.trim(), "");
  if (doc.keyPoints.length) out.push("## Key points", "", ...doc.keyPoints.map((k) => `- ${k}`), "");
  if ("whyItMatters" in doc && doc.whyItMatters) out.push("## Why it matters", "", doc.whyItMatters, "");
  if ("sourceNotes" in doc && doc.sourceNotes) {
    out.push("## How the reports compare", "");
    if (doc.sourceNotes.agree) out.push(`Agree: ${doc.sourceNotes.agree}`, "");
    if (doc.sourceNotes.differ.length) out.push("Differ:", ...doc.sourceNotes.differ.map((d) => `- ${d}`), "");
  }
  if (doc.sources.length) {
    out.push("## Sources", "");
    doc.sources.forEach((s, i) => {
      const bits = [s.outlet, "publishedAt" in s && s.publishedAt ? s.publishedAt.slice(0, 10) : null, "primary" in s && s.primary ? "primary source" : null].filter(Boolean);
      out.push(`${i + 1}. [${s.title.replace(/[[\]]/g, "")}](${s.url}) (${bits.join(", ")})`);
    });
    out.push("");
  }
  if ("thread" in doc && doc.thread) out.push(`Part of the developing story: [${doc.thread.title}](${doc.thread.url}) (${doc.thread.storyCount} stories)`, "");
  out.push("## Cite", "", doc.cite.text, "", "---", "", `Written by Digest AI's editorial model from the linked sources; the sources are the record. ${doc.license}`, `JSON: ${doc.json}`, "");
  return out.join("\n");
}

/** One row of an index file. */
export function storyItem(s: Story) {
  return {
    slug: s.slug,
    headline: s.headline,
    summary: firstSentence(s.summaryMd || s.keyPoints.join(" "), 240),
    category: s.category,
    firstPublishedAt: s.firstPublishedAt,
    updatedAt: storyModified(s),
    sourceCount: s.articleCount,
    hasPrimarySource: s.hasPrimary,
    url: storyUrl(s.slug),
    json: storyJsonUrl(s.slug),
  };
}

/** Newest first by first publication (never updatedAt, which moves when an old story gains a source). */
export function latestStories(limit = 100): Story[] {
  return [...stories]
    .sort((a, b) => Date.parse(b.firstPublishedAt || "0") - Date.parse(a.firstPublishedAt || "0"))
    .slice(0, limit);
}

export function briefingDoc(date: string) {
  return envelope({
    date,
    url: `${SITE}/today`,
    stories: briefingStories.map((s) => ({ ...storyItem(s), keyPoints: s.keyPoints, whyItMatters: s.whyItMatters })),
    also: briefingAlso.map(storyItem),
  });
}

export function categoryDoc(key: string, limit = 50) {
  return envelope({
    category: { slug: key, name: categories[key], url: `${SITE}/category/${key}` },
    stories: storiesInCategory(key)
      .sort((a, b) => Date.parse(b.firstPublishedAt || "0") - Date.parse(a.firstPublishedAt || "0"))
      .slice(0, limit)
      .map(storyItem),
  });
}

/** Every story that gets a machine-readable copy: live stories, then archived ones without a live page. */
export function storyDocs(): { slug: string; doc: StoryDoc }[] {
  const live = new Set(stories.map((s) => s.slug));
  return [
    ...stories.map((s) => ({ slug: s.slug, doc: storyJson(s) as StoryDoc })),
    ...archived.filter((a) => a.slug && !live.has(a.slug)).map((a) => ({ slug: a.slug, doc: archivedJson(a) as StoryDoc })),
  ];
}

export const json = (data: unknown) => new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json; charset=utf-8" } });
