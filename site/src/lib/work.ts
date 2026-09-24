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
  /** Same tool, same key (pipeline/digest/work.py, tool_key): "Ink Canvas" covered twice in a week
      is one thing to try. The export writes it so the site groups exactly as the pipeline does;
      older exports have no such field (toolKeyOf falls back to the name). */
  toolKey?: string;
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
  /** The collapsed card's labels, at most three, in order: cost ("Free", "Free to try", "Paid: from
      $X/mo", "Price not stated"...), time ("5 minutes", "An afternoon", "Needs a developer") and "No
      tech skills" / "No card needed" only when the article said so (work.py, card_labels). */
  labels?: WorkLabel[];
  /** The Try link's words, a verb and a place: "Try it in Gmail", "Open Canva" (work.py, action_label). */
  action?: string;
  /** "For example, a café could use it to ..." from the card's own uses, or "" (plain.py, scenario). */
  scenario?: string;
  /** False when the card has no headline that keeps the rules or nothing to use it for: it stays off
      the hub's lists (its story page still shows it). */
  hub?: boolean;
  /** The price the article stated, as data, with every figure checked against the article
      (work.py, price_grounded). The free-text `cost` above is unchanged; this is what the price
      history is built from (pipeline/digest/prices.py). Absent when the article stated none. */
  price?: WorkCardPrice;
}

export interface WorkCardPrice {
  /** The plan the figure is for ("Pro"), or "". */
  plan: string;
  /** The figure itself, or null when only a free tier is known. 0 means free. */
  amount: number | null;
  /** "USD", "EUR", "GBP"...; "" when there is no figure. */
  currency: string;
  /** "month", "year", "one-off", "usage" or "". */
  period: string;
  /** What the free plan gives ("500 images a month"), or "". */
  freeLimit: string;
  /** The article's own sentence stating the price, or "" when it could not be found in the text. */
  quoted: string;
}

export interface WorkLabel {
  kind: "cost" | "time" | "ease";
  text: string;
  /** Cost labels only: the kind behind the words, for the free / paid / not-stated styles. */
  costKind?: WorkCard["costKind"];
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
  /** One card per tool per week (work.py, fold_by_tool): the kept story's id, to the ids of the
      other stories about the same tool that it now stands for. */
  folded?: Record<string, number[]>;
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

/* ---------- what the tools cost, over time (pipeline/digest/prices.py) ---------- */

/** One observation of one tool's price: what it was, when, and where that was stated. */
export interface PriceObservation {
  /** When the price was stated (the story's date, or the run that read the maker's page). */
  at: string;
  /** When it was last confirmed still to be that: the card's "Price checked 22 Sept". */
  checkedAt?: string;
  plan: string;
  amount: number | null;
  currency: string;
  period: string;
  freeLimit: string;
  /** The sentence that stated it, in the article's or the pricing page's own words. */
  quoted: string;
  sourceUrl: string | null;
  storySlug: string | null;
  /** "article" (what the coverage said) or "maker" (the maker's own pricing page). */
  source: "article" | "maker";
}

export interface PriceTool {
  key: string;
  tool: string;
  maker: string | null;
  observations: PriceObservation[];
  current: PriceObservation;
  checkedAt: string | null;
  /** "was $12 in August", or "" with only one observation. */
  was: string;
}

export interface PriceChange {
  key: string;
  tool: string;
  maker: string | null;
  /** "rose", "fell", "free" (the free tier changed) or "opened" (a free tier where there was none). */
  kind: "rose" | "fell" | "free" | "opened";
  at: string;
  now: PriceObservation;
  before: PriceObservation;
  was: string;
}

export interface WorkPrices {
  generatedAt: string;
  tools: PriceTool[];
  changes: PriceChange[];
}

export const SECTION_NAME = "AI at Work";
export const SECTION_TAGLINE = "Simple AI tips for small businesses. Each one takes minutes to read.";

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
    key: "customers", slug: JOB_SLUGS.customers, label: "Get more customers", task: "to get customers",
    blurb: "Ads, SEO, email and everything that brings people in.",
    covers: "Advertising, search, email, social and the other ways people find a business and decide to try it.",
  },
  {
    key: "content", slug: JOB_SLUGS.content, label: "Make content faster", task: "to make content",
    blurb: "Writing, images, video and the work of publishing them.",
    covers: "Writing, images, video, audio, translation and the work of getting them published.",
  },
  {
    key: "sell", slug: JOB_SLUGS.sell, label: "Sell more", task: "to sell more",
    blurb: "Leads, follow-ups, checkout and the online shop.",
    covers: "Leads, follow-ups, quotes, checkout and the online shop: the steps between interest and a sale.",
  },
  {
    key: "support", slug: JOB_SLUGS.support, label: "Answer customers faster", task: "for customer support",
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
  "free tier": "Free to try",
  included: "Already included",
  paid: "Paid",
  unknown: "Price not stated",
};

/** The cost as a card shows it: the stated price, or the kind's label when there is none. */
export const costLabel = (cost: string | null | undefined, kind: string | null | undefined): string =>
  cost && cost !== "unknown" && kind && kind !== "unknown" ? cost : COST_LABELS[kind || "unknown"] || COST_LABELS.unknown;
/** Free, a free tier or already paid for: nothing new to pay to start. */
export const isFree = (kind: string | null | undefined): boolean => ["free", "free tier", "included"].includes(kind || "");
/** The effort scale's step (1 to 3) and its words; a card whose coverage did not say has none. */
export const EFFORT_STEPS: Record<string, { step: number; label: string }> = {
  minutes: { step: 1, label: "A few minutes" },
  "an afternoon": { step: 2, label: "An afternoon" },
  "needs a developer": { step: 3, label: "Needs a developer" },
};

/** The collapsed card's labels (at most three: cost, time, and a skill or risk-reducer the article
    stated). The export writes them (work.py, card_labels); an older export gets cost and time. */
export function cardLabels(card: WorkCard): WorkLabel[] {
  if (card.labels?.length) return card.labels.slice(0, 3);
  const out: WorkLabel[] = [{ kind: "cost", costKind: card.costKind, text: COST_LABELS[card.costKind] || COST_LABELS.unknown }];
  const step = card.effort ? EFFORT_STEPS[card.effort] : undefined;
  if (step) out.push({ kind: "time", text: step.label });
  return out;
}

/** The one action's words: the export's verb-and-place label ("Try it in Gmail", "Open Canva"), else
    "Try it". */
export const actionLabel = (card: WorkCard): string => (card.action || "").trim() || "Try it";

/** Never the one thing to try: a card that needs a developer, or one kept off the hub. */
export const featurableCard = (card: WorkCard): boolean => card.effort !== "needs a developer" && card.hub !== false && !card.skip;

export const work: WorkData = readJson<WorkData>("work.json", {
  generatedAt: new Date().toISOString(),
  storyIds: [],
  tools: [],
  weeks: {},
  jobs: {},
});

export const workPrices: WorkPrices = readJson<WorkPrices>("work-prices.json", {
  generatedAt: work.generatedAt,
  tools: [],
  changes: [],
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
/** A card the hub lists: the export marks one without a rule-keeping headline or any use hub: false. */
const onHub = (s: Story | undefined): s is WorkStory => hasCard(s) && s.workCard.hub !== false;

/** Every story in the section, newest first. */
export const workStories: WorkStory[] = work.storyIds
  .map(storyFor)
  .filter(onHub)
  // The export already sorts by first publication; stories.json may be filtered further (withdrawn
  // articles), so the order is rebuilt here rather than trusted.
  .sort((a, b) => Date.parse(b.firstPublishedAt || "0") - Date.parse(a.firstPublishedAt || "0"));

/** The fallback when work.json is missing but stories carry cards (a partial data copy). */
export const sectionStories: WorkStory[] = workStories.length
  ? workStories
  : stories.filter(onHub).sort((a, b) => Date.parse(b.firstPublishedAt || "0") - Date.parse(a.firstPublishedAt || "0"));

export const briefingItems: WorkStory[] = workBriefing.storyIds.map(storyFor).filter(onHub);
export const briefingAlso: WorkStory[] = workBriefing.alsoIds.map(storyFor).filter(onHub);

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

/* ---------- one card per tool ---------- */

/** A card's tool key: the export's (pipeline/digest/work.py, tool_key), so the site never has a
    second copy of the rule. An export from before the field falls back to the name and the maker,
    with invisible characters stripped ("React​iv AI Scheduler" is "Reactiv AI Scheduler"). */
export function toolKeyOf(card: WorkCard): string {
  if (card.toolKey) return card.toolKey;
  return `${cleanName(card.tool)}|${cleanName(card.maker || "")}`.toLowerCase();
}

/** A tool or maker name as it is compared and as it is shown: no zero-width or bidirectional
    characters, ordinary hyphens, single spaces (work.py, clean_name). */
export function cleanName(text: string | null | undefined): string {
  return String(text || "")
    .normalize("NFKC")
    .replace(/[­​-‏‪-‮⁠-⁤﻿]/g, "")
    .replace(/[‐-―−]/g, "-")
    .replace(/\s+/g, " ")
    .trim();
}

/** One story per tool, in the given order: the first card of each tool stays and the rest are left
    out, so the hub never shows the same tool twice in one list (work.py, fold_by_tool). Pass a list
    already in the order the page wants. */
export function oneCardPerTool<T extends WorkStory>(list: T[]): T[] {
  const seen = new Set<string>();
  return list.filter((s) => {
    const key = toolKeyOf(s.workCard);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

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

/* ---------- what it costs, and what it used to cost ---------- */

const CURRENCY_SIGNS: Record<string, string> = { USD: "$", EUR: "€", GBP: "£", JPY: "¥", INR: "₹" };
const PERIOD_SUFFIX: Record<string, string> = { month: "/mo", year: "/yr", "one-off": " once", usage: " per use" };

/** A price in words, the way the pipeline writes it (work.py, price_text): "$19/mo", "€99 once",
    "Free: 500 images a month". "" when there is nothing to say. */
export function priceText(p: { amount?: number | null; currency?: string; period?: string; freeLimit?: string } | null | undefined): string {
  if (!p) return "";
  if (p.amount == null) return p.freeLimit ? `Free: ${p.freeLimit}` : "";
  if (p.amount === 0) return "Free"; // "$0" is a figure; "Free" is the answer the reader wanted
  const sign = CURRENCY_SIGNS[p.currency || ""] || "";
  // Money keeps its pennies: "$1.20", never "$1.2"; a round figure keeps none: "$12", "€1,200".
  const figure = Number.isInteger(p.amount)
    ? p.amount.toLocaleString("en-GB")
    : p.amount.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const money = sign ? `${sign}${figure}` : `${figure} ${p.currency || ""}`.trim();
  return money + (PERIOD_SUFFIX[p.period || ""] || "");
}

/** The price history of the tool a card is about, or undefined. */
const PRICES_BY_KEY = new Map(workPrices.tools.map((t) => [t.key, t]));
export const priceHistoryFor = (card: WorkCard): PriceTool | undefined => PRICES_BY_KEY.get(toolKeyOf(card));
export const priceHistoryForKey = (key: string | null | undefined): PriceTool | undefined =>
  (key ? PRICES_BY_KEY.get(key) : undefined);

/** When the tool's price was last confirmed, for "Price checked 22 Sept" beside the cost chip.
    Null when nothing has ever been recorded for it. */
export function priceCheckedAt(card: WorkCard): string | null {
  const history = priceHistoryFor(card);
  return history?.checkedAt || history?.current?.at || null;
}

/** "was $12 in August" for a tool with more than one observation, else null. */
export function priceWas(history: PriceTool | undefined): string | null {
  return history?.was ? history.was : null;
}

/** What a change on /work/prices is called, in the reader's words. */
export const PRICE_CHANGE_LABELS: Record<PriceChange["kind"], string> = {
  rose: "Price went up",
  fell: "Price came down",
  free: "Free tier changed",
  opened: "Opened up",
};
/** The order the page groups them in: the good news first, then the bad, then the rest. */
export const PRICE_CHANGE_ORDER: PriceChange["kind"][] = ["fell", "opened", "rose", "free"];

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
  // An export from before the plain-words rules may still carry the old "Tool: what it does" stand-in.
  if (!h || (card.labels === undefined && h === fallbackHeadline(card.tool, card.whatItDoes))) {
    return { title: card.whatItDoes || card.tool, sub: null };
  }
  return { title: h, sub: card.whatItDoes || null };
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
    if (hasCard(s) && featurableCard(s.workCard)) return s;
  }
  // Never a card that needs a developer, or one the hub does not list.
  return candidates.find((s) => featurableCard(s.workCard) && namedMaker(s));
}

/** "marketers and online shops" from ["marketer", "ecommerce"]. */
export function whoLine(whoFor: string[]): string {
  const names = whoFor.map((w) => WHO_LABELS[w] || w);
  if (names.length <= 1) return names[0] || "small teams";
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** The tool a story's card belongs to, so a story page can link into the directory. The directory's
    rows carry the pipeline's own key, so the two agree on what one tool is. */
export function toolFor(card: WorkCard): WorkTool | undefined {
  const key = toolKeyOf(card);
  return work.tools.find((t) => t.key === key)
    || work.tools.find((t) => cleanName(t.tool).toLowerCase() === cleanName(card.tool).toLowerCase());
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
  const featured = (id != null ? pool.find((s) => s.id === id && featurableCard(s.workCard) && namedMaker(s)) : undefined)
    || pool.find((s) => featurableCard(s.workCard) && namedMaker(s));
  // One line per tool, and never the tool the pick is already about: the band showed "Ink Canvas"
  // twice when two stories carried it (oneCardPerTool).
  const featuredKey = featured ? toolKeyOf(featured.workCard) : null;
  const also = oneCardPerTool(pool.filter((s) => s !== featured && !s.workCard.skip && toolKeyOf(s.workCard) !== featuredKey))
    .slice(0, featured ? 2 : 3);
  if (!featured && also.length < 3) return null;
  // The section's whole count for each job, which is what the job page itself shows. The band has no
  // second number to disagree with; the shortcut says "items in total" so it cannot be misread as
  // this week's (the hub's tiles show both, pages/work/index.astro).
  const jobs = JOBS.map((job) => ({ job, count: sectionStories.filter((s) => s.workCard.jobs.includes(job.key)).length }))
    .filter((j) => j.count > 0);
  return { featured, also, jobs };
}
