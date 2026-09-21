/* AI at Work (/work): the section's own data.

   The pipeline (pipeline/digest/work.py) writes work.json and work-briefing.json beside the rest of
   the export. A story belongs to the section when the summary model filled a practical card for it,
   which is broader than the "marketing" category: a Google Ads change files under marketing, a new
   free transcription tool may file under agents, and both are things a small team can use.

   Everything here falls back to empty, so the site builds before the first card exists. */
import fs from "node:fs";
import path from "node:path";
import { stories, storyFor, type Episode, type Story } from "./data";
import { JOB_SLUGS, jobCounts, jobIndexable } from "./indexing.mjs";

export interface WorkCard {
  tool: string;
  maker: string | null;
  whatItDoes: string;
  whoFor: string[];
  useFor: string[];
  cost: string;
  costKind: "free" | "free tier" | "included" | "paid" | "unknown";
  effort: "minutes" | "an afternoon" | "needs a developer" | null;
  watchOut: string;
  link: string | null;
  jobs: string[];
  /** Why this is not a this-week job (a waitlist, a beta, a developer), or null. */
  skip: string | null;
  /** Who cannot use it, from the caveat: "not in the EEA, UK", "Business plan and above". Older
      exports have no such field. */
  limits?: string[];
  usefulness: number;
  /** What the reader gets, in plain words ("Turn 20 customer reviews into three ad angles"). The
      pipeline writes the tool plus what it does when the article gave no outcome (work.py,
      fallback_headline); cardTitle() below tells the two apart. Older exports have no such field. */
  headline?: string;
  /** The plan it already comes with ("Google Workspace Business Standard"), or "". */
  includedIn?: string;
  /* The expanded card's blocks (components/WorkEntry.astro). Each block renders only when its
     field is there; the pipeline writes them only when the article gives them (work.py, card_out). */
  /** What the owner gets, in one or two plain sentences written to "you" ("Get a week of social
      posts drafted in one sitting"). The model's line when it passed the article checks, else one
      the pipeline builds from the card's own fields (work.py, fallback_you_get), so every card from
      the export that added it has one. Older exports have no such field. */
  youGet?: string;
  /** How to do it, one short step each. */
  steps?: string[];
  /** Where the steps came from: the article, or the maker's own page (pipeline/digest/howto.py).
      Present whenever `steps` is. */
  stepsSource?: "article" | "maker";
  /** A prompt a reader can copy into the tool as it is. */
  prompt?: string;
  /** What the work looked like before and after, in a sentence or two each. */
  example?: { before: string; after: string };
}

export interface WorkTool {
  key: string;
  tool: string;
  maker: string | null;
  whatItDoes: string;
  whoFor: string[];
  jobs: string[];
  cost: string | null;
  costKind: string | null;
  effort: string | null;
  watchOut: string | null;
  link: string | null;
  lastChange: string | null;
  firstSeen: string | null;
  changes: { slug: string; headline: string; date: string | null }[];
  changeCount: number;
}

export interface WorkWeek {
  week: string;
  changed: number[];
  try: number[];
  skip: number[];
  tools: number;
  /** The week's own counts; `try` and `skip` are cut to what a page shows (8 and 6). */
  tryCount?: number;
  skipCount?: number;
}

export interface WorkData {
  generatedAt: string;
  storyIds: number[];
  tools: WorkTool[];
  weeks: Record<string, WorkWeek>;
  jobs: Record<string, number[]>;
}

export interface WorkBriefing {
  date: string;
  generatedAt: string;
  windowHours: number;
  storyIds: number[];
  alsoIds: number[];
  /** The featured pick ("one thing to try"), always storyIds[0] when set: a real maker, an official
      link and a publisher's coverage (work.py, featurable). Null when nothing qualified that day;
      older exports have no such field. */
  featuredId?: number | null;
  stats: { items: number; tools: number; free: number; minutes: number };
}

const DATA_DIR = path.resolve(process.cwd(), "src/data");

function readJson<T>(name: string, fallback: T): T {
  try {
    return JSON.parse(fs.readFileSync(path.join(DATA_DIR, name), "utf-8")) as T;
  } catch {
    return fallback;
  }
}

export const SECTION_NAME = "AI at Work";
export const SECTION_TAGLINE = "Practical AI for marketing, customers and running a small business.";

/** The five jobs the section sorts by, in the order the filters show them. Each has its own page at
    /work/<slug> (pages/work/[job].astro); the slugs live in indexing.mjs so the sitemap agrees. */
export interface Job {
  key: string;
  slug: string;
  label: string;
  blurb: string;
  /** The phrase after "AI tools to" / "AI tools for" in the page title and heading. */
  task: string;
  /** What the job covers, in a sentence, for the page's introduction. */
  covers: string;
}
export const JOBS: Job[] = [
  {
    key: "customers", slug: JOB_SLUGS.customers, label: "Get customers", task: "to get customers",
    blurb: "Ads, SEO, email and everything that brings people in.",
    covers: "Advertising, search, email, social and the other ways people find a business and decide to try it.",
  },
  {
    key: "content", slug: JOB_SLUGS.content, label: "Make content", task: "to make content",
    blurb: "Writing, images, video and the work of publishing them.",
    covers: "Writing, images, video, audio, translation and the work of getting them published.",
  },
  {
    key: "sell", slug: JOB_SLUGS.sell, label: "Sell", task: "to sell more",
    blurb: "Leads, follow-ups, checkout and the online shop.",
    covers: "Leads, follow-ups, quotes, checkout and the online shop: the steps between interest and a sale.",
  },
  {
    key: "support", slug: JOB_SLUGS.support, label: "Support customers", task: "for customer support",
    blurb: "Answering people faster without answering worse.",
    covers: "Answering customers' questions by chat, email and phone, faster and without answering worse.",
  },
  {
    key: "business", slug: JOB_SLUGS.business, label: "Run the business", task: "to run a small business",
    blurb: "Admin, books, scheduling and the jobs nobody wants.",
    covers: "Admin, bookkeeping, scheduling, documents, hiring and the other jobs that keep a small business running.",
  },
];
export const jobBySlug = (slug: string): Job | undefined => JOBS.find((j) => j.slug === slug);
/** Cards and tools per job, counted the way indexing.mjs decides whether a job page is thin. */
export const jobThin = (key: string): boolean => !jobIndexable(jobCounts(stories)[key]);
export const JOB_LABELS: Record<string, string> = Object.fromEntries(JOBS.map((j) => [j.key, j.label]));

export const WHO_LABELS: Record<string, string> = {
  marketer: "marketers",
  sales: "sales",
  founder: "founders",
  support: "support teams",
  ops: "operations",
  ecommerce: "online shops",
};

export const COST_LABELS: Record<string, string> = {
  free: "Free",
  "free tier": "Free tier",
  included: "Already included",
  paid: "Paid",
  unknown: "Cost not stated",
};

/** The cost as a card shows it: the stated price, or the kind's label when there is none. */
export const costLabel = (cost: string | null | undefined, kind: string | null | undefined): string =>
  cost && cost !== "unknown" && kind && kind !== "unknown" ? cost : COST_LABELS[kind || "unknown"] || COST_LABELS.unknown;
/** Free, a free tier or already paid for: nothing new to pay to start. */
export const isFree = (kind: string | null | undefined): boolean => ["free", "free tier", "included"].includes(kind || "");
/** The effort scale's step (1 to 3) and its words; a card whose coverage did not say has none. */
export const EFFORT_STEPS: Record<string, { step: number; label: string }> = {
  minutes: { step: 1, label: "Minutes" },
  "an afternoon": { step: 2, label: "An afternoon" },
  "needs a developer": { step: 3, label: "Needs a developer" },
};

export const work: WorkData = readJson<WorkData>("work.json", {
  generatedAt: new Date().toISOString(),
  storyIds: [],
  tools: [],
  weeks: {},
  jobs: {},
});

export const workBriefing: WorkBriefing = readJson<WorkBriefing>("work-briefing.json", {
  date: new Date().toISOString().slice(0, 10),
  generatedAt: work.generatedAt,
  windowHours: 24,
  storyIds: [],
  alsoIds: [],
  stats: { items: 0, tools: 0, free: 0, minutes: 0 },
});

/* The section's own podcast (pipeline/digest/audio.py): one episode a week, in its own manifest and
   its own feed at /work/podcast.xml, so a listener who subscribed to the daily news show at
   /podcast.xml never gets a marketing playbook they did not ask for, and the other way round. */
export const workEpisodes: Episode[] = readJson<Episode[]>("work-episodes.json", []);
export const latestWorkEpisode: Episode | undefined = workEpisodes[0];
export const workEpisodeFor = (week: string): Episode | undefined => workEpisodes.find((e) => e.week === week);

/** A story with a card, typed so pages can rely on the card being there. */
export type WorkStory = Story & { workCard: WorkCard };

const hasCard = (s: Story | undefined): s is WorkStory => Boolean(s && (s as WorkStory).workCard);

/** Every story in the section, newest first. */
export const workStories: WorkStory[] = work.storyIds
  .map(storyFor)
  .filter(hasCard)
  // The export already sorts by first publication; stories.json may be filtered further (withdrawn
  // articles), so the order is rebuilt here rather than trusted.
  .sort((a, b) => Date.parse(b.firstPublishedAt || "0") - Date.parse(a.firstPublishedAt || "0"));

/** The fallback when work.json is missing but stories carry cards (a partial data copy). */
export const sectionStories: WorkStory[] = workStories.length
  ? workStories
  : stories.filter(hasCard).sort((a, b) => Date.parse(b.firstPublishedAt || "0") - Date.parse(a.firstPublishedAt || "0"));

export const briefingItems: WorkStory[] = workBriefing.storyIds.map(storyFor).filter(hasCard);
export const briefingAlso: WorkStory[] = workBriefing.alsoIds.map(storyFor).filter(hasCard);

export const workCardFor = (story: Story): WorkCard | null => ((story as WorkStory).workCard ?? null);

/** Section stories of an ISO week, as the playbook page lists them. */
export function weekStories(week: string): {
  changed: WorkStory[]; tryThis: WorkStory[]; skip: WorkStory[]; tools: number; tryCount: number; skipCount: number;
} {
  const w = work.weeks[week];
  const pick = (ids: number[] | undefined) => (ids || []).map(storyFor).filter(hasCard);
  const changed = pick(w?.changed);
  // The try and skip lists are cut to what a page shows; the counts are the whole week's, taken from
  // the cards themselves so an export without tryCount/skipCount still says the right number.
  const skipCount = changed.filter((s) => s.workCard.skip).length;
  return { changed, tryThis: pick(w?.try), skip: pick(w?.skip), tools: w?.tools || 0, tryCount: changed.length - skipCount, skipCount };
}

/** "1 item", "3 items". */
export const count = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export const workWeeks: string[] = Object.keys(work.weeks).sort((a, b) => (a < b ? 1 : -1));

/** "Canva Magic Studio" -> "canva-magic-studio", for the per-tool anchors on /work/tools. */
export function toolSlug(tool: WorkTool): string {
  const base = `${tool.tool} ${tool.maker || ""}`.trim();
  return base
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^a-zA-Z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .toLowerCase()
    .slice(0, 60);
}

/* ---------- the card's headline ---------- */

/** work.py's fallback_headline, to the character, so a page can tell a written outcome headline
    from the pipeline's stand-in for one. */
const HEADLINE_MAX = 80;
function cutWords(text: string, limit: number): string {
  if (text.length <= limit) return text;
  return text.slice(0, limit - 1).replace(/\s+\S*$/, "").replace(/[ ,;:\-–—]+$/, "") + "…";
}
export function fallbackHeadline(tool: string, what: string): string {
  const w = (what || "").trim().replace(/\.+$/, "");
  if (!w) return cutWords(tool, HEADLINE_MAX);
  let line: string;
  if (w.toLowerCase().startsWith(tool.toLowerCase())) line = w;
  else if (/^[A-Z][a-z]+s\b/.test(w) && !/^(?:This|Its|Is|Has|Was|Does|Analytics|News)\b/.test(w)) line = `${tool} ${w[0].toLowerCase()}${w.slice(1)}`;
  else line = `${tool}: ${w}`;
  return cutWords(line, HEADLINE_MAX + 10);
}

/** What a card's heading says: the outcome headline when the pipeline wrote one, else what the tool
    does (the tool's name is already on the card's label). `sub` is the line under an outcome
    headline, so what the tool does is never lost. */
export function cardTitle(card: WorkCard): { title: string; sub: string | null } {
  const h = (card.headline || "").trim();
  if (!h || h === fallbackHeadline(card.tool, card.whatItDoes)) return { title: card.whatItDoes, sub: null };
  return { title: h, sub: card.whatItDoes };
}

/* ---------- what you get ---------- */

const PLAIN_STOP = new Set(("a an the and or but of to in on for with from by at as is are be it its this that your you " +
  "yours their them they can will get gets lets let into out up more less one all any every each so than then").split(" "));
const plainWords = (text: string): Set<string> =>
  new Set((text.toLowerCase().match(/[a-z0-9]+/g) || [])
    .filter((w) => w.length > 2 && !PLAIN_STOP.has(w))
    .map((w) => w.replace(/(?:ing|ed|es|s)$/, "")));

/** Share of the shorter text's meaningful words that the other one also has (0 to 1). */
export function wordOverlap(a: string, b: string): number {
  const x = plainWords(a);
  const y = plainWords(b);
  if (!x.size || !y.size) return 0;
  let shared = 0;
  for (const w of x) if (y.has(w)) shared++;
  return shared / Math.min(x.size, y.size);
}

/** The card's "What you get" line, or null when there is none or when it only says again what the
    heading already says (most of its words are the heading's), so a reader never reads one thing
    twice. `title` is the heading the card shows (cardTitle). */
export function youGetLine(card: WorkCard, title: string): string | null {
  const line = (card.youGet || "").trim();
  if (!line) return null;
  return wordOverlap(line, title) >= 0.7 ? null : line;
}

/** Where a card's steps came from, as the card says it: "From the article", "From Canva's own page". */
export function stepsSourceLabel(card: WorkCard): string {
  if (card.stepsSource !== "maker") return "From the article";
  const maker = (card.maker || "").trim();
  if (!maker) return "From the maker's own page";
  return `From ${maker}${/s$/i.test(maker) ? "’" : "’s"} own page`;
}

/* ---------- the featured pick ---------- */

/** work.py's HANDLE and COMMUNITY_HOSTS: a maker that is a forum handle ("MoistTonight3997", "u/x",
    "jane_doe"), or the author of one of the story's forum posts, is not a company behind a tool. */
const HANDLE = /^(?:\/?u\/|@)\S+$|^[A-Za-z][A-Za-z-]*\d{3,}$|^[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+$/;
const COMMUNITY_HOSTS = ["reddit.com", "redd.it", "news.ycombinator.com", "ycombinator.com", "github.com", "gitlab.com",
  "x.com", "twitter.com", "lobste.rs", "bsky.app", "mastodon.social", "discord.com", "discord.gg"];
const hostOf = (url: string | null | undefined): string => {
  try { return new URL(url || "").hostname.toLowerCase().replace(/^www\./, ""); } catch { return ""; }
};
const onCommunity = (host: string) => COMMUNITY_HOSTS.some((h) => host === h || host.endsWith(`.${h}`));
/** The card names a maker, and the maker is not a forum handle. */
export function namedMaker(story: WorkStory): boolean {
  const name = (story.workCard.maker || "").trim();
  if (!name || HANDLE.test(name)) return false;
  const bare = name.toLowerCase().replace(/^u\//, "");
  return !story.articles.some((a) => {
    const author = (a.author || "").trim().replace(/^\/?u\//, "").toLowerCase();
    return author && author === bare && onCommunity((a.domain || hostOf(a.url)).toLowerCase().replace(/^www\./, ""));
  });
}

/** The hub's one thing to try: the pipeline's featured pick when the export names one, else the
    first card with a named maker (never a forum handle) among `candidates`, in their order. */
export function featuredPick(candidates: WorkStory[]): WorkStory | undefined {
  const id = workBriefing.featuredId;
  if (id != null) {
    const s = storyFor(id);
    if (hasCard(s)) return s;
  }
  return candidates.find((s) => !s.workCard.skip && namedMaker(s)) || candidates.find(namedMaker);
}

/** "marketers and online shops" from ["marketer", "ecommerce"]. */
export function whoLine(whoFor: string[]): string {
  const names = whoFor.map((w) => WHO_LABELS[w] || w);
  if (names.length <= 1) return names[0] || "small teams";
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** The tools a story's card belongs to, so a story page can link into the directory. */
export function toolFor(card: WorkCard): WorkTool | undefined {
  const key = `${card.tool}|${card.maker || ""}`.toLowerCase();
  return work.tools.find((t) => `${t.tool}|${t.maker || ""}`.toLowerCase() === key)
    || work.tools.find((t) => t.tool.toLowerCase() === card.tool.toLowerCase());
}

/* ---------- the home page's AI at Work ---------- */

export interface HomeWork {
  /** The week's one thing to try: the pipeline's featured pick, else the first fresh card with a
      named maker (never a forum handle, namedMaker) that is not a "leave for now" card. */
  featured: WorkStory | undefined;
  /** More things to try: two beside a featured pick, three without one. Never "leave for now". */
  also: WorkStory[];
  /** The job pages that have something on them, with their counts, in the section's order. */
  jobs: { job: Job; count: number }[];
}

/** What the home page shows of AI at Work (components/WorkHome.astro, WorkTeaser.astro): cards of
    the seven days before `generatedAt`, in the section briefing's order (usefulness), then newest
    first. Null when there is neither a featured pick nor three good cards, so the page shows
    nothing rather than a thin block that reads like an ad for an empty section. */
export function homeWork(generatedAt: string): HomeWork | null {
  const weekAgo = Date.parse(generatedAt) - 7 * 24 * 3600 * 1000;
  const fresh = (s: WorkStory) => Date.parse(s.firstPublishedAt || s.updatedAt || "0") >= weekAgo;
  const pool = [...briefingItems, ...sectionStories.filter((s) => !briefingItems.some((b) => b.id === s.id))].filter(fresh);
  const id = workBriefing.featuredId;
  const featured = (id != null ? pool.find((s) => s.id === id && !s.workCard.skip && namedMaker(s)) : undefined)
    || pool.find((s) => !s.workCard.skip && namedMaker(s));
  const also = pool.filter((s) => s !== featured && !s.workCard.skip).slice(0, featured ? 2 : 3);
  if (!featured && also.length < 3) return null;
  // Counted the way the hub's job tiles count them (pages/work/index.astro).
  const jobs = JOBS.map((job) => ({ job, count: sectionStories.filter((s) => s.workCard.jobs.includes(job.key)).length }))
    .filter((j) => j.count > 0);
  return { featured, also, jobs };
}
