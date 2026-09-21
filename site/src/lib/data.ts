import fs from "node:fs";
import path from "node:path";
import { marked } from "marked";
import { published, entitySlug, dateKey, weekKey, storyIndexable, noindexPaths, modelPages, modelIndexable, TOPIC_MIN_STORIES, DAILY_MIN_STORIES, WEEK_MIN_STORIES, WORK_MIN_ITEMS } from "./indexing.mjs";

// Indexing rules live in indexing.mjs so the sitemap in astro.config.mjs applies the same ones.
export { entitySlug, dateKey, weekKey, storyIndexable, TOPIC_MIN_STORIES, DAILY_MIN_STORIES, WEEK_MIN_STORIES, WORK_MIN_ITEMS };
import { loadRedirects } from "./redirects.mjs";

export interface Discussion {
  site: "hn" | "reddit";
  url: string;
  points: number | null;
}

export interface Article {
  id: number;
  slug: string | null;
  url: string;
  domain: string;
  source: string | null;
  via: string | null;
  sourceKey: string | null;
  sourceType: "primary" | "press" | "newsletter" | "community";
  title: string;
  headline: string | null;
  author: string | null;
  publishedAt: string | null;
  description: string | null;
  contentMd: string | null;
  wordCount: number;
  imageUrl: string | null;
  summaryMd: string | null;
  keyPoints: string[];
  whyItMatters: string | null;
  category: string | null;
  entities: Record<string, string[]>;
  contentType: string | null;
  importance: number | null;
  predictedScore: number | null;
  isLead: boolean;
  discussion: Discussion | null;
  modelRelease: ModelRelease | null;
  funding: Funding | null;
  /** The source's own title hedged (may, could, a question) and our headline does not (admin card). */
  hedged?: boolean;
}

export interface ModelRelease {
  name: string;
  lab: string | null;
  kind: string;
  availability: string;
  license: string | null;
  context: string | null;
  link: string | null;
  date?: string | null;
  storySlug?: string;
  storyHeadline?: string;
  sources?: number;
}

export interface Funding {
  company: string;
  amount_usd: number | null;
  round: string;
  investors: string[];
  valuation_usd: number | null;
  date?: string | null;
  storySlug?: string;
  storyHeadline?: string;
  sources?: number;
}

export interface Thread {
  id: number;
  slug: string;
  title: string;
  summary: string | null;
  category: string | null;
  categoryName: string;
  entities: Record<string, string[]>;
  storyCount: number;
  named?: boolean;
  ogImage?: string;
  firstAt: string | null;
  updatedAt: string | null;
  storyIds: number[];
}

export interface Coverage {
  primary: number;
  press: number;
  newsletter: number;
  community: number;
}

export interface Story {
  id: number;
  slug: string;
  headline: string;
  summaryMd: string | null;
  keyPoints: string[];
  whyItMatters: string | null;
  category: string | null;
  categoryName: string;
  entities: Record<string, string[]>;
  importance: number;
  score: number;
  pinned: boolean;
  articleCount: number;
  /** Sources counted in articleCount and coverage but not listed (a full story, cluster.py). */
  overflowCount?: number;
  coverage: Coverage;
  hasPrimary: boolean;
  discussions: Discussion[];
  threadId: number | null;
  pulse: string | null;
  sourceNotes?: { agree?: string; differ?: string[] } | null;
  firstPublishedAt: string | null;
  updatedAt: string | null;
  imageUrl: string | null;
  ogImage: string | null;
  leadArticleId: number;
  articles: Article[];
}

export interface Entity {
  name: string;
  kind: "companies" | "models" | "people";
  storyIds: number[];
}

export interface Briefing {
  date: string;
  generatedAt: string;
  windowHours: number;
  storyIds: number[];
  alsoIds: number[];
  stats: { stories: number; articles: number; minutes: number };
}

export interface Meta {
  generatedAt: string;
  siteUrl: string;
  categories: Record<string, string>;
  storyCount: number;
  articleCount: number;
}

const DATA_DIR = path.resolve(process.cwd(), "src/data");

function readJson<T>(name: string, fallback: T): T {
  try {
    return JSON.parse(fs.readFileSync(path.join(DATA_DIR, name), "utf-8")) as T;
  } catch {
    return fallback;
  }
}

export const DEFAULT_CATEGORIES: Record<string, string> = {
  models: "Generative AI & Models",
  agents: "Agents & Tools",
  research: "Research",
  business: "Business & Funding",
  policy: "Policy & Regulation",
  hardware: "Hardware & Compute",
  enterprise: "Enterprise & Industry",
  robotics: "Robotics & Physical AI",
  society: "Society & Work",
  marketing: "Marketing & Small Business",
};

export const meta: Meta = readJson<Meta>("meta.json", {
  generatedAt: new Date().toISOString(),
  siteUrl: "https://digestai.news",
  categories: DEFAULT_CATEGORIES,
  storyCount: 0,
  articleCount: 0,
});

export const categories: Record<string, string> = meta.categories || DEFAULT_CATEGORIES;

export const stories: Story[] = published(readJson<Story[]>("stories.json", []));
export const entities: Entity[] = readJson<Entity[]>("entities.json", []);
export const sources: { key: string; name: string; url: string; kind: string; type: string }[] = readJson("sources.json", []);
export interface Episode {
  date: string;
  /** Only on an AI at Work episode: the ISO week it reads (pipeline/digest/audio.py). */
  week?: string;
  title: string;
  file: string;
  url: string;
  bytes: number;
  seconds: number;
  publishedAt: string;
  description: string;
  stories: { slug: string; headline: string }[];
  transcript: string;
}
export const episodes: Episode[] = readJson<Episode[]>("episodes.json", []);
export const briefing: Briefing = readJson<Briefing>("briefing.json", {
  date: new Date().toISOString().slice(0, 10),
  generatedAt: meta.generatedAt,
  windowHours: 24,
  storyIds: [],
  alsoIds: [],
  stats: { stories: 0, articles: 0, minutes: 0 },
});
export const newsletters: Record<string, { publicUrl: string | null; subject: string | null }> = readJson("newsletters.json", {});
export const threads: Thread[] = readJson<Thread[]>("threads.json", []);
export const topicInfo: Record<string, { name: string; kind: string; description: string }> = readJson("topics.json", {});
export const trackers: { models: ModelRelease[]; funding: Funding[] } = readJson("trackers.json", { models: [], funding: [] });
/** Paths built but kept out of the index and the sitemaps (thin hubs, single-source briefs, quiet days). */
export const noindex: Set<string> = noindexPaths(stories, entities, trackers.models);

/* ---- media store (pipeline/digest/media.py) ----
   media.json maps a story slug to where its share image and thumbnail are: a site path ("/og/…",
   "/media/…") or the tag of the GitHub release that holds it. Release downloads carry no image type,
   which Facebook and LinkedIn refuse for link previews, so share images from a release are used only
   when PUBLIC_OG_FROM_STORE is "1"; otherwise older stories get the default card. <img> tags are fine
   with release files (browsers recognise the picture). */
export interface MediaIndex {
  repo: string;
  og: Record<string, string>;
  thumb: Record<string, string>;
}
export const mediaIndex: MediaIndex = readJson<MediaIndex>("media.json", { repo: "Optimumedia/digestai", og: {}, thumb: {} });
const ogFromStore = import.meta.env.PUBLIC_OG_FROM_STORE === "1";

function mediaUrl(where: string | undefined, name: string): string | null {
  if (!where) return null;
  if (where.startsWith("/")) return `${meta.siteUrl}${where}`;
  return `https://github.com/${mediaIndex.repo}/releases/download/${encodeURIComponent(where)}/${encodeURIComponent(name)}`;
}

/** Absolute share image for a story's og:image. */
export function shareImage(story: { slug: string }): string {
  const where = mediaIndex.og[story.slug];
  if (where && (where.startsWith("/") || ogFromStore)) return mediaUrl(where, `og-${story.slug}.png`)!;
  return `${meta.siteUrl}/og-default.png`;
}

/* ---- share images with their sizes (Google Discover, og:image:width/height, NewsArticle image) ----
   Our own cards: the 1200x630 share card from pipeline/digest/images.py, plus the 16:9, 4:3 and
   1:1 variants it draws into public/og for recent stories. News outlets' photos are not used here: we
   do not own them, and a picture in the structured data is one Google may show as the image of our
   page. The one exception is the company's own announcement picture (primaryImage below). Sizes are read from the files at build time (no database column); a card
   that lives only in the media store is the renderer's fixed size. */
export interface ImageInfo {
  url: string;
  width: number;
  height: number;
}
const PUBLIC_DIR = path.resolve(process.cwd(), "public");
const CARD_SIZE = { width: 1200, height: 630 }; // images.py W, H
export const CARD_VARIANTS = ["16x9", "4x3", "1x1"] as const;
const sizeCache = new Map<string, { width: number; height: number } | null>();

/** Width and height of a PNG from its header, or null when the file is missing or not a PNG. */
export function pngSize(file: string): { width: number; height: number } | null {
  if (sizeCache.has(file)) return sizeCache.get(file)!;
  let out: { width: number; height: number } | null = null;
  try {
    const fd = fs.openSync(file, "r");
    const head = Buffer.alloc(24);
    fs.readSync(fd, head, 0, 24, 0);
    fs.closeSync(fd);
    if (head.readUInt32BE(0) === 0x89504e47 && head.toString("ascii", 12, 16) === "IHDR") {
      out = { width: head.readUInt32BE(16), height: head.readUInt32BE(20) };
    }
  } catch {
    out = null;
  }
  sizeCache.set(file, out);
  return out;
}

/** The site's default card, for pages without their own. */
export function defaultImage(): ImageInfo {
  return { url: `${meta.siteUrl}/og-default.png`, ...(pngSize(path.join(PUBLIC_DIR, "og-default.png")) || CARD_SIZE) };
}

/** The story's own share card with its size, or null when it has none (the page then uses the default). */
export function storyCard(story: { slug: string }): ImageInfo | null {
  const url = shareImage(story);
  if (url === `${meta.siteUrl}/og-default.png`) return null;
  const where = mediaIndex.og[story.slug];
  const size = where && where.startsWith("/") ? pngSize(path.join(PUBLIC_DIR, where)) : null;
  if (where && where.startsWith("/") && !size) return null; // listed but not in this build
  return { url, ...(size || CARD_SIZE) };
}

/* ---- the company's own share image (pipeline/digest/images.py primary_images) ----
   When the story's primary source is the company's own announcement (openai.com, blog.google, …) and
   the picture that page declares is at least 1200 px wide, it is the story's og:image and twitter:image:
   a press image the company publishes to be shared, linked on the company's server, never copied.
   News outlets' photos are never used (they are usually licensed from agencies). The pipeline measured
   the picture and checked it still answers; a story missing from primary-images.json keeps our card.
   "ideal" means a shape close to 1.91:1 (1.5 to 2.1): then it also leads the NewsArticle images;
   any other shape is still fine for og (the networks crop) but our card stays first there. */
export interface PrimaryImage extends ImageInfo {
  ideal: boolean;
}
const primaryImages = readJson<Record<string, PrimaryImage>>("primary-images.json", {});

/** The company's own picture for the story, or null when it has none good enough. */
export function primaryImage(story: { slug: string }): PrimaryImage | null {
  const p = primaryImages[story.slug];
  if (!p || typeof p.url !== "string" || !p.url.startsWith("https://")) return null;
  if (!(p.width >= 1200) || !(p.height > 0)) return null;
  const ratio = p.width / p.height;
  return { url: p.url, width: p.width, height: p.height, ideal: ratio >= 1.5 && ratio <= 2.1 };
}

/** Every image of the story for structured data and the news sitemap: the share card then the 16:9,
    4:3 and 1:1 variants present in this build, and the company's own picture (first when its shape is
    ideal, last otherwise); only images at least 1200 px wide, the size Discover asks for. */
export function storyImages(story: { slug: string }): ImageInfo[] {
  const card = storyCard(story);
  const out: ImageInfo[] = card ? [card] : [];
  for (const v of CARD_VARIANTS) {
    const size = pngSize(path.join(PUBLIC_DIR, "og", `${story.slug}-${v}.png`));
    if (size) out.push({ url: `${meta.siteUrl}/og/${encodeURIComponent(story.slug)}-${v}.png`, ...size });
  }
  const cards = out.filter((i) => i.width >= 1200);
  const p = primaryImage(story);
  if (!p) return cards;
  const own: ImageInfo = { url: p.url, width: p.width, height: p.height };
  return p.ideal ? [own, ...cards] : [...cards, own];
}

/** The NewsArticle image list: never empty, and our card (the story's, else the site's) first unless
    the company's picture has the ideal shape. */
export function storyJsonLdImages(story: { slug: string }): ImageInfo[] {
  const images = storyImages(story);
  const p = primaryImage(story);
  if (!images.length) return [defaultImage()];
  if (p && !p.ideal && images[0].url === p.url) return [defaultImage(), ...images];
  return images;
}

/** og:image and twitter:image: the company's own picture when there is one, else our card, else the
    site's default card; always with its real size. */
export function storyOgImage(story: { slug: string }): ImageInfo {
  const p = primaryImage(story);
  if (p) return { url: p.url, width: p.width, height: p.height };
  return storyImages(story)[0] || defaultImage();
}

/** When the story last changed in a way a reader can see: the newest publication time among the
    sources it lists, never before it was first published. Not updatedAt, which moves whenever an
    article joins the story, even one that is only counted (a full story), and would make an old
    story look new to Google (the freshness rules: first_published_at means new). */
export function storyModified(story: Story): string | null {
  const first = story.firstPublishedAt;
  const firstT = Date.parse(first || "");
  if (Number.isNaN(firstT)) return first || story.updatedAt;
  let latest = firstT;
  for (const a of story.articles) {
    const t = Date.parse(a.publishedAt || "");
    if (!Number.isNaN(t) && t > latest) latest = t;
  }
  // A feed's future-dated article cannot move the date past the last time the story changed.
  const ceiling = Date.parse(story.updatedAt || "");
  if (!Number.isNaN(ceiling) && latest > ceiling) latest = Math.max(ceiling, firstT);
  return latest === firstT ? first : new Date(latest).toISOString().replace(/\.000Z$/, "Z");
}

/** A headline for structured data: Google shows at most 110 characters, cut at a word. */
export function clipHeadline(text: string, max = 110): string {
  if (text.length <= max) return text;
  return text.slice(0, max - 1).replace(/[\s,;:–—-]+\S*$/, "") + "…";
}

/** ~600 px WebP of the story's picture, or null (the page then uses the original). */
export function thumbImage(story: { slug: string }): string | null {
  return mediaUrl(mediaIndex.thumb[story.slug], `thumb-${story.slug}.webp`);
}

/* ---- archive (pipeline/digest/archive.py): stories older than the export window keep a small page. */
export interface ArchivedStory {
  slug: string;
  headline: string;
  summary: string;
  keyPoints: string[];
  category: string | null;
  categoryName: string;
  firstPublishedAt: string | null;
  updatedAt: string | null;
  archivedAt: string | null;
  sources: { title: string; url: string; source: string }[];
}
export const archived: ArchivedStory[] = readJson<ArchivedStory[]>("archive.json", []);

const storyById = new Map(stories.map((s) => [s.id, s]));
export const storyFor = (id: number): Story | undefined => storyById.get(id);
const storyBySlug = new Map(stories.map((s) => [s.slug, s]));
export const storyForSlug = (slug: string): Story | undefined => storyBySlug.get(slug);
/** Old story addresses that now point at the story they were merged into (redirects.mjs). */
export const storyRedirects: { from: string; to: string }[] = loadRedirects(DATA_DIR);
const threadById = new Map(threads.map((t) => [t.id, t]));
export const threadFor = (id: number | null | undefined): Thread | undefined => (id ? threadById.get(id) : undefined);

export function threadStories(t: Thread): Story[] {
  return t.storyIds.map(storyFor).filter((s): s is Story => Boolean(s));
}

/** Monday of an ISO week key like 2026-W37 (the key itself comes from indexing.mjs). */
export function weekMonday(key: string): Date {
  const [y, w] = key.split("-W").map(Number);
  const jan4 = new Date(Date.UTC(y, 0, 4));
  const monday = new Date(jan4.getTime() - ((jan4.getUTCDay() + 6) % 7) * 864e5 + (w - 1) * 7 * 864e5);
  return monday;
}

export function storiesByWeek(): Map<string, Story[]> {
  const map = new Map<string, Story[]>();
  for (const s of byRecency) {
    const key = weekKey(s.firstPublishedAt || s.updatedAt);
    if (!map.has(key)) map.set(key, []);
    map.get(key)!.push(s);
  }
  for (const list of map.values()) list.sort((a, b) => b.importance - a.importance || b.score - a.score);
  return new Map([...map.entries()].sort((a, b) => (a[0] < b[0] ? 1 : -1)));
}

export function money(n: number | null | undefined): string {
  if (!n) return "undisclosed";
  if (n >= 1e12) return `$${(n / 1e12).toFixed(n >= 1e13 ? 0 : 1)}T`;
  if (n >= 1e9) return `$${(n / 1e9).toFixed(n >= 1e10 ? 0 : 1)}B`;
  if (n >= 1e6) return `$${Math.round(n / 1e6)}M`;
  return `$${Math.round(n / 1e3)}K`;
}

/** Tracker facts attached to a hub: model releases whose name matches, funding rounds whose company matches. */
export function modelFacts(name: string): ModelRelease[] {
  const s = entitySlug(name);
  return trackers.models.filter((m) => entitySlug(m.name) === s).sort((a, b) => (a.date || "") < (b.date || "") ? 1 : -1);
}
export function companyFunding(name: string): Funding[] {
  const s = entitySlug(name);
  return trackers.funding.filter((f) => entitySlug(f.company) === s).sort((a, b) => (a.date || "") < (b.date || "") ? 1 : -1);
}
export function modelsByLab(lab: string | null | undefined, excludeName?: string): ModelRelease[] {
  if (!lab) return [];
  const l = lab.toLowerCase();
  const seen = new Set<string>();
  return trackers.models.filter((m) => (m.lab || "").toLowerCase() === l && m.name !== excludeName && !seen.has(m.name) && seen.add(m.name)).slice(0, 8);
}

export function sourceLeaderboard(days = 7): { name: string; stories: number; articles: number }[] {
  const since = Date.now() - days * 864e5;
  const map = new Map<string, { name: string; stories: number; articles: number }>();
  for (const s of stories) {
    if (Date.parse(s.updatedAt || "0") < since) continue;
    const seen = new Set<string>();
    for (const a of s.articles) {
      const name = a.source || a.domain;
      const row = map.get(name) || { name, stories: 0, articles: 0 };
      row.articles += 1;
      if (!seen.has(name)) { row.stories += 1; seen.add(name); }
      map.set(name, row);
    }
  }
  return [...map.values()].sort((a, b) => b.stories - a.stories || b.articles - a.articles).slice(0, 25);
}

export const byScore: Story[] = [...stories].sort((a, b) => Number(b.pinned) - Number(a.pinned) || b.score - a.score);
export const byRecency: Story[] = [...stories].sort(
  (a, b) => Date.parse(b.updatedAt || b.firstPublishedAt || "0") - Date.parse(a.updatedAt || a.firstPublishedAt || "0"),
);

export const briefingStories: Story[] = briefing.storyIds.map(storyFor).filter((s): s is Story => Boolean(s));
export const briefingAlso: Story[] = briefing.alsoIds.map(storyFor).filter((s): s is Story => Boolean(s));

export function storiesInCategory(cat: string): Story[] {
  return byRecency.filter((s) => s.category === cat);
}

export function storiesForEntity(e: Entity): Story[] {
  return e.storyIds.map((id) => storyById.get(id)).filter((s): s is Story => Boolean(s)).sort(
    (a, b) => Date.parse(b.updatedAt || "0") - Date.parse(a.updatedAt || "0"),
  );
}

/** Topic pages, with spelling variants (OpenAI / Openai) merged under one slug. */
export function topicPages(): { slug: string; entity: Entity }[] {
  const bySlug = new Map<string, Entity>();
  for (const e of entities) {
    const slug = entitySlug(e.name);
    if (!slug) continue;
    const existing = bySlug.get(slug);
    if (existing) existing.storyIds = [...new Set([...existing.storyIds, ...e.storyIds])];
    else bySlug.set(slug, { ...e, storyIds: [...e.storyIds] });
  }
  return [...bySlug.entries()].map(([slug, entity]) => ({ slug, entity }));
}

/** Slugs that actually have a topic page; tags for anything else render as plain text. */
export const topicSlugs: Set<string> = new Set(topicPages().map((t) => t.slug));

/* ---- model pages (/models/<slug>) ----
   One page per tracked model. Its slug is the one a topic hub on the same name would have, and that
   hub, when it exists, is a redirect here: the model page is the model's only indexable address. */
export interface ModelPage {
  slug: string;
  name: string;
  /** Tracker rows for this model, newest first (a model can be tracked more than once). */
  rows: ModelRelease[];
  storyIds: Set<number>;
  /** Stories on the topic hub under the same slug (0 when there is none); the share card needs 3. */
  hubStories: number;
}
const modelPageMap: Map<string, ModelPage> = modelPages(stories, entities, trackers.models);
export const modelPageList: ModelPage[] = [...modelPageMap.values()];
export const modelPageFor = (slug: string): ModelPage | undefined => modelPageMap.get(slug);
export const modelPageIndexable = (page: ModelPage): boolean => modelIndexable(page);

/** Where a name tag links: the model page for a tracked model, else its topic hub, else nowhere. */
export function hubHref(name: string | null | undefined): string | null {
  const slug = entitySlug(name || "");
  if (!slug) return null;
  if (modelPageMap.has(slug)) return `/models/${slug}`;
  return topicSlugs.has(slug) ? `/topic/${slug}` : null;
}

/** Tracked models among a story's entities (for the "Model page" line on a story). */
export function trackedModelsIn(story: Story): ModelPage[] {
  const seen = new Set<string>();
  const out: ModelPage[] = [];
  const names = Object.values(story.entities || {}).flat();
  for (const n of names) {
    const page = modelPageMap.get(entitySlug(n));
    if (page && !seen.has(page.slug)) { seen.add(page.slug); out.push(page); }
  }
  for (const page of modelPageList) {
    if (!seen.has(page.slug) && page.rows.some((r) => r.storySlug === story.slug)) { seen.add(page.slug); out.push(page); }
  }
  return out;
}

export function storiesByDay(): Map<string, Story[]> {
  const map = new Map<string, Story[]>();
  for (const s of byRecency) {
    const key = dateKey(s.firstPublishedAt || s.updatedAt);
    if (!key) continue;
    if (!map.has(key)) map.set(key, []);
    map.get(key)!.push(s);
  }
  for (const list of map.values()) list.sort((a, b) => b.score - a.score);
  // Newest day first. Map order alone followed the most recently *updated* story, which put an
  // older day on top whenever a new article landed in an older story.
  return new Map([...map.entries()].sort((a, b) => (a[0] < b[0] ? 1 : -1)));
}

export function related(story: Story, limit = 5): Story[] {
  const names = new Set(Object.values(story.entities || {}).flat().map((n) => n.toLowerCase()));
  const scored = stories
    .filter((s) => s.id !== story.id)
    .map((s) => {
      const shared = Object.values(s.entities || {}).flat().filter((n) => names.has(n.toLowerCase())).length;
      const sameCat = s.category === story.category ? 1 : 0;
      const age = Math.abs(Date.parse(s.updatedAt || "0") - Date.parse(story.updatedAt || "0")) / 864e5;
      return { s, w: shared * 3 + sameCat - Math.min(age, 30) / 10 };
    })
    .filter((x) => x.w > 0)
    .sort((a, b) => b.w - a.w);
  return scored.slice(0, limit).map((x) => x.s);
}

function escapeHtmlInMarkdown(md: string): string {
  return md.replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function renderMarkdown(md: string | null | undefined): string {
  if (!md) return "";
  return marked.parse(escapeHtmlInMarkdown(md), { async: false, gfm: true, breaks: false }) as string;
}

export function plain(md: string | null | undefined, max = 200): string {
  const text = (md || "").replace(/[#*_`>]/g, "").replace(/\s+/g, " ").trim();
  return text.length > max ? text.slice(0, max - 1).replace(/\s\S*$/, "") + "…" : text;
}

export function firstSentence(md: string | null | undefined, max = 180): string {
  const text = plain(md, 1000);
  const m = text.match(/^.*?[.!?](?=\s|$)/);
  return plain(m ? m[0] : text, max);
}

export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "";
  const diff = Math.max(0, now - Date.parse(iso));
  const m = Math.round(diff / 6e4);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 36) return `${h}h ago`;
  const d = Math.round(h / 24);
  if (d < 14) return `${d}d ago`;
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

export function formatDate(iso: string | null | undefined, opts: Intl.DateTimeFormatOptions = { day: "numeric", month: "long", year: "numeric" }): string {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString("en-GB", { ...opts, timeZone: "UTC" });
}

/* Text for <time> elements, written at build time so the date is in the HTML that crawlers and
   readers without JavaScript see. app.js swaps it for "3h ago" while the date is under two weeks
   old and leaves it alone after that. */
const buildYear = new Date(meta.generatedAt).getUTCFullYear();

/** "16 Sep", or "16 Sep 2025" once the year differs from the build's. */
export function shortDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const year = new Date(iso).getUTCFullYear() === buildYear ? {} : { year: "numeric" as const };
  return formatDate(iso, { day: "numeric", month: "short", ...year });
}

/** "16 September 2026, 09:55 UTC". */
export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "";
  return `${formatDate(iso)}, ${new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" })} UTC`;
}

export function readingMinutes(words: number): number {
  return Math.max(1, Math.round(words / 230));
}

export function leadArticle(story: Story): Article {
  return story.articles.find((a) => a.id === story.leadArticleId) || story.articles[0];
}

export function fullTextArticle(story: Story): Article | null {
  const lead = leadArticle(story);
  if (lead.contentMd) return lead;
  return story.articles.find((a) => a.contentMd) || null;
}

export function sourceNames(story: Story, limit = 4): { names: string[]; more: number } {
  const names = [...new Set(story.articles.map((a) => a.source || a.domain))];
  return { names: names.slice(0, limit), more: Math.max(0, names.length - limit) };
}

export function topDiscussion(story: Story): Discussion | null {
  return story.discussions?.[0] || null;
}

export function feedItem(s: Story) {
  return {
    id: s.id,
    slug: s.slug,
    headline: s.headline,
    category: s.category,
    categoryName: s.categoryName,
    updatedAt: s.updatedAt,
    publishedAt: s.firstPublishedAt,
    articleCount: s.articleCount,
    hasPrimary: s.hasPrimary,
  };
}
