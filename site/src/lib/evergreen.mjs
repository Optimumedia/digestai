/* Evergreen search pages: lists and comparisons that answer lasting questions, built from data the
   export already writes (stories.json, trackers.json, work.json). No new pipeline step, no database
   read: everything here runs once per site build.

   - /models/pricing          what each model's API costs, as the coverage stated it
   - /models/open-weights     models you can download, with licence and context window
   - /models/context-windows  context windows, largest first
   - /compare/<a>-vs-<b>      two assistants (ChatGPT vs Claude) or two models, side by side
   - /work/tools/<task>       AI tools for one marketing or small-business task, and the free ones

   Plain JavaScript because astro.config.mjs (the sitemap) imports it before TypeScript is loaded, so
   the pages and the sitemap decide "worth indexing" from the same numbers. Every page has a data
   threshold: below it the page is built but carries noindex and stays out of the sitemap, and a
   model comparison below it is not built at all.

   Nothing here invents a fact. Prices and plans are lifted from Digest AI's own digests only when one
   sentence names one model and states the figure, and the page shows that sentence with its story.
   A fact the coverage never gave is left blank, never guessed. */
import { entitySlug } from "./indexing.mjs";

/* ---- thresholds ---- */
/** /models/pricing is indexable from this many models with a stated price. */
export const MODEL_PRICES_MIN = 8;
/** /models/open-weights and /models/context-windows are indexable from this many models. */
export const MODEL_LIST_MIN = 15;
/** /work/tools/<task> is indexable from this many tools; the free-tools page from FREE_MIN_TOOLS. */
export const TASK_MIN_TOOLS = 6;
export const FREE_MIN_TOOLS = 10;
/** A model comparison is built only when both models share this many stories, have this many facts
    known for both, and at least one sentence of the coverage compares them. */
export const COMPARE_MIN_SHARED = 3;
export const COMPARE_MIN_FACTS = 4;
export const COMPARE_MAX_MODEL_PAIRS = 30;
/** An assistant comparison (always built, from a fixed list) is indexable from this many shared stories. */
export const ASSISTANT_MIN_SHARED = 10;
export const ASSISTANT_MIN_FACTS = 4;

const CHAT_KINDS = new Set(["llm", "multimodal", "code"]);
const USABLE = ["api", "open_weights", "consumer"];

/* ---- who makes a model ----
   The tracker keeps the lab as the coverage spelled it ("Claude", "Alphabet", "Google DeepMind",
   "Nvidia"), and now and then the wrong one (GPT-6 Sol under Microsoft, which resells it). A family
   prefix nobody else uses settles the big three; the rest are spelling variants of one name. */
const FAMILY_LABS = [
  [/^(?:gpt|chatgpt|codex)\b/i, "OpenAI"],
  [/^(?:claude|opus|sonnet|haiku|fable)\b/i, "Anthropic"],
  [/^(?:gemini|gemma)\b/i, "Google"],
];
const LAB_NAMES = [
  [/^(?:open ?ai(?: group)?(?: pbc)?|chatgpt)$/i, "OpenAI"],
  [/^(?:anthropic|claude)$/i, "Anthropic"],
  [/^(?:google(?: deepmind| research| cloud)?|deepmind|alphabet)$/i, "Google"],
  [/^nvidia$/i, "NVIDIA"],
  [/^(?:meta(?: ai| platforms)?|meta(?:.?s)? superintelligence labs?(?: \(msl\))?)$/i, "Meta"],
  [/^mistral(?: ai)?$/i, "Mistral AI"],
  [/^deepseek(?: ai)?$/i, "DeepSeek"],
  [/^(?:liquid ?ai)$/i, "Liquid AI"],
  [/^(?:moonshot(?: ai)?|kimi)$/i, "Moonshot AI"],
  [/^(?:alibaba(?: cloud)?|qwen(?: team)?)$/i, "Alibaba"],
  [/^(?:z\.ai|zhipu(?: ai)?)$/i, "Z.ai"],
  [/^tencent(?: hunyuan)?$/i, "Tencent"],
  [/^(?:microsoft(?: ai)?|mai)$/i, "Microsoft"],
  [/^(?:x\.?ai|grok)$/i, "xAI"],
];

export function canonicalLab(lab, name) {
  const n = String(name || "").trim();
  for (const [rx, label] of FAMILY_LABS) if (rx.test(n)) return label;
  const l = String(lab || "").trim();
  if (!l || /^(?:unknown|n\/a|none|null)$/i.test(l)) return null;
  for (const [rx, label] of LAB_NAMES) if (rx.test(l)) return label;
  return l;
}

/* ---- context windows ---- */
const WORD_NUMBERS = { one: 1, two: 2, four: 4 };

/** Tokens in a context window as the coverage wrote it ("1.05M tokens", "262,144 tokens", "one
    million tokens", "500K token context window"), or null when it is not a token count. */
export function parseContext(text) {
  const t = String(text || "").toLowerCase().replace(/(\d),(?=\d{3}\b)/g, "$1");
  if (!t.trim() || /position|pixel|second|frame|minute|hour/.test(t)) return null;
  const m = t.match(/(\d+(?:\.\d+)?|one|two|four)\s*-?\s*(m\b|million|k\b|thousand)?/);
  if (!m) return null;
  let n = WORD_NUMBERS[m[1]] ?? parseFloat(m[1]);
  const unit = m[2] || "";
  if (unit.startsWith("m")) n *= 1e6;
  else if (unit === "k" || unit === "thousand") n *= 1e3;
  else if (!/token/.test(t) && !/^\s*\d+\s*$/.test(t)) return null;
  n = Math.round(n);
  return n >= 2048 && n <= 1e8 ? n : null;
}

/** "1.05M", "262K", "32K": tokens for a table cell. */
export function formatTokens(n) {
  if (!n) return "";
  if (n >= 1e6) return `${+(n / 1e6).toFixed(2)}M`;
  return `${Math.round(n / 1e3)}K`;
}

/* ---- one entry per model ----
   The tracker can hold one model under two names ("Opus 5.5" and "Claude Opus 5.5"). A name that is
   the tail of exactly one longer name from the same lab, and carries a version number, is the same
   model; its rows join the longer one, whose page is the model's address. */
export function canonicalModels(models = []) {
  const groups = new Map();
  for (const m of models) {
    const slug = entitySlug(m.name || "");
    if (!slug) continue;
    if (!groups.has(slug)) groups.set(slug, []);
    groups.get(slug).push(m);
  }
  const labOf = (rows) => canonicalLab(rows.find((r) => r.lab)?.lab, rows[0].name);
  const heads = [...groups.entries()].map(([slug, rows]) => ({ slug, name: rows[0].name, lower: rows[0].name.toLowerCase(), lab: labOf(rows) }));
  const into = new Map();
  for (const short of heads) {
    if (!/\d/.test(short.name)) continue;
    const longer = heads.filter((l) => l.slug !== short.slug && l.lower.endsWith(` ${short.lower}`) && l.lab === short.lab);
    if (longer.length === 1) into.set(short.slug, longer[0].slug);
  }
  const target = (slug) => (into.has(slug) && !into.has(into.get(slug)) ? into.get(slug) : slug);
  const members = new Map();
  for (const slug of groups.keys()) {
    const t = target(slug);
    if (!members.has(t)) members.set(t, []);
    members.get(t).push(slug);
  }
  const out = [];
  for (const [slug, slugs] of members) {
    const own = groups.get(slug);
    const rows = slugs.flatMap((s) => groups.get(s)).sort((a, b) => ((a.date || "") < (b.date || "") ? 1 : -1));
    const name = own[0].name;
    const ctxRow = rows.find((r) => parseContext(r.context));
    out.push({
      slug,
      name,
      names: [name, ...slugs.filter((s) => s !== slug).map((s) => groups.get(s)[0].name)],
      lab: canonicalLab(rows.find((r) => r.lab)?.lab, name),
      kind: rows.find((r) => r.kind && r.kind !== "other" && r.kind !== "unknown")?.kind || "other",
      availability: USABLE.filter((a) => rows.some((r) => r.availability === a)),
      license: rows.find((r) => r.license)?.license || null,
      context: ctxRow?.context || null,
      contextTokens: ctxRow ? parseContext(ctxRow.context) : null,
      link: rows.map((r) => r.link).find((l) => l && /^https?:\/\//.test(l)) || null,
      firstDate: rows.map((r) => r.date).filter(Boolean).sort()[0] || null,
      launchSlugs: [...new Set(rows.map((r) => r.storySlug).filter(Boolean))],
    });
  }
  return out;
}

/* ---- finding names in text ---- */
const lower = (s) => String(s || "").toLowerCase();

/** True when `name` (lower case) stands as a whole name in `text` (lower case): "gpt-6" is not in
    "gpt-6.1 sol", "opus 5.5" is in "claude opus 5.5". */
export function containsName(text, name) {
  if (!name) return false;
  let i = text.indexOf(name);
  while (i >= 0) {
    const before = text[i - 1] || " ";
    const after = text[i + name.length] || " ";
    const next = text[i + name.length + 1] || " ";
    const continues = /[\w-]/.test(after) || (after === "." && /\d/.test(next));
    if (!/[\w.-]/.test(before) && !continues) return true;
    i = text.indexOf(name, i + 1);
  }
  return false;
}

/** A name worth looking for in running text: with a version number, or two words or more. A
    single plain word ("Muse", "Astra", "Sterling") is too often an ordinary word. */
const searchable = (name) => name.length >= 4 && (/\d/.test(name) || /\s/.test(name.trim()));

/** Our own text of a story, split into sentences: the digest and the key points. Never the
    publishers' articles, which no page may reprint. */
export function storySentences(story) {
  const out = [];
  for (const part of [story.summaryMd || "", ...(story.keyPoints || [])]) {
    const clean = String(part).replace(/\*\*|__|`/g, "").replace(/\s+/g, " ").trim();
    for (const s of clean.split(/(?<=[.!?])(?<!\bvs\.)(?<!\b[A-Z]\.)\s+(?=[A-Z"'“‘$0-9])/)) {
      const t = s.trim();
      if (t.length >= 20 && t.length <= 320) out.push(t);
    }
  }
  return out;
}

/** Ids of the stories about each model: its launch stories, every story whose names list it, and
    every headline that names it. */
export function modelMentions(stories, canon) {
  const byName = new Map();
  for (const m of canon) for (const n of m.names) byName.set(lower(n), m.slug);
  const inHeadlines = canon.flatMap((m) => m.names.filter(searchable).map((n) => [lower(n), m.slug]));
  const out = new Map(canon.map((m) => [m.slug, new Set()]));
  const idBySlug = new Map(stories.map((s) => [s.slug, s.id]));
  for (const m of canon) for (const slug of m.launchSlugs) if (idBySlug.has(slug)) out.get(m.slug).add(idBySlug.get(slug));
  for (const s of stories) {
    for (const n of Object.values(s.entities || {}).flat()) {
      const slug = byName.get(lower(n));
      if (slug) out.get(slug).add(s.id);
    }
    const h = lower(s.headline);
    for (const [n, slug] of inHeadlines) if (containsName(h, n)) out.get(slug).add(s.id);
  }
  return out;
}

/* ---- API prices, from our own digests ----
   A price is taken only from a sentence that names exactly one model, before the figure, and states
   it per million tokens. Anything that could be a different price for the same model is left out:
   cached or batch rates, fast modes, resellers, before-and-after changes, "matches X's pricing". */
const NUM = String.raw`(\d+(?:\.\d+)?)`;
const MILLION = String.raw`(?:1\s?m\b|1\s?million|one million|million|m\b|mtok|1,000,000)`;
const PER = String.raw`(?:per|\/|a)\s*${MILLION}`;
const LABELLED = new RegExp(String.raw`\$${NUM}\s*(?:${PER}\s*)?(?:input|prompt)\b[^$]{0,50}?\$${NUM}\s*(?:${PER}\s*)?(?:output|completion)\b`, "i");
const SHORTHAND = new RegExp(String.raw`\$${NUM}\s*\/\s*\$${NUM}\s*${PER}`, "i");
const INPUT_ONLY = new RegExp(String.raw`\$${NUM}\s*${PER}\s*(?:input|prompt)\b`, "i");
const HAS_MILLION = new RegExp(MILLION, "i");
const NOT_LIST_PRICE = /\bcach(?:e|ed|ing)\b|\bbatch\b|fast mode|priority|off-peak|discount|\d+ ?% off|\bpromo|resell|subscription|\bpreviously\b|\bused to\b|\bwas priced\b|\b(?:down|up|cut|reduced|raised|rises?|falls?) from\b|\bfrom \$|doubl|tripl|\bhalv|\bmatch(?:es|ed|ing)?\b|identical|same as|compared (?:to|with)|rumou?r|leak|expected to|\bwould\b|\bcould\b|\bmight\b|\bup to [\d,]+ ?(?:k |thousand )?tokens|\bunder [\d,]+ ?k?\b|or less\b|or fewer\b|\bprompts? (?:under|over|above)\b|\b(?:drops?|dropped|fell|falls|cuts?|reduc\w*|decreas\w*|lower(?:s|ed)?|raise[sd]?|increase[sd]?|rises?|rose|planned)\b|\b(?:on|via|through) (?:openrouter|fireworks|together|groq|cerebras|baseten|deepinfra|novita|bedrock|vertex|azure)\b/i;
const OTHER_MODEL = /\b(?:gpt|claude|gemini|grok|llama|kimi|glm|qwen|deepseek|mistral|muse|opus|sonnet|haiku|fable)[- ]?[\w.]*\d[\w.]*/gi;

/** {input, output} per million tokens from one sentence, or null. Output can be null. */
export function priceFromSentence(sentence) {
  if (NOT_LIST_PRICE.test(String(sentence || ""))) return null;
  // Only what comes before a comparison is the subject's own price: in "Opus 5.5 is $4/$20 per 1M
  // tokens (vs. Fable's $10/$50)" the second pair belongs to another model.
  const s = String(sentence || "").split(/\b(?:vs\.?|versus|whereas|while|compared|unlike)\b/i)[0];
  if (!HAS_MILLION.test(s)) return null;
  const dollars = (s.match(/\$\s?\d/g) || []).length;
  let m = s.match(LABELLED);
  if (m) return { input: +m[1], output: +m[2] };
  m = s.match(SHORTHAND);
  if (m && dollars === 2) return { input: +m[1], output: +m[2] };
  m = s.match(INPUT_ONLY);
  if (m && dollars === 1) return { input: +m[1], output: null };
  return null;
}

/** The models a sentence names (canonical slugs, with where each is first named), plus whether it
    names a model we do not track. A name inside a longer one ("GPT-6" in "GPT-6 Luna") does not count. */
function namedIn(sentence, canon) {
  const text = lower(sentence);
  const spans = [];
  for (const m of canon) {
    for (const n of m.names) {
      const ln = lower(n);
      if (searchable(n) && containsName(text, ln)) spans.push({ slug: m.slug, at: text.indexOf(ln), end: text.indexOf(ln) + ln.length });
    }
  }
  const kept = spans.filter((x) => !spans.some((y) => y.slug !== x.slug && y.at <= x.at && y.end >= x.end && y.end - y.at > x.end - x.at));
  const hits = new Map();
  for (const x of kept) hits.set(x.slug, Math.min(hits.get(x.slug) ?? 1e9, x.at));
  const known = [...hits.keys()].flatMap((slug) => canon.find((m) => m.slug === slug).names.map(lower));
  const stranger = (text.match(OTHER_MODEL) || []).some((tok) => !known.some((n) => n.includes(tok.toLowerCase()) || tok.toLowerCase().includes(n)));
  return { hits, stranger };
}

/** A product the tracker holds that is not a model ("Decisions API"): its price is not a model's. */
const NOT_A_MODEL_NAME = /\b(?:API|app|platform|agent|assistant)$/i;
/** Words a sentence may open with before a figure that is the launched model's own ("The pricing is
    set at", "It is available via the Liquid API at"). */
const PLAIN_LEAD = new Set(["the", "it", "its", "this", "that", "these", "pricing", "priced", "price", "prices", "access", "developers",
  "usage", "input", "output", "costs", "cost", "at", "for", "with", "through", "via", "available", "currently", "both", "each",
  "and", "but", "plus", "users", "customers", "a", "an", "in", "on", "per", "tokens", "only", "now", "standard", "list"]);
/** Another model named before the figure: "the base model ($0.15/$0.50) before swapping to GLM-Legal". */
const ANOTHER_MODEL = /\b(?:base|previous|predecessor|older|earlier|existing|other|rival|competing|sister|parent|larger|smaller)\s+(?:model|version|tier)s?\b/i;

/** In the launch story of a model, a figure in a sentence that does not name it is that model's
    price only when nothing else is named before the figure and the model is not named after it. */
function pricesTheLaunch(clause, model) {
  const at = clause.indexOf("$");
  const before = clause.slice(0, at);
  const after = lower(clause.slice(at));
  if (ANOTHER_MODEL.test(before)) return false;
  if (model.names.some((n) => after.includes(lower(n)))) return false;
  const own = new Set([...model.names, model.lab || ""].flatMap((n) => lower(n).split(/[\s\-]+/)).filter(Boolean));
  for (const word of before.match(/[A-Za-z][\w'’.-]*/g) || []) {
    if (!/^[A-Z]/.test(word) || /^[A-Z0-9]+s?$/.test(word) || /\d/.test(word)) continue; // "API", "GPUs", "FP8"
    const w = lower(word).replace(/['’]s$/, "");
    if (!PLAIN_LEAD.has(w) && !own.has(w)) return false; // "Neon costs $3": another model's price
  }
  return true;
}

/** Each model's API price per million tokens, newest statement first: {input, output, at, slug,
    headline, quote, sources, primary, earlier}. `earlier` is the newest older statement that gave a
    different figure, so a page can say a price moved instead of hiding it. */
export function modelPrices(stories, canon) {
  // A story that is the tracker's launch story for exactly one model, and whose headline names it, is
  // about that model: "The model costs $0.60 per million input tokens" there is that model's price.
  const launchOf = new Map();
  for (const m of canon) for (const slug of m.launchSlugs) launchOf.set(slug, launchOf.has(slug) ? null : m);
  const found = new Map();
  for (const s of stories) {
    const launched = launchOf.get(s.slug);
    // In its own launch story a one-word name ("Glimpse") is safe to match, with its capital letters.
    const about = launched && launched.names.some((n) => (searchable(n) ? containsName(lower(s.headline), lower(n)) : n.length >= 3 && containsName(s.headline || "", n))) ? launched : null;
    for (const sentence of storySentences(s)) {
      if (!sentence.includes("$")) continue;
      // A price belongs to its own clause: in "Gemini 4 Hydrogen is free in AI Studio; Neon costs
      // $3.00/$15.00 per 1M tokens" the figure is Neon's, not Hydrogen's.
      const clause = sentence.split(/;\s+/).find((c) => c.includes("$")) || sentence;
      const price = priceFromSentence(clause);
      if (!price) continue;
      const { hits, stranger } = namedIn(clause, canon);
      if (stranger || hits.size > 1) continue;
      let slug;
      if (hits.size === 1) {
        const [[named, at]] = [...hits.entries()];
        if (at > clause.indexOf("$")) continue; // the model must come before the figure it pays
        slug = named;
      } else if (about && pricesTheLaunch(clause, about)) {
        slug = about.slug;
      } else continue;
      if (NOT_A_MODEL_NAME.test(canon.find((m) => m.slug === slug)?.name || "")) continue;
      if (!found.has(slug)) found.set(slug, []);
      found.get(slug).push({ ...price, at: s.firstPublishedAt, slug: s.slug, headline: s.headline, quote: sentence, sources: s.articleCount || 1, primary: Boolean(s.hasPrimary) });
    }
  }
  const out = new Map();
  for (const [model, list] of found) {
    list.sort((a, b) => ((a.at || "") < (b.at || "") ? 1 : (a.at || "") > (b.at || "") ? -1 : b.sources - a.sources));
    const now = list[0];
    // A same-story sentence that adds the output price the newest one left out completes it.
    if (now.output == null) {
      const fuller = list.find((p) => p.slug === now.slug && p.input === now.input && p.output != null);
      if (fuller) Object.assign(now, { output: fuller.output, quote: fuller.quote });
    }
    const earlier = list.find((p) => p.at < now.at && (p.input !== now.input || (p.output != null && now.output != null && p.output !== now.output)));
    // An introductory or launch price says so in its own sentence; the page labels it.
    out.set(model, { ...now, intro: /\bintro(?:ductory)?\b|launch pric/i.test(now.quote), earlier: earlier ?{ input: earlier.input, output: earlier.output, at: earlier.at, slug: earlier.slug } : null });
  }
  return out;
}

/** "$2", "$2.70", "$0.10", "$0.042": money keeps its pennies, a round figure keeps none. */
export function usd(n) {
  if (n == null) return "";
  if (Number.isInteger(n)) return `$${n}`;
  return `$${n >= 0.1 ? n.toFixed(2) : n.toFixed(3).replace(/(\.\d\d\d?)0$/, "$1")}`;
}

/** "$2 in / $10 out", "$0.10 in": a price for a table cell. */
export function priceLabel(p) {
  if (!p) return "";
  return p.output != null ? `${usd(p.input)} in / ${usd(p.output)} out` : `${usd(p.input)} in`;
}

/* ---- comparisons ---- */
const COMPARES = /\b(?:vs\.?|versus|than|compared|while|whereas|ahead of|behind|beat|beats|outperform\w*|overtak\w*|trails?|unlike|instead of)\b/i;

/** Sentences of the coverage that set two things against each other, one per story: those naming
    only these two first, then those that line them up with others; newest first within each. */
function headToHead(stories, ids, testA, testB, others = () => 0, limit = 8) {
  const found = [];
  const list = [...ids].map((id) => stories.byId.get(id)).filter(Boolean)
    .sort((a, b) => ((a.firstPublishedAt || "") < (b.firstPublishedAt || "") ? 1 : -1));
  for (const s of list) {
    const hits = storySentences(s).filter((t) => COMPARES.test(t) && testA(t) && testB(t));
    const hit = hits.find((t) => others(t) === 0) || hits.find((t) => others(t) <= 1);
    if (hit) found.push({ quote: hit, slug: s.slug, headline: s.headline, at: s.firstPublishedAt, sources: s.articleCount || 1, only: others(hit) === 0 });
  }
  return [...found.filter((q) => q.only), ...found.filter((q) => !q.only)].slice(0, limit);
}

const AVAILABILITY_WORDS = { api: "API", open_weights: "Open weights", consumer: "Consumer app" };
const KIND_WORDS = { llm: "Language model", multimodal: "Multimodal model", code: "Coding model", image: "Image model", video: "Video model", audio: "Audio model", embedding: "Embedding model", robotics: "Robotics model" };

/** The rows of a model comparison: a fact goes in when either side has it; `both` counts the rows
    where both do, which is what makes the comparison worth a page. */
function modelFacts(a, b, prices, mentions) {
  const rows = [];
  const add = (key, label, va, vb) => { if (va || vb) rows.push({ key, label, a: va || "", b: vb || "" }); };
  add("maker", "Made by", a.lab, b.lab);
  add("first", "First covered", a.firstDate, b.firstDate);
  add("kind", "Type", KIND_WORDS[a.kind], KIND_WORDS[b.kind]);
  add("availability", "Where to get it", a.availability.map((x) => AVAILABILITY_WORDS[x]).join(", "), b.availability.map((x) => AVAILABILITY_WORDS[x]).join(", "));
  add("license", "Licence", a.license, b.license);
  add("context", "Context window", a.contextTokens ? `${formatTokens(a.contextTokens)} tokens` : "", b.contextTokens ? `${formatTokens(b.contextTokens)} tokens` : "");
  add("price", "API price per 1M tokens", priceLabel(prices.get(a.slug)), priceLabel(prices.get(b.slug)));
  add("stories", "Stories on Digest AI", String(mentions.get(a.slug)?.size || 0), String(mentions.get(b.slug)?.size || 0));
  return { rows, both: rows.filter((r) => r.a && r.b && r.key !== "stories").length };
}

/** Plain differences that follow from the facts alone: newer, larger context window, cheaper. */
function modelDifferences(a, b, prices) {
  const out = [];
  if (a.firstDate && b.firstDate && a.firstDate.slice(0, 10) !== b.firstDate.slice(0, 10)) {
    const [newer, older] = a.firstDate > b.firstDate ? [a, b] : [b, a];
    const days = Math.round((Date.parse(newer.firstDate) - Date.parse(older.firstDate)) / 864e5);
    if (days >= 1) out.push(`${newer.name} was first covered ${days} ${days === 1 ? "day" : "days"} after ${older.name}.`);
  }
  if (a.contextTokens && b.contextTokens && a.contextTokens !== b.contextTokens) {
    const [big, small] = a.contextTokens > b.contextTokens ? [a, b] : [b, a];
    out.push(`${big.name} has the larger context window: ${formatTokens(big.contextTokens)} tokens against ${formatTokens(small.contextTokens)}.`);
  }
  const pa = prices.get(a.slug);
  const pb = prices.get(b.slug);
  if (pa?.output != null && pb?.output != null && pa.output !== pb.output) {
    const [cheap, dear] = pa.output < pb.output ? [[a, pa], [b, pb]] : [[b, pb], [a, pa]];
    const ratio = dear[1].output / cheap[1].output;
    out.push(`${cheap[0].name} was reported cheaper for output: ${usd(cheap[1].output)} per million output tokens against ${usd(dear[1].output)} for ${dear[0].name}${ratio >= 1.5 ? ` (about ${ratio >= 10 ? Math.round(ratio) : +ratio.toFixed(1)} times less)` : ""}.`);
  }
  if (a.availability.includes("open_weights") !== b.availability.includes("open_weights")) {
    const open = a.availability.includes("open_weights") ? a : b;
    out.push(`Only ${open.name} was released as open weights you can download and run yourself.`);
  }
  return out;
}

/** A family name ("GPT-6", "Gemini 4") is not one model: another tracked name starts with it. */
const familyName = (m, canon) => canon.some((o) => o !== m && o.names.some((n) => lower(n).startsWith(`${lower(m.name)} `) || lower(n).startsWith(`${lower(m.name)}.`)));

export function modelPairs(stories, canon, prices, mentions) {
  const candidates = canon.filter((m) => CHAT_KINDS.has(m.kind) && searchable(m.name) && m.lab && !familyName(m, canon) && (mentions.get(m.slug)?.size || 0) >= COMPARE_MIN_SHARED);
  const pairs = [];
  for (let i = 0; i < candidates.length; i++) {
    for (let j = i + 1; j < candidates.length; j++) {
      const [a, b] = [candidates[i], candidates[j]].sort((x, y) => (x.slug < y.slug ? -1 : 1));
      const ma = mentions.get(a.slug);
      const shared = [...mentions.get(b.slug)].filter((id) => ma.has(id));
      if (shared.length < COMPARE_MIN_SHARED) continue;
      const facts = modelFacts(a, b, prices, mentions);
      if (facts.both < COMPARE_MIN_FACTS) continue;
      const names = (m) => m.names.filter(searchable).map(lower);
      const has = (m) => (t) => names(m).some((n) => containsName(lower(t), n));
      // Any other model in the sentence, tracked or not, makes it a line-up rather than a head-to-head.
      const others = (t) => {
        const { hits, stranger } = namedIn(t, canon);
        return [...hits.keys()].filter((slug) => slug !== a.slug && slug !== b.slug).length + (stranger ? 1 : 0);
      };
      const quotes = headToHead(stories, shared, has(a), has(b), others);
      if (!quotes.some((q) => q.only)) continue;
      pairs.push({ kind: "model", slug: `${a.slug}-vs-${b.slug}`, a, b, shared, facts: facts.rows, factCount: facts.both, quotes, differences: modelDifferences(a, b, prices) });
    }
  }
  pairs.sort((x, y) => y.shared.length - x.shared.length || (x.slug < y.slug ? -1 : 1));
  return pairs.slice(0, COMPARE_MAX_MODEL_PAIRS).map((p) => ({ ...p, indexable: true }));
}

/* ---- assistants: the products people compare by name ---- */
export const ASSISTANTS = [
  { key: "chatgpt", name: "ChatGPT", maker: "OpenAI", rx: /\bChatGPT\b/i, plans: /\bChatGPT (?:Go|Plus|Pro|Business|Team|Enterprise)\b/ },
  { key: "claude", name: "Claude", maker: "Anthropic", rx: /\bClaude\b/, plans: /\bClaude (?:Pro|Max|Team|Enterprise)\b|\b(?:Pro|Max) plan\b/ },
  { key: "gemini", name: "Gemini", maker: "Google", rx: /\bGemini\b/, plans: /\bGoogle AI (?:Plus|Pro|Ultra)\b|\bGemini (?:Advanced|Business|Enterprise)\b/ },
  { key: "copilot", name: "Copilot", maker: "Microsoft", rx: /(?<!GitHub )\bCopilot\b/, plans: /\b(?:Microsoft 365 )?Copilot (?:Pro|Business|license|licence|add-on)\b/ },
  { key: "grok", name: "Grok", maker: "xAI", rx: /\bGrok\b/, plans: /\bSuperGrok(?: Heavy| Plus)?\b/ },
  { key: "perplexity", name: "Perplexity", maker: "Perplexity", rx: /\bPerplexity\b/, plans: /\bPerplexity (?:Pro|Max|Enterprise)\b/ },
];
/** The pairs people search for, in that order (Google's suggestions, Oct 2026). */
export const ASSISTANT_PAIRS = [
  ["chatgpt", "claude"], ["chatgpt", "gemini"], ["claude", "gemini"], ["chatgpt", "copilot"], ["chatgpt", "grok"],
  ["claude", "copilot"], ["gemini", "copilot"], ["claude", "grok"], ["gemini", "grok"], ["chatgpt", "perplexity"],
];
const PLAN_REJECT = /\bwas\b|previously|used to|down from|up from|discount|promo|first month|trial|student|india|rupee|bundle|resell/i;
const MONTHLY = String.raw`[^$.;]{0,40}?\(?\$(\d+(?:\.\d{1,2})?)\s*(?:\/|per|a)\s*(?:user\s*(?:\/|per|a)\s*)?(?:mo\b|month)`;

/** Paid plans the coverage priced, newest statement per plan: [{plan, amount, at, slug, quote}]. */
function assistantPlans(stories, assistant) {
  const rx = new RegExp(`(${assistant.plans.source})${MONTHLY}`, assistant.plans.flags.replace("g", ""));
  const others = ASSISTANTS.filter((x) => x.key !== assistant.key);
  const found = new Map();
  for (const s of stories.newest) {
    for (const t of storySentences(s)) {
      if (!t.includes("$") || !assistant.rx.test(t) || PLAN_REJECT.test(t) || others.some((o) => o.rx.test(t))) continue;
      const m = t.match(rx);
      if (!m) continue;
      const plan = /^(?:Pro|Max) plan$/i.test(m[1]) ? `${assistant.name} ${m[1].split(" ")[0]}` : m[1].replace(/ (?:license|licence|add-on)$/i, "");
      const perUser = /\buser\b|\bseat\b/i.test(m[0]);
      if (!found.has(plan)) found.set(plan, { plan, amount: +m[2], perUser, at: s.firstPublishedAt, slug: s.slug, headline: s.headline, quote: t });
    }
  }
  return [...found.values()].sort((a, b) => a.amount - b.amount);
}

function assistantProfile(stories, assistant, canon, prices, tools) {
  const ids = new Set();
  for (const s of stories) {
    const text = `${s.headline} ${Object.values(s.entities || {}).flat().join(" · ")}`;
    if (assistant.rx.test(text)) ids.add(s.id);
  }
  const models = canon.filter((m) => m.lab === assistant.maker && CHAT_KINDS.has(m.kind))
    .sort((a, b) => ((a.firstDate || "") < (b.firstDate || "") ? 1 : -1));
  const ctx = models.filter((m) => m.contextTokens).sort((a, b) => b.contextTokens - a.contextTokens)[0] || null;
  const priced = models.filter((m) => prices.get(m.slug)).map((m) => ({ model: m, price: prices.get(m.slug) }))
    .sort((a, b) => (a.price.output ?? a.price.input * 5) - (b.price.output ?? b.price.input * 5));
  const ownTools = tools.filter((t) => canonicalLab(t.maker, t.tool) === assistant.maker);
  const dates = [...ids].map((id) => stories.byId.get(id)?.firstPublishedAt).filter(Boolean).sort();
  return {
    ...assistant, ids, models, newest: models[0] || null, widest: ctx, cheapest: priced[0] || null,
    plans: assistantPlans(stories, assistant), tools: ownTools, since: dates[0] || null,
  };
}

export function assistantPairs(stories, canon, prices, tools) {
  const profiles = new Map(ASSISTANTS.map((a) => [a.key, assistantProfile(stories, a, canon, prices, tools)]));
  return ASSISTANT_PAIRS.map(([x, y]) => {
    const a = profiles.get(x);
    const b = profiles.get(y);
    const shared = [...a.ids].filter((id) => b.ids.has(id));
    const rows = [];
    const add = (key, label, va, vb) => { if (va || vb) rows.push({ key, label, a: va || "", b: vb || "" }); };
    add("maker", "Made by", a.maker, b.maker);
    add("stories", "Stories on Digest AI", a.ids.size ? String(a.ids.size) : "", b.ids.size ? String(b.ids.size) : "");
    add("newest", "Newest model covered", a.newest ? a.newest.name : "", b.newest ? b.newest.name : "");
    add("context", "Largest context window reported", a.widest ? `${formatTokens(a.widest.contextTokens)} tokens (${a.widest.name})` : "", b.widest ? `${formatTokens(b.widest.contextTokens)} tokens (${b.widest.name})` : "");
    add("price", "Cheapest API price reported, per 1M tokens", a.cheapest ? `${priceLabel(a.cheapest.price)} (${a.cheapest.model.name})` : "", b.cheapest ? `${priceLabel(b.cheapest.price)} (${b.cheapest.model.name})` : "");
    const planLine = (p) => p.plans.slice(0, 3).map((pl) => `${pl.plan} $${pl.amount}${pl.perUser ? " a user" : ""}/mo`).join("; ");
    add("plans", "Paid plans priced in the coverage", planLine(a), planLine(b));
    add("tools", "Tools in AI at Work", a.tools.length ? String(a.tools.length) : "", b.tools.length ? String(b.tools.length) : "");
    const both = rows.filter((r) => r.a && r.b).length;
    const rest = ASSISTANTS.filter((x) => x.key !== a.key && x.key !== b.key);
    const quotes = headToHead(stories, shared, (t) => a.rx.test(t), (t) => b.rx.test(t), (t) => rest.filter((x) => x.rx.test(t)).length);
    return {
      kind: "assistant", slug: `${a.key}-vs-${b.key}`, a, b, shared, facts: rows, factCount: both, quotes,
      indexable: shared.length >= ASSISTANT_MIN_SHARED && both >= ASSISTANT_MIN_FACTS && quotes.some((q) => q.only),
    };
  });
}

/* ---- AI tools for a task (AI at Work) ---- */
/** The tasks marketers and small businesses search for, each matched against what a tool does and
    what its cards say it is for. The five jobs already have pages (/work/<job>); these are narrower. */
export const TASKS = [
  { slug: "seo", heading: "AI tools for SEO", label: "SEO and AI search", covers: "Ranking in search engines and in AI answers: site audits, keyword research, AI visibility tracking and Search Console.", rx: /\bseo\b|search engine optim|search rankings?|organic (?:search|traffic)|keyword research|ai overviews?|ai (?:search )?visibility|answer engine|\baeo\b|search console|site audit/i },
  { slug: "ads", heading: "AI tools for ads", label: "Ads", covers: "Paid ads on Google, Meta, TikTok, Amazon and in AI assistants: writing them, making the creative and running the campaigns.", rx: /\bads\b|\bad (?:copy|creatives?|campaigns?|text|manager)\b|advertis|\bppc\b|performance max|advantage\+|sponsored (?:products?|placements?)/i },
  { slug: "social-media", heading: "AI tools for social media", label: "Social media", covers: "Posting, editing and planning for Instagram, TikTok, LinkedIn, YouTube and the other networks.", rx: /social media|instagram|tiktok|linkedin|facebook|\breels\b|youtube|pinterest|social posts?/i },
  { slug: "email", heading: "AI tools for email and newsletters", label: "Email and newsletters", covers: "Newsletters, campaigns, replies and the everyday inbox work around them.", rx: /\be-?mails?\b|newsletters?|mailchimp|klaviyo|brevo|subject lines?|\binbox\b|\bgmail\b|\boutlook\b/i },
  { slug: "video", heading: "AI tools for video", label: "Video", covers: "Making, editing, captioning and translating video for ads, social and the website.", rx: /\bvideos?\b|footage|\bclips?\b|subtitles|captions|video edit/i },
  { slug: "images", heading: "AI tools for images and design", label: "Images and design", covers: "Product photos, graphics, logos, banners and layouts, without a designer for every change.", rx: /\bimages?\b|\bphotos?\b|graphic design|\bgraphics\b|\bdesigns?\b|illustrat|\blogos?\b|product shots?|mockups?|\bbanners?\b|canva|photoshop/i },
  { slug: "writing", heading: "AI tools for writing", label: "Writing", covers: "Drafting and rewriting blog posts, product descriptions, documents and everyday business writing.", rx: /\bwrit(?:e|es|ing)\b|\bdrafts?\b|copywrit|blog posts?|\barticles?\b|product descriptions?|rewrit|proofread|grammar/i },
  { slug: "online-shops", heading: "AI tools for online shops", label: "Online shops", covers: "Running a shop online: listings, catalogues, checkout, shopping assistants and marketplace selling.", rx: /e-?commerce|online (?:shop|store)s?|shopify|woocommerce|etsy|\bsellers?\b|merchants?|product listings?|checkout|\bcatalog(?:ue)?s?\b|shopping/i },
  { slug: "meetings", heading: "AI tools for meetings and notes", label: "Meetings and notes", covers: "Recording, transcribing and summarising meetings and calls, and keeping the notes in order.", rx: /\bmeetings?\b|transcri|note-?taking|meeting notes|call summar|\bcalls?\b.{0,20}\bsummar/i },
  { slug: "spreadsheets", heading: "AI tools for spreadsheets and data", label: "Spreadsheets and data", covers: "Spreadsheets, dashboards and the numbers behind the business.", rx: /spreadsheets?|excel|google sheets|\bsheets\b|data analysis|dashboards?|analytics/i },
  { slug: "automation", heading: "AI tools for workflow automation", label: "Automation", covers: "Connecting the apps a business already uses and letting repeat work run by itself.", rx: /\bautomat(?:e|es|ed|ing|ion|ions)\b|workflows?|zapier|\bn8n\b|make\.com|no-code/i },
  { slug: "websites", heading: "AI tools for building a website", label: "Websites", covers: "Building and updating a website or landing page, and checking how it does.", rx: /\bwebsites?\b|landing pages?|site builder|web pages?|wordpress|webflow|\bwix\b|squarespace/i },
  { slug: "free", heading: "Free AI tools for small business", label: "Free tools", covers: "Tools the coverage said cost nothing, or have a free tier, so a small team can try them without paying.", free: true },
];
const FREE_KINDS = new Set(["free", "free tier"]);
const HANDLE = /^(?:\/?u\/|@)\S+$|^[A-Za-z][A-Za-z-]*\d{3,}$|^[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+$/;
const DEV_THING = /\.(?:js|py|ts|sh|txt|json|md)$|\b(?:sdk|cli|api|mcp|library|framework|npm|pip|repo)\b/i;

/** A tool a small business can use as it is: a named maker, its own page, no developer needed. */
export function taskEligible(t) {
  const maker = String(t.maker || "").trim();
  if (!maker || HANDLE.test(maker)) return false;
  if (!/^https:\/\//.test(t.link || "")) return false;
  if (t.effort === "needs a developer" || DEV_THING.test(t.tool || "")) return false;
  return true;
}

/** Every task with its tools (newest change first), whether it is worth indexing, and its lastmod.
    A tool belongs to a task when its name or its one line on what it does says so; a model ("Claude
    Opus 5.5") is not a tool, and one name is listed once. `modelNames` are the tracker's names. */
export function taskPages(tools = [], modelNames = []) {
  const models = new Set(modelNames.map(lower));
  const seen = new Set();
  const eligible = tools.filter((t) => {
    const name = lower(t.tool).trim();
    if (!taskEligible(t) || models.has(name) || seen.has(name)) return false;
    seen.add(name);
    return true;
  });
  return TASKS.map((task) => {
    const list = eligible.filter((t) => (task.free ? FREE_KINDS.has(t.costKind) : task.rx.test(`${t.tool} ${t.whatItDoes || ""}`)));
    return {
      ...task,
      tools: list,
      indexable: list.length >= (task.free ? FREE_MIN_TOOLS : TASK_MIN_TOOLS),
      lastmod: list.map((t) => t.lastChange).filter(Boolean).sort().pop() || null,
    };
  });
}

/* ---- everything, once per build ---- */
export function buildEvergreen({ stories = [], models = [], tools = [] } = {}) {
  const list = stories.filter((s) => s.articles?.length);
  list.byId = new Map(list.map((s) => [s.id, s]));
  list.newest = [...list].sort((a, b) => ((a.firstPublishedAt || "") < (b.firstPublishedAt || "") ? 1 : -1));
  const canon = canonicalModels(models);
  const mentions = modelMentions(list, canon);
  const prices = modelPrices(list, canon);
  const bySlug = new Map(canon.map((m) => [m.slug, m]));
  const priced = [...prices.entries()].map(([slug, price]) => ({ model: bySlug.get(slug), price }))
    .sort((a, b) => (a.price.output ?? Infinity) - (b.price.output ?? Infinity) || a.price.input - b.price.input);
  const openWeights = canon.filter((m) => m.availability.includes("open_weights"))
    .sort((a, b) => ((a.firstDate || "") < (b.firstDate || "") ? 1 : -1));
  const contexts = canon.filter((m) => m.contextTokens && CHAT_KINDS.has(m.kind))
    .sort((a, b) => b.contextTokens - a.contextTokens || ((a.firstDate || "") < (b.firstDate || "") ? 1 : -1));
  const pairs = [...assistantPairs(list, canon, prices, tools), ...modelPairs(list, canon, prices, mentions)];
  const tasks = taskPages(tools, canon.flatMap((m) => m.names));

  const newestDate = (values) => values.filter(Boolean).sort().pop() || null;
  const pages = new Map();
  pages.set("/models/pricing", { indexable: priced.length >= MODEL_PRICES_MIN, lastmod: newestDate(priced.map((p) => p.price.at)) });
  pages.set("/models/open-weights", { indexable: openWeights.length >= MODEL_LIST_MIN, lastmod: newestDate(openWeights.map((m) => m.firstDate)) });
  pages.set("/models/context-windows", { indexable: contexts.length >= MODEL_LIST_MIN, lastmod: newestDate(contexts.map((m) => m.firstDate)) });
  for (const p of pairs) pages.set(`/compare/${p.slug}`, { indexable: p.indexable, lastmod: newestDate(p.shared.map((id) => list.byId.get(id)?.firstPublishedAt)) });
  const indexedPairs = pairs.filter((p) => p.indexable);
  pages.set("/compare", { indexable: indexedPairs.length >= 3, lastmod: newestDate(indexedPairs.map((p) => pages.get(`/compare/${p.slug}`).lastmod)) });
  for (const t of tasks) pages.set(`/work/tools/${t.slug}`, { indexable: t.indexable, lastmod: t.lastmod });
  return { canon, bySlug, mentions, prices, priced, openWeights, contexts, pairs, tasks, pages };
}
