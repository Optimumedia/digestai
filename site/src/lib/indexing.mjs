/* Which pages are worth putting in front of search engines. Plain JavaScript because both the
   page templates (through data.ts) and astro.config.mjs (the sitemap) import it, and the config is
   loaded before TypeScript is available. A page that fails these rules is still built and linked,
   but carries "noindex, follow" and stays out of every sitemap: hundreds of one-story hubs and
   single-source briefs were most of what Google saw, and it indexed almost nothing. */

export const TOPIC_MIN_STORIES = 3;
export const DAILY_MIN_STORIES = 3;
export const WEEK_MIN_STORIES = 5;
/** An AI at Work playbook week, or the section itself, with fewer practical items than this is thin. */
export const WORK_MIN_ITEMS = 3;
/** A story with one source, no primary document and importance at or below this is a thin page. */
export const THIN_STORY_IMPORTANCE = 4;

/** Stories the site publishes: the export can carry stories whose articles were all withdrawn. */
export function published(stories) {
  return stories.filter((s) => s.articles?.length);
}

export function entitySlug(name) {
  return name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[^a-zA-Z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .toLowerCase();
}

export function dateKey(iso) {
  return (iso || "").slice(0, 10);
}

/** ISO week key like 2026-W37. */
export function weekKey(iso) {
  const d = new Date(iso || Date.now());
  const day = (d.getUTCDay() + 6) % 7;
  const thursday = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() - day + 3));
  const jan4 = new Date(Date.UTC(thursday.getUTCFullYear(), 0, 4));
  const week = 1 + Math.round(((thursday.getTime() - jan4.getTime()) / 864e5 - 3 + ((jan4.getUTCDay() + 6) % 7)) / 7);
  return `${thursday.getUTCFullYear()}-W${String(week).padStart(2, "0")}`;
}

export function storyIndexable(s) {
  return !((s.articleCount || 0) <= 1 && !s.hasPrimary && (s.importance ?? 0) <= THIN_STORY_IMPORTANCE);
}

/** Topic hubs by slug with the ids of the stories they list, spelling variants merged. */
export function topicStoryIds(stories, entities) {
  const known = new Set(stories.map((s) => s.id));
  const topics = new Map();
  for (const e of entities) {
    const slug = entitySlug(e.name);
    if (!slug) continue;
    const ids = topics.get(slug) || new Set();
    for (const id of e.storyIds || []) if (known.has(id)) ids.add(id);
    topics.set(slug, ids);
  }
  return topics;
}

/* ---- AI at Work job pages (/work/<job>) ----
   The five jobs the section filters by, each with its own address. The keys are the ones
   pipeline/digest/work.py writes into every card's "jobs" list; the slugs are what people type. */
export const JOB_MIN_ITEMS = 3;
export const JOB_SLUGS = {
  customers: "get-customers",
  content: "make-content",
  sell: "sell",
  support: "support",
  business: "run-the-business",
};

/** Cards and distinct tools per job key, counted over the stories that carry a practical card. */
export function jobCounts(stories) {
  const out = {};
  for (const key of Object.keys(JOB_SLUGS)) {
    const cards = stories.filter((s) => s.workCard && (s.workCard.jobs || []).includes(key));
    const tools = new Set(cards.map((s) => `${s.workCard.tool}|${s.workCard.maker || ""}`.toLowerCase()));
    out[key] = { cards: cards.length, tools: tools.size };
  }
  return out;
}

/** A job page is thin until it has three cards or three tools behind it. */
export function jobIndexable(counts) {
  return Math.max(counts?.cards || 0, counts?.tools || 0) >= JOB_MIN_ITEMS;
}

/* ---- Model pages (/models/<slug>) ----
   One page per tracked model, under the same slug its topic hub would have (entitySlug of the name),
   so a model has exactly one address: when a topic hub exists under that slug it becomes a redirect
   to the model page (the model page lists the same stories, plus the tracker's own). */

/** Tracked models by slug: their tracker rows (newest first) and the ids of every story about them. */
export function modelPages(stories, entities, models) {
  const idBySlug = new Map(stories.map((s) => [s.slug, s.id]));
  const topics = topicStoryIds(stories, entities);
  const pages = new Map();
  for (const m of models || []) {
    const slug = entitySlug(m.name || "");
    if (!slug) continue;
    const hub = topics.get(slug);
    const page = pages.get(slug) || { slug, name: m.name, rows: [], storyIds: new Set(hub || []), hubStories: hub ? hub.size : 0 };
    page.rows.push(m);
    const id = idBySlug.get(m.storySlug);
    if (id !== undefined) page.storyIds.add(id);
    pages.set(slug, page);
  }
  for (const page of pages.values()) {
    page.rows.sort((a, b) => ((a.date || "") < (b.date || "") ? 1 : -1));
    page.name = page.rows[0].name;
  }
  return pages;
}

/** A model page is thin when all it has is its launch story and no spec (licence, context window). */
export function modelIndexable(page) {
  const specs = page.rows.some((r) => r.license || r.context);
  return page.storyIds.size >= 2 || specs;
}

/** Every page path that carries noindex and is left out of the sitemaps, from the exported data.
    `models` is trackers.json's model list; without it no model pages are counted. */
export function noindexPaths(stories, entities, models = []) {
  const out = new Set();
  for (const s of stories) if (!storyIndexable(s)) out.add(`/story/${s.slug}`);
  for (const [slug, ids] of topicStoryIds(stories, entities)) if (ids.size < TOPIC_MIN_STORIES) out.add(`/topic/${slug}`);
  const days = new Map();
  const weeks = new Map();
  for (const s of stories) {
    const at = s.firstPublishedAt || s.updatedAt;
    const day = dateKey(at);
    if (day) days.set(day, (days.get(day) || 0) + 1);
    const week = weekKey(at);
    weeks.set(week, (weeks.get(week) || 0) + 1);
  }
  for (const [day, n] of days) if (n < DAILY_MIN_STORIES) out.add(`/daily/${day}`);
  for (const [week, n] of weeks) if (n < WEEK_MIN_STORIES) out.add(`/week/${week}`);
  // AI at Work: the same bar, counted over stories that carry a practical card. The templates mark
  // these pages thin from the same numbers, so the meta tag and the sitemap always agree.
  const work = stories.filter((s) => s.workCard);
  const workWeeks = new Map();
  for (const s of work) {
    const key = weekKey(s.firstPublishedAt || s.updatedAt);
    workWeeks.set(key, (workWeeks.get(key) || 0) + 1);
  }
  for (const [week, n] of workWeeks) if (n < WORK_MIN_ITEMS) out.add(`/work/week/${week}`);
  if (work.length < WORK_MIN_ITEMS) out.add("/work");
  if (new Set(work.map((s) => `${s.workCard.tool}|${s.workCard.maker || ""}`.toLowerCase())).size < WORK_MIN_ITEMS) {
    out.add("/work/tools");
  }
  const jobs = jobCounts(work);
  for (const [key, slug] of Object.entries(JOB_SLUGS)) if (!jobIndexable(jobs[key])) out.add(`/work/${slug}`);
  // Models: a thin model page is noindex; a topic hub under a tracked model's slug is a redirect page.
  for (const page of modelPages(stories, entities, models).values()) {
    if (!modelIndexable(page)) out.add(`/models/${page.slug}`);
    out.add(`/topic/${page.slug}`);
  }
  return out;
}
