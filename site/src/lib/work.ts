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
