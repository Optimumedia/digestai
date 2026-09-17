/* Which pages are worth putting in front of search engines. Plain JavaScript because both the
   page templates (through data.ts) and astro.config.mjs (the sitemap) import it, and the config is
   loaded before TypeScript is available. A page that fails these rules is still built and linked,
   but carries "noindex, follow" and stays out of every sitemap: hundreds of one-story hubs and
   single-source briefs were most of what Google saw, and it indexed almost nothing. */

export const TOPIC_MIN_STORIES = 3;
export const DAILY_MIN_STORIES = 3;
export const WEEK_MIN_STORIES = 5;
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

/** Every page path that carries noindex and is left out of the sitemaps, from the exported data. */
export function noindexPaths(stories, entities) {
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
  return out;
}
