/* Plain-text copies of the answer pages for AI assistants: /today.md, /models/latest.md,
   /api/models.json and /llms-full.txt. Built from the export at build time, no database read.

   Like every file in api.ts: our own text (headlines, digests, key points) and links only, never a
   publisher's article text or description. Dates are written out, because an assistant quoting a file
   needs to say when the answer was true. */
import { briefing, briefingStories, briefingAlso, latestByLab, modelReleases, releaseMonths, monthName, trackers, storiesByWeek, weekKey, formatDate, firstSentence, meta, money, dealAmount, type Release } from "./data";
import { SITE, LICENSE, latestStories, storyUrl, envelope } from "./api";

const day = (iso: string | null | undefined) => formatDate(iso) || "undated";
const asOf = () => formatDate(meta.generatedAt);

function modelLine(r: Release): string {
  const page = `${SITE}/models/${r.slug}`;
  const story = r.storySlug ? `; launch story: ${storyUrl(r.storySlug)}` : "";
  return `- ${r.lab}: ${r.name}, first reported ${day(r.date)} (${page}${story})`;
}

/** /today.md: the briefing, dated, with each story's key points and address. */
export function todayMarkdown(): string {
  const date = formatDate(briefing.date + "T00:00:00Z");
  const out = [
    `# AI news today, ${date}`,
    "",
    `Digest AI's daily briefing: the AI stories that matter on ${date}, picked from ${briefing.stats.stories} stories and ${briefing.stats.articles} articles. Updated ${formatDate(briefing.generatedAt || meta.generatedAt)} ${new Date(briefing.generatedAt || meta.generatedAt).toISOString().slice(11, 16)} UTC.`,
    "",
    `Page: ${SITE}/today. Permanent page for this day: ${SITE}/daily/${briefing.date}`,
    "",
  ];
  briefingStories.forEach((s, i) => {
    out.push(`## ${i + 1}. ${s.headline}`, "", `${storyUrl(s.slug)} · first published ${day(s.firstPublishedAt)} · ${s.articleCount} ${s.articleCount === 1 ? "source" : "sources"}${s.hasPrimary ? ", including the primary source" : ""}`, "");
    if (s.keyPoints.length) out.push(...s.keyPoints.map((k) => `- ${k}`), "");
    if (s.whyItMatters) out.push(`Why it matters: ${s.whyItMatters}`, "");
  });
  if (briefingAlso.length) {
    out.push("## Also today", "", ...briefingAlso.map((s) => `- ${s.headline}: ${storyUrl(s.slug)}`), "");
  }
  out.push("---", "", `Written by Digest AI's editorial model from the linked sources; each story page lists them. ${LICENSE}`, "");
  return out.join("\n");
}

/** /api/models.json: the newest model from each lab and every launch the answers may state. */
export function modelsDoc() {
  const item = (r: Release) => ({
    name: r.name,
    lab: r.lab,
    type: r.kind,
    availability: r.availability,
    license: r.license || null,
    contextWindow: r.context || null,
    firstReportedAt: r.date || null,
    confirmed: r.confirmed,
    url: `${SITE}/models/${r.slug}`,
    officialPage: r.link || null,
    launchStory: r.storySlug ? { headline: r.storyHeadline || null, url: storyUrl(r.storySlug) } : null,
  });
  return envelope({
    asOf: meta.generatedAt,
    url: `${SITE}/models/latest`,
    notes: "One row per model, at the date it was first reported. Rumours, plans, apps and stories that only use a model are left out; confirmed means the lab's own announcement or two publishers.",
    newestByLab: latestByLab.map(item),
    months: [...releaseMonths.entries()].map(([key, rows]) => ({ month: key, url: `${SITE}/models/released/${key}`, releases: rows.length })),
    releases: modelReleases.slice(0, 150).map(item),
  });
}

/** /api/funding.json: every deal the funding tracker has listed, newest first. */
export function fundingDoc() {
  const rows = trackers.funding;
  return envelope({
    asOf: meta.generatedAt,
    url: `${SITE}/funding`,
    notes: "One row per deal, at the date it was first reported, listed when its story's headline reports it as done. Plans, filings, talks, results and contracts are left out; an acquisition is listed under the company bought, with the buyer first among the investors. amountUsd is the figure the sources state, converted approximately when amountStated gives it in another currency.",
    deals: rows.map((r) => ({
      company: r.company,
      amountUsd: r.amount_usd || null,
      amountStated: r.amount_stated || null,
      round: r.round,
      valuationUsd: r.valuation_usd || null,
      investors: r.investors || [],
      firstReportedAt: r.date || null,
      sources: r.sources || null,
      story: r.storySlug ? { headline: r.storyHeadline || null, url: storyUrl(r.storySlug) } : null,
    })),
  });
}

/** /models/latest.md */
export function modelsMarkdown(): string {
  const out = [
    `# Latest AI models, ${asOf()}`,
    "",
    `The newest confirmed model launch from each of ${latestByLab.length} labs, newest first, with the date it was first reported. Confirmed means the lab's own announcement or two publishers; rumours, plans, apps and stories that only use a model are left out. From Digest AI's model tracker, taken from the coverage, not from our own testing.`,
    "",
    `Page: ${SITE}/models/latest`,
    "",
    ...latestByLab.map(modelLine),
    "",
    "## Releases by month",
    "",
    ...[...releaseMonths.entries()].map(([key, rows]) => `- ${monthName(key)}: ${rows.length} releases, ${SITE}/models/released/${key}`),
    "",
    "---",
    "",
    LICENSE,
    "",
  ];
  return out.join("\n");
}

/** /llms-full.txt: what Digest AI is, where each kind of answer lives, and the current answers. */
export function llmsFull(): string {
  const now = asOf();
  const weekList = storiesByWeek().get(weekKey(meta.generatedAt)) || [];
  const deals = trackers.funding
    .filter((r) => r.amount_usd && Date.parse(r.date || "0") >= Date.parse(meta.generatedAt) - 30 * 864e5)
    .sort((a, b) => (b.amount_usd || 0) - (a.amount_usd || 0))
    .slice(0, 10);
  const out = [
    "# Digest AI (digestai.news), full index",
    "",
    `> An automated AI news desk. It reads about 80 sources every 20 minutes, merges the outlets that report the same event into one story, writes a short plain-English digest and links every source. Not affiliated with other products named DigestAI. Generated ${now}; the site and this file are rebuilt about every 20 minutes.`,
    "",
    `Short index: ${SITE}/llms.txt. About and editorial standards: ${SITE}/about. Contact: hello@digestai.news.`,
    "",
    "## Which page answers which question",
    "",
    `- What is the AI news today? ${SITE}/today (dated; Markdown: ${SITE}/today.md). A fixed page per day: ${SITE}/daily/<YYYY-MM-DD>`,
    `- What happened in AI this week? ${SITE}/week; a fixed page per week: ${SITE}/week/<YYYY-Www>`,
    `- What is the newest AI model, or the newest model from a lab? ${SITE}/models/latest (Markdown: ${SITE}/models/latest.md, JSON: ${SITE}/api/models.json)`,
    `- Which AI models came out in a given month? ${SITE}/models/released/<YYYY-MM>`,
    `- When was a model released, who makes it, its licence and context window? ${SITE}/models/<model-slug>`,
    `- What is the latest news about a company or person? ${SITE}/topic/<slug>, for example ${SITE}/topic/openai`,
    `- What are the biggest AI funding rounds? ${SITE}/funding`,
    `- How can a small business or marketer use AI? ${SITE}/work`,
    `- Any single story, with every source: ${SITE}/story/<slug> (also .md and .json)`,
    "",
    `## Today's briefing, ${formatDate(briefing.date + "T00:00:00Z")}`,
    "",
    ...briefingStories.map((s) => `- ${s.headline}. ${firstSentence(s.summaryMd || s.keyPoints[0] || "", 220)} ${storyUrl(s.slug)}`),
    "",
    `## Newest model from each lab, as of ${now}`,
    "",
    ...latestByLab.map(modelLine),
    "",
  ];
  if (weekList.length) {
    out.push(`## Most important stories this week (${weekList.length} published since Monday)`, "", ...weekList.slice(0, 10).map((s) => `- ${s.headline} (${day(s.firstPublishedAt)}): ${storyUrl(s.slug)}`), "");
  }
  if (deals.length) {
    out.push(`## Largest AI deals reported in the 30 days to ${now}`, "", ...deals.map((r) => `- ${r.company}: ${dealAmount(r)} (${r.round.replace(/_/g, " ")}), ${day(r.date)}${r.storySlug ? `: ${storyUrl(r.storySlug)}` : ""}`), "");
  }
  out.push(
    "## Newest stories",
    "",
    ...latestStories(60).map((s) => `- ${s.headline} (${day(s.firstPublishedAt)}, ${s.articleCount} ${s.articleCount === 1 ? "source" : "sources"}): ${storyUrl(s.slug)}`),
    "",
    "## How to cite",
    "",
    `Cite the story page and the outlets it links. Form: Digest AI, "<headline>", <date first published>, ${SITE}/story/<slug>. Each story's .json carries it ready in cite.text. Digests are written by Digest AI's editorial model from the listed sources, which remain the record.`,
    "",
    LICENSE,
    "",
  );
  return out.join("\n");
}

export const text = (body: string, type = "text/plain") => new Response(body, { headers: { "Content-Type": `${type}; charset=utf-8` } });
