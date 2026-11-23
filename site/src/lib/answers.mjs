/* What the answer pages may state as fact, from the model tracker.

   AI assistants quote a page like /models/latest as written, so a wrong row becomes a wrong answer in
   someone else's product. The tracker is built for browsing and holds rows that are fine in a long
   table and wrong as an answer: a rumour ("Anthropic may launch Opus 6 next week"), a product that is
   not a model ("Decisions API"), a story that only uses the model ("Claude Opus 5.5 turns text prompts
   into motion graphics", dated weeks after the launch), a lab called "Claude" or "TBC". The rules here
   leave those out of every stated answer; the /models table still lists everything.

   Plain JavaScript so the page templates (through data.ts), astro.config.mjs (the sitemap) and the
   Python tests (run in Node) all use the same rules. */
import { entitySlug } from "./indexing.mjs";

/** The tracker's spellings of one organisation, and products named where a lab belongs. */
const LAB_ALIASES = new Map(Object.entries({
  "anthropic": "Anthropic",
  "anthropic pbc": "Anthropic",
  "claude": "Anthropic",
  "openai": "OpenAI",
  "chatgpt": "OpenAI",
  "google": "Google",
  "google deepmind": "Google",
  "google research": "Google",
  "google cloud": "Google",
  "deepmind": "Google",
  "gemini": "Google",
  "alphabet": "Google",
  "meta": "Meta",
  "meta ai": "Meta",
  "meta platforms": "Meta",
  "nvidia": "NVIDIA",
  "microsoft": "Microsoft",
  "microsoft research": "Microsoft",
  "microsoft ai": "Microsoft",
  "mistral": "Mistral AI",
  "mistral ai": "Mistral AI",
  "deepseek": "DeepSeek",
  "deepseek ai": "DeepSeek",
  "liquid ai": "Liquid AI",
  "liquidai": "Liquid AI",
  "moonshot": "Moonshot AI",
  "moonshot ai": "Moonshot AI",
  "kimi": "Moonshot AI",
  "alibaba": "Alibaba",
  "alibaba cloud": "Alibaba",
  "tencent": "Tencent",
  "hunyuan": "Tencent",
  "xai": "xAI",
  "x.ai": "xAI",
  "spacexai": "xAI",
  "grok": "xAI",
  "black forest labs": "Black Forest Labs",
  "bfl": "Black Forest Labs",
  "z.ai": "Z.ai",
  "zhipu": "Z.ai",
  "zhipu ai": "Z.ai",
  "cohere": "Cohere",
  "cohere ai": "Cohere",
  "allen institute for ai": "Allen Institute for AI",
  "ai2": "Allen Institute for AI",
  "elevenlabs": "ElevenLabs",
  "perplexity ai": "Perplexity",
  "perplexity research": "Perplexity",
  "runway ai inc.": "Runway",
  "amazon web services": "Amazon",
  "aws": "Amazon",
  "recursive superintelligence": "Recursive",
  "canva research": "Canva",
}));

/** Longer spellings of the big labs ("Meta Superintelligence Labs", "Alibaba Qwen Team"). Not used on
    a joint credit ("NASA and IBM"), which stays as written. */
const LAB_PATTERNS = [
  [/\bqwen\b|^alibaba\b|^damo academy\b/i, "Alibaba"],
  [/^meta\b/i, "Meta"],
  [/^tencent\b/i, "Tencent"],
  [/^xiaomi\b/i, "Xiaomi"],
  [/^nanbeige\b/i, "Nanbeige"],
];

/** A placeholder, or a note the extraction wrote where the maker's name should be. */
const NO_LAB = /^(?:tbc|tbd|n\/?a|none|null|unknown|the authors?)$|\bunspecified\b|\bunknown\b|^independent project\b|\bpull request\b/i;

/** One organisation, one spelling, or null when the row does not really name a maker. */
export function canonicalLab(lab) {
  const raw = String(lab || "").trim();
  if (!raw || NO_LAB.test(raw)) return null;
  const name = raw.replace(/\s*\([^)]*\)\s*$/, "").trim() || raw;
  const known = LAB_ALIASES.get(name.toLowerCase());
  if (known) return known;
  if (!/\band\b|&|,/.test(raw)) for (const [pattern, label] of LAB_PATTERNS) if (pattern.test(raw)) return label;
  return name;
}

/** The tracker with one spelling per lab: every page that shows a lab reads the rows through this. */
export function withCanonicalLabs(models) {
  return (models || []).map((m) => ({ ...m, lab: canonicalLab(m.lab) }));
}

/** A headline about something that has not happened, or that the reports only suspect. */
const NOT_YET = /\?\s*$|\b(?:may|might|could|reportedly|rumou?rs?|rumou?red|leak(?:s|ed)?|expected|upcoming|soon|plans?|planning|preparing|teas(?:e|es|ed)|hints?|delay(?:s|ed)?|in talks|set to|to launch|will launch|next week|next month)\b/i;
/** Words that say the story is the launch itself. The same family as pipeline/digest/trackers.py. */
const LAUNCH = /\b(?:launch(?:es|ed)?|releas(?:e|es|ed)|unveil(?:s|ed)?|introduc(?:e|es|ed)|debut(?:s|ed)?|announc(?:e|es|ed)|roll(?:s|ed)?[- ]out|ship(?:s|ped)?|open[- ]sourc(?:e|es|ed)|drop(?:s|ped)?|now available|generally available|available|new)\b/i;
/** Tracker kinds that are models. "other" is mostly apps, APIs and agents: real news, not a model
    answer, so an "other" row counts only when its headline itself calls the thing a model. */
const MODEL_KINDS = new Set(["llm", "multimodal", "image", "video", "audio", "code", "embedding", "robotics"]);
const CALLED_A_MODEL = /\bmodels?\b/i;

/** What identifies a model across spellings: lower case, no punctuation, without the lab's own name
    or brand ("Claude Sonnet 5.5" and "Sonnet 5.5" from Anthropic are one model). */
export function modelIdentity(name, lab) {
  const brand = String(lab || "").toLowerCase().split(/\s+/)[0];
  const skip = new Set([brand, ...(brand === "anthropic" ? ["claude"] : [])]);
  const words = entitySlug(String(name || "")).split("-").filter((w) => w && !skip.has(w));
  return `${words.join("")}|${brand}`;
}

/** True when the story headline names the model. Names carry build detail headlines leave out
    ("DeepSeek-V4.1-Flash-20261017", "Deepseek V4.1 Flash (revision 1017)"), so the leading 60% of the
    name is enough when that part has a version in it. "Kumo Relational" does not match a story about
    Kumo Forecast: "Kumo" alone has no version, as pipeline/digest/trackers.py also requires. */
function headlineNames(row) {
  const text = entitySlug(row.storyHeadline || "").replace(/-/g, "");
  const brand = String(row.lab || "").toLowerCase().split(/\s+/)[0];
  const words = entitySlug(String(row.name || "").replace(/\([^)]*\)/g, " ")).split("-")
    .filter((w) => w && w !== brand && !(brand === "anthropic" && w === "claude"));
  if (!words.length) return false;
  if (text.includes(words.join(""))) return true;
  for (let n = words.length - 1; n >= Math.ceil(0.6 * words.length); n--) {
    const part = words.slice(0, n).join("");
    if (part.length >= 4 && /\d/.test(part) && /[a-z]/.test(part) && text.includes(part)) return true;
  }
  return false;
}

/** Why a tracker row may not be stated as a release, or null when it may. A confirmation is a
    separate question (`confirmedRelease`): this one asks only whether the row is a launch at all. */
export function releaseIssue(row) {
  if (!row?.name || !row.date) return "no name or date";
  if (!row.lab) return "no named maker";
  if (!MODEL_KINDS.has(row.kind) && !CALLED_A_MODEL.test(row.storyHeadline || "")) return "not a model";
  if (NOT_YET.test(row.storyHeadline || "")) return "rumour or plan";
  if (!headlineNames(row)) return "story is about something else";
  if (!LAUNCH.test(row.storyHeadline || "")) return "story is not the launch";
  return null;
}

/** Enough to state as a fact on its own: `confirmed` is the launch story's own test (the lab's
    document, or two publishers: indexing.mjs storyConfirmed), passed in because an old row's story may
    have left the build, and then the row's own count of sources decides. */
export function confirmedRelease(row, confirmed) {
  if (releaseIssue(row)) return false;
  return typeof confirmed === "boolean" ? confirmed : (row.sources || 0) >= 2;
}

/** One row per model, newest first. `date` is the first report of the model on any row that is not a
    rumour, since a launch often reaches the tracker first through a story about something else ("OpenAI
    and Anthropic cut prices", dated the day Haiku 5.5 came out); the row itself, and so the story it
    links, is the earliest that passes every rule. Specs a launch row left empty come from later rows.
    `confirmed(row)` says whether a row's story is confirmed, or undefined when it is not in this build. */
export function releaseList(models, confirmed = () => undefined) {
  const rows = [...(models || [])].sort((a, b) => ((a.date || "") < (b.date || "") ? -1 : 1));
  const firstSeen = new Map();
  for (const m of rows) {
    if (!m.date || !m.lab || NOT_YET.test(m.storyHeadline || "")) continue;
    const key = modelIdentity(m.name, m.lab);
    if (!firstSeen.has(key)) firstSeen.set(key, m.date);
  }
  const byModel = new Map();
  for (const m of rows) {
    if (releaseIssue(m)) continue;
    const key = modelIdentity(m.name, m.lab);
    const seen = byModel.get(key);
    if (!seen) {
      byModel.set(key, { ...m, date: firstSeen.get(key) || m.date, slug: entitySlug(m.name), confirmed: confirmedRelease(m, confirmed(m)) });
      continue;
    }
    for (const f of ["license", "context", "link"]) if (!seen[f] && m[f]) seen[f] = m[f];
    seen.confirmed = seen.confirmed || confirmedRelease(m, confirmed(m));
  }
  return [...byModel.values()].sort((a, b) => ((a.date || "") < (b.date || "") ? 1 : -1));
}

/** A lab appears on /models/latest from this many models in the tracker: one-off makers are in the
    monthly lists, and a table of hundreds of names answers nothing. */
export const LATEST_MIN_MODELS = 2;
/** At most this many labs in the table, the ones with the most tracked models. */
export const LATEST_MAX_LABS = 24;

/** The newest confirmed release from each lab with a track record, most recent first. `models` is the
    whole tracker: a lab's track record counts every model it was tracked for, launch row or not. */
export function newestByLab(releases, models) {
  const tracked = new Map();
  for (const m of models || []) {
    if (!m.lab) continue;
    if (!tracked.has(m.lab)) tracked.set(m.lab, new Set());
    tracked.get(m.lab).add(modelIdentity(m.name, m.lab));
  }
  const count = (lab) => tracked.get(lab)?.size || 0;
  const newest = new Map();
  for (const r of releases) if (r.confirmed && !newest.has(r.lab)) newest.set(r.lab, r);
  return [...newest.values()]
    .filter((r) => count(r.lab) >= LATEST_MIN_MODELS)
    .sort((a, b) => count(b.lab) - count(a.lab))
    .slice(0, LATEST_MAX_LABS)
    .map((r) => ({ ...r, tracked: count(r.lab) }))
    .sort((a, b) => ((a.date || "") < (b.date || "") ? 1 : -1));
}

/* ---- One page per month (/models/released/2026-10) ----
   "What AI models came out in October?" has a dated answer that stays true, which is what makes it
   worth an address of its own. A month with too few rows would be a stub, so it is built and linked
   but carries noindex and stays out of the sitemap, like every other thin page. */
export const RELEASE_MONTH_MIN = 5;

/** Releases by month ("2026-10"), newest month first, each month newest first. */
export function releasesByMonth(releases) {
  const months = new Map();
  for (const r of releases) {
    const key = String(r.date || "").slice(0, 7);
    if (!/^\d{4}-\d{2}$/.test(key)) continue;
    if (!months.has(key)) months.set(key, []);
    months.get(key).push(r);
  }
  return new Map([...months.entries()].sort((a, b) => (a[0] < b[0] ? 1 : -1)));
}

export const monthIndexable = (rows) => (rows?.length || 0) >= RELEASE_MONTH_MIN;

/** The answer pages' paths and last changes, for the sitemap plan in indexing.mjs. */
export function answerPages(models) {
  const rows = withCanonicalLabs(models);
  const list = releaseList(rows);
  const lastmod = new Map();
  const thin = new Set();
  if (list[0]?.date) lastmod.set("/models/latest", list[0].date);
  if (newestByLab(list, rows).length < 3) thin.add("/models/latest");
  for (const [key, rows] of releasesByMonth(list)) {
    lastmod.set(`/models/released/${key}`, rows[0].date);
    if (!monthIndexable(rows)) thin.add(`/models/released/${key}`);
  }
  return { lastmod, thin };
}
