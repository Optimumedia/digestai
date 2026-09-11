import fs from "node:fs";
import path from "node:path";
import { marked } from "marked";

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
  coverage: Coverage;
  hasPrimary: boolean;
  discussions: Discussion[];
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
export const briefing: Briefing = readJson<Briefing>("briefing.json", {
  date: new Date().toISOString().slice(0, 10),
  generatedAt: meta.generatedAt,
  windowHours: 24,
  storyIds: [],
  alsoIds: [],
  stats: { stories: 0, articles: 0, minutes: 0 },
});
export const newsletters: Record<string, { publicUrl: string | null; subject: string | null }> = readJson("newsletters.json", {});

const storyById = new Map(stories.map((s) => [s.id, s]));
export const storyFor = (id: number): Story | undefined => storyById.get(id);

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
  return map;
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
