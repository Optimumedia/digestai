import fs from "node:fs";
import path from "node:path";
import { marked } from "marked";
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
};

export const meta: Meta = readJson<Meta>("meta.json", {
  generatedAt: new Date().toISOString(),
  siteUrl: "https://digestai.news",
  categories: DEFAULT_CATEGORIES,
  storyCount: 0,
  articleCount: 0,
});

export const categories: Record<string, string> = meta.categories || DEFAULT_CATEGORIES;

export const stories: Story[] = readJson<Story[]>("stories.json", []).filter((s) => s.articles?.length);
export const entities: Entity[] = readJson<Entity[]>("entities.json", []);
export const sources: { key: string; name: string; url: string; kind: string; type: string }[] = readJson("sources.json", []);
export interface Episode {
  date: string;
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

/** ISO week key like 2026-W37 and its Monday. */
export function weekKey(iso: string | null | undefined): string {
  const d = new Date(iso || Date.now());
  const day = (d.getUTCDay() + 6) % 7;
  const thursday = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() - day + 3));
  const jan4 = new Date(Date.UTC(thursday.getUTCFullYear(), 0, 4));
  const week = 1 + Math.round(((thursday.getTime() - jan4.getTime()) / 864e5 - 3 + ((jan4.getUTCDay() + 6) % 7)) / 7);
  return `${thursday.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
}

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

export function entitySlug(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^a-zA-Z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .toLowerCase();
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

export function dateKey(iso: string | null | undefined): string {
  return (iso || "").slice(0, 10);
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
