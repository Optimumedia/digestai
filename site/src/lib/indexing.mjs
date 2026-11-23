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
/** /work/prices is a page about movement: below this many tools whose price actually changed it is
    a stub, so it is built and linked but carries noindex and stays out of the sitemap. */
export const WORK_PRICES_MIN_ENTRIES = 5;
/** A story with one source, no primary document and importance at or below this is a thin page. */
export const THIN_STORY_IMPORTANCE = 4;

/* ---- Long lists: 60 rows a page ----
   /topic/openai listed 845 stories in 1.4 MB of HTML, which is more than a crawler or an AI assistant
   reads to the end. A list page now shows its newest LIST_PAGE_SIZE rows and the older ones go on
   numbered pages under it (/topic/openai/2, /models/<slug>/2, /category/<key>/2, /daily/<date>/2,
   /threads/2). Page 1 keeps the address, the introduction, the facts and the questions, and is the
   only one indexed: every numbered page is "noindex, follow" and is never in a sitemap. */
export const LIST_PAGE_SIZE = 60;

/** How many pages a list of `total` rows takes (at least one). */
export function listPageCount(total, size = LIST_PAGE_SIZE) {
  return Math.max(1, Math.ceil((total || 0) / size));
}

/** True for a numbered older page of a list (page 2 and up), by its path without a trailing slash. */
export function isOlderListPage(path) {
  const p = String(path || "").replace(/\.html$/, "").replace(/\/$/, "");
  return /^\/(?:topic|models|category|daily)\/[^/]+\/\d+$/.test(p) || /^\/threads\/\d+$/.test(p);
}

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
    `models` is trackers.json's model list; without it no model pages are counted. `priceChanges` is
    work-prices.json's change list; without it /work/prices counts as thin. */
export function noindexPaths(stories, entities, models = [], priceChanges = []) {
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
  if ((priceChanges?.length || 0) < WORK_PRICES_MIN_ENTRIES) out.add("/work/prices");
  // Models: a thin model page is noindex; a topic hub under a tracked model's slug is a redirect page.
  for (const page of modelPages(stories, entities, models).values()) {
    if (!modelIndexable(page)) out.add(`/models/${page.slug}`);
    out.add(`/topic/${page.slug}`);
  }
  return out;
}

/* ---- The sitemap: the best pages only ----
   Indexable is not the same as listed. Every page above that is not noindex can be indexed and is
   linked from the site, but the sitemap names only the pages we most want crawled: the stories search
   engines are offered (storySearchWorthy below, at most SITEMAP_MAX_STORIES of them) and the hubs
   that lead to them. With 1,800 URLs listed and almost no backlinks, Google crawled most of them and
   indexed few, and by November 2026 the list had grown to 7,298; a short list of strong pages spends
   the crawl where it counts. Search-quiet pages (below) are never listed. */

/** Threads are listed from this many stories (the same number as THREAD_SEARCH_MIN). */
export const THREAD_SITEMAP_MIN = 5;
/** Fixed hub pages that are listed (when not noindex). Categories, topics, models, threads and the
    AI at Work subpages are matched by the rules in sitemapIndex. */
export const SITEMAP_HUBS = ["", "/today", "/week", "/work", "/work/tools", "/work/prices", "/models", "/funding", "/api", "/cite", "/about", "/listen", "/threads", "/about/how-it-works", "/about/martin"];

/** Publisher identity from a host name: news.example.co.uk and example.co.uk are one source.
    The same rule as pipeline/digest/hold.py registrable(). */
export function registrable(domain) {
  let host = String(domain || "").toLowerCase().trim().replace(/^\.+|\.+$/g, "");
  host = host.split("@").pop().split(":")[0];
  if (host.startsWith("www.")) host = host.slice(4);
  const parts = host.split(".").filter(Boolean);
  if (parts.length >= 3 && parts[parts.length - 1].length === 2 && ["co", "com", "org", "net", "ac", "gov", "edu", "ne", "or"].includes(parts[parts.length - 2])) {
    return parts.slice(-3).join(".");
  }
  return parts.length >= 2 ? parts.slice(-2).join(".") : host;
}

function hostOf(url) {
  try { return new URL(url).hostname; } catch { return ""; }
}

/** Confirmed, as pipeline/digest/export.py build_briefing() decides it: reported by two or more
    publishers (by registrable domain), or by the lab or company itself, or pinned by the owner. */
export function storyConfirmed(s) {
  if (s.pinned || s.hasPrimary) return true;
  const publishers = new Set((s.articles || []).map((a) => registrable(a.domain || hostOf(a.url))).filter(Boolean));
  return publishers.size >= 2;
}

/* ---- Search-quiet pages: for readers and AI assistants, not for Google and Bing ----
   Measured on the live site on 30 Oct 2026: of the 4,112 stories in the sitemap, 2,707 (66%) came
   from a single publisher, and 1,035 topic hubs listed 3 to 11 stories with no text of their own.
   Google had crawled the key pages and declined to index them, and Bing judged every page too low in
   quality to show. A rewrite of one outlet's article, or a list of three headlines, is the kind of
   page that pulls a whole site down. Digest AI's own value is the story that merges several
   publishers, so those, and the hubs with real depth, are what search engines are offered.

   A search-quiet page is still built, linked, readable and in the site search. It carries
   <meta name="googlebot"> and <meta name="bingbot"> "noindex, follow" (Base.astro `quiet`), while
   the general robots tag stays "index": assistants that cite the site (ChatGPT is the biggest
   referrer) keep every page. It is left out of every sitemap and of IndexNow. */

/** Distinct publishers (registrable domains) that reported a story. */
export function storyPublishers(s) {
  return new Set((s.articles || []).map((a) => registrable(a.domain || hostOf(a.url))).filter(Boolean)).size;
}

/** A single-publisher story is offered to search engines only from this importance (the "hot" chip). */
export const SEARCH_SINGLE_SOURCE_IMPORTANCE = 8;

/** Whether search engines are offered a story: indexable, and reported by two or more publishers, or
    one of the most important stories of the day, or pinned by the owner. A company's own announcement
    alone does not count: the company's page outranks a summary of it. */
export function storySearchWorthy(s) {
  if (!storyIndexable(s)) return false;
  return Boolean(s.pinned) || storyPublishers(s) >= 2 || (s.importance ?? 0) >= SEARCH_SINGLE_SOURCE_IMPORTANCE;
}

/** Whether a story's page goes in the sitemap (the sitemap also caps how many, SITEMAP_MAX_STORIES). */
export function storyInSitemap(s) {
  return storySearchWorthy(s);
}

/** At most this many live stories are listed, strongest first (most publishers, then newest). */
export const SITEMAP_MAX_STORIES = 1000;

/** A topic hub is offered to search engines from this many stories, or from TOPIC_SEARCH_MIN_WITH_INTRO
    when it has its own written introduction (topics.json); below that it is a list of headlines. */
export const TOPIC_SEARCH_MIN = 12;
export const TOPIC_SEARCH_MIN_WITH_INTRO = 6;
/** A developing-story thread is offered from this many stories. */
export const THREAD_SEARCH_MIN = 5;

/** Whether a topic's written introduction is a real one (the pipeline's placeholder is a count line). */
export function topicHasIntro(info) {
  const d = String(info?.description || "").trim();
  return Boolean(d) && !/^\d+ stor(y|ies) since /.test(d);
}

export function topicSearchWorthy(count, info) {
  return count >= TOPIC_SEARCH_MIN || (count >= TOPIC_SEARCH_MIN_WITH_INTRO && topicHasIntro(info));
}

export function threadSearchWorthy(t) {
  return (t?.storyCount || t?.storyIds?.length || 0) >= THREAD_SEARCH_MIN;
}

/** A model page built on its launch story alone repeats that story; from two stories it is a page. */
export function modelSearchWorthy(page) {
  return modelIndexable(page) && page.storyIds.size >= 2;
}

/** An archive page is offered when two or more publishers covered the story. */
export function archivedSearchWorthy(a) {
  return archivedInSitemap(a);
}

/** True for the daily archive (/daily and /daily/<date>): lists of headlines that the category pages,
    the weekly recaps and the stories themselves already carry. */
export function isDailyArchive(path) {
  return /^\/daily(?:\/[^/]+)?$/.test(String(path || "").replace(/\.html$/, "").replace(/\/$/, ""));
}

/** Every search-quiet path, from the exported data (pages already noindex are not repeated). */
export function quietPaths({ stories = [], entities = [], models = [], threads = [], archived = [], topics = {} } = {}) {
  const out = new Set();
  for (const s of stories) if (storyIndexable(s) && !storySearchWorthy(s)) out.add(`/story/${s.slug}`);
  const live = new Set(stories.map((s) => s.slug));
  for (const a of archived) if (a.slug && !live.has(a.slug) && !archivedSearchWorthy(a)) out.add(`/story/${a.slug}`);
  const modelSlugs = new Set();
  for (const page of modelPages(stories, entities, models).values()) {
    modelSlugs.add(page.slug);
    if (modelIndexable(page) && !modelSearchWorthy(page)) out.add(`/models/${page.slug}`);
  }
  for (const [slug, ids] of topicStoryIds(stories, entities)) {
    if (modelSlugs.has(slug) || ids.size < TOPIC_MIN_STORIES) continue; // a redirect or already noindex
    if (!topicSearchWorthy(ids.size, topics[slug])) out.add(`/topic/${slug}`);
  }
  for (const t of threads) if (!threadSearchWorthy(t)) out.add(`/thread/${t.slug}`);
  return out;
}

/** When a story last changed as its page shows it: first published, or the newest source the page
    lists. Never updatedAt on its own, which moves when an article is only counted and would make an
    old story look new; updatedAt only caps a feed's future-dated article. */
export function storyModified(story) {
  const first = story.firstPublishedAt;
  const firstT = Date.parse(first || "");
  if (Number.isNaN(firstT)) return first || story.updatedAt || null;
  let latest = firstT;
  for (const a of story.articles || []) {
    const t = Date.parse(a.publishedAt || "");
    if (!Number.isNaN(t) && t > latest) latest = t;
  }
  const ceiling = Date.parse(story.updatedAt || "");
  if (!Number.isNaN(ceiling) && latest > ceiling) latest = Math.max(ceiling, firstT);
  return latest === firstT ? first : new Date(latest).toISOString().replace(/\.000Z$/, "Z");
}

/** An archive page (a story older than the export window) is listed when two or more publishers
    covered it; the archive keeps no importance or primary-source flag. */
export function archivedInSitemap(a) {
  return new Set((a.sources || []).map((x) => registrable(hostOf(x.url))).filter(Boolean)).size >= 2;
}

/** The sitemap plan from the exported data: `include(path)` says whether a built page is listed and
    `lastmod` holds each listed page's last change (a story's as its page states it, a hub's as its
    newest story's). Paths have no trailing slash; the home page is "". */
export function sitemapIndex({ stories = [], entities = [], models = [], threads = [], archived = [], priceChanges = [], topics = {} } = {}) {
  const noindex = noindexPaths(stories, entities, models, priceChanges);
  const quiet = quietPaths({ stories, entities, models, threads, archived, topics });
  const listed = new Set(SITEMAP_HUBS);
  const lastmod = new Map();
  const modById = new Map(stories.map((s) => [s.id, storyModified(s)]));
  const newest = (values) => [...values].filter(Boolean).sort().pop();
  const newestOf = (ids) => newest([...ids].map((id) => modById.get(id)));

  for (const s of stories) lastmod.set(`/story/${s.slug}`, modById.get(s.id));
  // The strongest stories first (most publishers, then newest), at most SITEMAP_MAX_STORIES: older
  // ones stay indexable and linked from their hubs, and the list no longer grows with every run.
  const strength = (s) => (s.pinned || (s.importance ?? 0) >= SEARCH_SINGLE_SOURCE_IMPORTANCE ? 3 : Math.min(storyPublishers(s), 3));
  stories
    .filter(storyInSitemap)
    .map((s) => ({ s, k: strength(s), t: s.firstPublishedAt || s.updatedAt || "" }))
    .sort((a, b) => b.k - a.k || (a.t < b.t ? 1 : a.t > b.t ? -1 : 0))
    .slice(0, SITEMAP_MAX_STORIES)
    .forEach(({ s }) => listed.add(`/story/${s.slug}`));
  const live = new Set(stories.map((s) => s.slug));
  for (const a of archived) {
    if (!a.slug || live.has(a.slug)) continue;
    lastmod.set(`/story/${a.slug}`, a.updatedAt);
    if (archivedSearchWorthy(a)) listed.add(`/story/${a.slug}`);
  }
  const all = newest(modById.values());
  // /week leads with the current week's stories, so it changes whenever a story does.
  for (const p of ["", "/today", "/week"]) if (all) lastmod.set(p, all);
  const byCategory = new Map();
  for (const s of stories) if (s.category) byCategory.set(s.category, [...(byCategory.get(s.category) || []), s.id]);
  for (const [cat, ids] of byCategory) lastmod.set(`/category/${cat}`, newestOf(ids));
  for (const t of threads) {
    lastmod.set(`/thread/${t.slug}`, t.updatedAt);
    if (threadSearchWorthy(t)) listed.add(`/thread/${t.slug}`);
  }
  for (const [slug, ids] of topicStoryIds(stories, entities)) {
    lastmod.set(`/topic/${slug}`, newestOf(ids));
    listed.add(`/topic/${slug}`); // thin, quiet and model slugs are left out in include()
  }
  for (const page of modelPages(stories, entities, models).values()) {
    lastmod.set(`/models/${page.slug}`, newestOf(page.storyIds));
    listed.add(`/models/${page.slug}`);
  }
  const work = stories.filter((s) => s.workCard);
  const workMod = newestOf(work.map((s) => s.id));
  if (workMod) { lastmod.set("/work", workMod); lastmod.set("/work/tools", workMod); }
  const priceMod = [...priceChanges].map((c) => c.at).filter(Boolean).sort().pop();
  if (priceMod) lastmod.set("/work/prices", priceMod);
  for (const [key, slug] of Object.entries(JOB_SLUGS)) {
    const mod = newestOf(work.filter((s) => s.workCard.jobs?.includes(key)).map((s) => s.id));
    if (mod) lastmod.set(`/work/${slug}`, mod);
    listed.add(`/work/${slug}`);
  }
  const workWeeks = new Map();
  for (const s of work) {
    const key = weekKey(s.firstPublishedAt || s.updatedAt);
    workWeeks.set(key, [...(workWeeks.get(key) || []), s.id]);
  }
  for (const [key, ids] of workWeeks) lastmod.set(`/work/week/${key}`, newestOf(ids));
  // Weekly recaps have their own written intro; the thin ones are noindex (WEEK_MIN_STORIES).
  const weeks = new Map();
  for (const s of stories) {
    const key = weekKey(s.firstPublishedAt || s.updatedAt);
    if (key) weeks.set(key, [...(weeks.get(key) || []), s.id]);
  }
  for (const [key, ids] of weeks) lastmod.set(`/week/${key}`, newestOf(ids));
  for (const p of [...lastmod.keys()]) if (!lastmod.get(p)) lastmod.delete(p);

  const include = (path) => {
    const p = String(path || "").replace(/\/$/, "");
    if (noindex.has(p) || quiet.has(p) || isOlderListPage(p) || isDailyArchive(p)) return false;
    return listed.has(p) || /^\/category\/[^/]+$/.test(p) || /^\/(?:work\/)?week\/[^/]+$/.test(p);
  };
  return { include, lastmod, noindex, quiet };
}
